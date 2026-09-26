"""Embedded web server"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import ClientConnectionResetError
from rich.console import Console

from src.coara.commands import execute_command
from src.coara.event_bus import Subscription
from src.coara.turn_context import turn
from src.core.config import config_manager
from src.core.logger import logger
from src.ui import web_tab_presence
from src.ui.attach_registry import AttachRegistry
from src.ui.dashboard_auth import check_dashboard_token
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.dashboard_tokens import load_or_create_dashboard_token
from src.ui.handlers import HANDLER_MIXINS
from src.ui.settings_handlers import SettingsHandlers
from src.ui.trace_store import TraceStore
from src.ui.turn_stream import TurnStream
from src.ui.web_interaction_channel import WebRemoteInteractionChannel
from src.ui.web_socket_registry import WebSocketRegistry

if TYPE_CHECKING:
    from src.coara.root import RootCoara

console = Console()


# App key for storing the server instance on the aiohttp Application
WEB_SERVER_APP_KEY: web.AppKey[Any] = web.AppKey("web_server", object)

# 模块会话（FlowRoot 构建对话等）里支持作用在模块主体上的会话级命令； 其余命令维持原语义（作用在主会话前台实例）。
_MODULE_SESSION_COMMANDS = frozenset({"compact"})


def _parent_tool_call_kwargs(frame: dict[str, Any]) -> dict[str, str]:
    """子智能体帧的父工具行标识（``parent_tool_call_id``）；主会话帧不添字段"""
    parent_tool_call_id = str(frame.get("parent_tool_call_id") or "")
    return {"parent_tool_call_id": parent_tool_call_id} if parent_tool_call_id else {}


def _diff_frame_kwargs(frame: dict[str, Any]) -> dict[str, str]:
    """diff 帧的归属字段：父工具行（子智能体产）+ 产生它的工具调用 id"""
    return {key: str(frame.get(key)) for key in ("parent_tool_call_id", "tool_call_id") if str(frame.get(key) or "")}


class WebServer(*HANDLER_MIXINS):  # type: ignore[misc]  # 基类是运行时 mixin 元组，静态展不开
    """Embedded web server that holds RootCoara and serves the SPA + WS + REST"""

    def __init__(
        self,
        root: RootCoara,
        *,
        workspace_dir: Path,
        coara_home: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        skip_trace_persistence: bool = False,
    ) -> None:
        self.root = root
        self.workspace_dir = workspace_dir
        self.coara_home = coara_home
        # web 独立视图（D6）：浏览器端看哪个空间，与全局前台解耦。
        self.host = host
        self.port = port
        self.skip_trace_persistence = skip_trace_persistence
        # When False, the server owns the root and shuts it down in stop().
        self._owns_root = not skip_trace_persistence

        # Trace store — in-process, directly subscribed to root.event_bus.
        self.trace_store = TraceStore(workspace_dir, coara_home=coara_home)
        self._trace_persistence_sub: Subscription | None = None
        self.auth_token = load_or_create_dashboard_token(workspace_dir, coara_home)

        # WS connection registry + interaction channel.
        self.registry = WebSocketRegistry()
        # 工作空间↔端占用唯一事实源（）：attach 占用与后续 web/matrix/CLI 视图占用共享同一张表。attach
        # 注册表只管连接生命周期，占用经 occupancy。
        from src.coara.workspace_occupancy import WorkspaceOccupancy

        self.workspace_occupancy = WorkspaceOccupancy()
        # 外挂 CLI（coara attach）专用：多连接并存。与 webui 单活跃 registry 完全独立，互不顶替；空间占用经共享
        # occupancy 表。
        self.attach_registry = AttachRegistry(self.workspace_occupancy)
        self.interaction_channel = WebRemoteInteractionChannel(self.registry)
        # 浏览器 registry 的错投路径（无浏览器=静默拒/有浏览器=弹错端）。
        from src.ui.attach_interaction_channel import AttachRemoteInteractionChannel

        self.attach_interaction_channel = AttachRemoteInteractionChannel(self)
        # pending prompt 挂起保留，重连后 redeliver_pending 重发。
        self.registry.on_disconnect = self.interaction_channel.mark_connection_disconnected

        # Back-reference on root so tools (save_draft / manage) can push WS navigation messages to the browser without
        # a global registry.
        root._web_server = self

        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._subscriptions: list[Any] = []
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Trace event batching
        self._trace_batch: list[dict[str, Any]] = []
        self._trace_batch_lock = asyncio.Lock()
        self._trace_flush_task: asyncio.Task[None] | None = None

        # Cached runtime state for lightweight heartbeat (avoids reading entire JSONL files every 2 seconds).
        self._last_runtime_snapshot: dict[str, Any] | None = None

        # would queue forever.
        self._chat_tasks: dict[str, set[asyncio.Task]] = {}
        # attach 跟话通道跟踪：conn_id → [(session_id, sender)]。
        self._followup_senders: dict[str, list[tuple[str, Any]]] = {}

        # 回合流注册表：回合与 WS 连接解耦的单一事实源。
        self.__turns: dict[str, TurnStream] | None = None
        # standby 流：无在飞回合时的帧出口（落带 + 带 view_seq 的广播）。 不挂 task、不进 __turns（不参与回合收尾注销）
        # 。
        self.__standby_streams: dict[tuple[str, str], TurnStream] | None = None

        # Per-session turn-chain tails
        self._turn_tails: dict[str, asyncio.Future[None]] = {}

        # 模块级独立会话主体缓存（key=subject，如 "flow" 工作流构建对话）。 惰性创建，全局常驻（不随工作空间切换重建）；
        # module_registry 驱动。
        self._module_roots: dict[str, Any] = {}
        self._module_roots_lock = asyncio.Lock()

        # 编排写穿未绑定草案的 flow 时复用它，不新开草案
        self.active_workflow_draft_id: str | None = None

        # cancel them instead of leaving them dangling.
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        # Web outbound file delivery (send_file → browser). Wired in start().
        self._web_file_bridge: Any | None = None

        # web 会话视图存储：web 聊天区的服务端唯一数据源（实时帧与刷新恢复 同读它）。bind_workspace 惰性重建，
        # 指向当前视图空间。
        from src.ui.view_recorder import shared_view_store

        # 与内核录制器共用同一份存储实例：落带与快照读的是同一条线。
        self._view_store = shared_view_store()
        self._view_store_workspace: Path | None = None
        self._bind_view_store(workspace_dir)
        # 都写入视图存储，刷新后不缺失。
        self._web_followup_view_turns: set[tuple[str, str]] = set()

        # 「没有端在听」就等于丢数据。
        _registry = getattr(self.root, "end_registry", None)
        if _registry is not None and hasattr(_registry, "set_tape_sink"):
            from src.ui.view_recorder import record_view_frame

            _registry.set_tape_sink(
                lambda frame: record_view_frame(frame, coara_home=getattr(self, "coara_home", None))
            )

    def _has_active_web_turn_stream(self, session_id: str) -> bool:
        """True when a web-originated TurnStream for this session is still in flight."""
        sid = str(session_id or "")
        if not sid:
            return False
        for stream in self._turns.values():
            if (
                str(getattr(stream, "session_id", "") or "") == sid
                and str(getattr(stream, "source", "") or "") == "web"
                and str(getattr(getattr(stream, "route", None), "channel_id", "") or "") != "awakened-web"
                and not getattr(stream, "done", True)
            ):
                return True
        return False

    # Lifecycle

    def _raise_if_port_in_use(self) -> None:
        """Detect another process already listening on ``self.port``"""
        try:
            own_pid = psutil.Process().pid
            for conn in psutil.net_connections(kind="inet"):
                if conn.status != psutil.CONN_LISTEN:
                    continue
                if not conn.laddr or conn.laddr.port != self.port:
                    continue
                other_pid = conn.pid
                if other_pid is None or other_pid == own_pid:
                    continue
                try:
                    other = psutil.Process(other_pid)
                    other_name = other.name()
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied:
                    other_name = "<unknown>"
                raise RuntimeError(
                    f"端口 {self.port} 已被 PID {other_pid} ({other_name}) 占用。"
                    f"可能是之前的 coara 进程没有完全退出。"
                    f"请先结束该进程（Windows: taskkill /PID {other_pid} /F），"
                    f"或在启动时指定其他端口。"
                )
        except psutil.AccessDenied:
            logger.warning(f"无法检查端口 {self.port} 占用情况（权限不足），将继续启动")

    async def start(self) -> str:
        """Start the server. Returns the URL with auth token."""
        # Let publish helpers resolve COARA_HOME / persisted dev_repo.txt.
        if self.coara_home:
            import os

            os.environ.setdefault("COARA_HOME", str(self.coara_home))

        # a new process starts, causing duplicated messages / stale state.
        self._raise_if_port_in_use()

        # Subscribe trace persistence (root.event_bus → TraceStore)
        from src.ui.trace_recording import (
            MultiWorkspaceTracePersistence,
            install_multi_workspace_trace_persistence,
        )

        existing = getattr(self.root, "trace_persistence", None)
        if isinstance(existing, MultiWorkspaceTracePersistence):
            self.trace_store = existing.store
        elif not self.skip_trace_persistence:
            # Foreground session may not be bound yet (tests / early boot)
            try:
                fg_dir = self.root.foreground_coara.workspace_dir
            except Exception:
                fg_dir = self.workspace_dir
            persistence = install_multi_workspace_trace_persistence(
                self.root,
                fg_dir,
                coara_home=self.coara_home,
            )
            self.trace_store = persistence.store
            self._trace_persistence_sub = persistence.subscription
            self._subscriptions.append(persistence.subscription)

        # Subscribe trace events for real-time WS broadcast (all topics, filtered in callback).
        ws_sub = self.root.event_bus.subscribe(callback=self._on_trace_event, topic=None)
        self._subscriptions.append(ws_sub)

        # providers 变更广播挂载点（core 层，web 保存/配置助手热重载都经它发）。
        from src.core.providers_events import set_providers_changed_publisher

        set_providers_changed_publisher(self.root.event_bus.publish)

        # web 跟话参与的它端回合结束：补 turn_end 帧到 web 视图（刷新不标中断） 并清理跟话视图标记。
        followup_end_sub = self.root.event_bus.subscribe(
            callback=self._on_web_followup_turn_end,
            topic="turn_end",
        )
        self._subscriptions.append(followup_end_sub)

        # 落盘」路径已删除——它在刷新后把子智能体答复复活成 assistant 气泡。

        # this server's trace_store. Without this, REST handlers
        switch_sub = self.root.event_bus.subscribe(
            callback=self._on_workspace_switched_event,
            topic="workspace_switched",
        )
        self._subscriptions.append(switch_sub)

        # 同空间模型切换：web 视图若 pin 该空间，立即推 runtime（勿等 5s 心跳）
        llm_sub = self.root.event_bus.subscribe(
            callback=self._on_llm_switched_event,
            topic="llm_switched",
        )
        self._subscriptions.append(llm_sub)

        # Registry add/remove/rename
        registry_sub = self.root.event_bus.subscribe(
            callback=self._on_registry_changed_event,
            topic="workspace_registry_changed",
        )
        self._subscriptions.append(registry_sub)

        # Wire send_file → Web before serving so tools are ready on first chat.
        self._wire_outbound_file_bridge()

        self.app = web.Application(middlewares=[self._version_header_middleware])
        self.app[WEB_SERVER_APP_KEY] = self

        # SPA + static
        self.app.router.add_get("/", self._handle_index)
        # 内核忙闲探询：自动更新 pending 应用前置守卫；挂在 app 级路由， 即使 REST 注册因故跳过，此端点也始终可用。
        self.app.router.add_get("/api/v1/kernel/busy", self._handle_kernel_busy)
        # supervisor 托盘退出通道：本机 + token 鉴权，触发优雅收尾（与信号退出
        # 同一条 stop_event 路径），supervisor 拿不到响应才兜底 terminate
        self.app.router.add_post("/api/v1/kernel/shutdown", self._handle_kernel_shutdown)
        self.app.router.add_get("/ws", self._handle_websocket)
        # 外挂 CLI（coara attach）专用端点：多连接并存，按发起连接路由回合输出。 与 webui 单活跃 /ws 完全独立，
        # 互不顶替。
        self.app.router.add_get("/ws/attach", self._handle_attach_websocket)

        # REST API — reuse dashboard_handlers' proven handlers.
        self._register_rest_routes()

        # Static file serving (Vite build output + attachments).
        static_dir = Path(__file__).resolve().parent / "static"
        if static_dir.is_dir():
            self.app.router.add_static("/static", static_dir, show_index=False)
        attachments_dir = self.workspace_dir / ".coara" / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        self.app.router.add_static("/attachments", attachments_dir, show_index=False)

        # serve index.html so client-side routing works on deep links.
        self.app.router.add_get("/{tail:.*}", self._handle_spa_fallback)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        # 托盘线程推 focus 用；跨进程 open_or_focus 也能找到本实例。
        self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        global _ACTIVE_WEB_SERVER
        _ACTIVE_WEB_SERVER = self

        # Heartbeat: periodically flush trace store + push state updates.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        url = self.build_url()
        return url

    def build_url(self, path: str = "", *, bust: bool = False) -> str:
        """Return the Web UI entry URL with the auth token baked in.

        ``path`` 为 SPA 路由（如 ``/login``），缺省进首页
        """
        route = path if (not path or path.startswith("/")) else f"/{path}"
        query = f"token={self.auth_token}"
        if bust:
            query += f"&_open={int(time.time() * 1000)}"
        # 路由本身可能已带查询串（如 /config?focus=models）——续接要用 &， 无脑接 ? 会产生第二个问号，token 不成参数、
        # 前端全部 API 401
        sep = "&" if "?" in route else "?"
        return f"http://{self.host}:{self.port}{route}{sep}{query}"

    def open_window(self, path: str = "") -> str:
        """打开或唤起 Web UI，返回入口 URL（托盘线程可调）"""
        self._run_open_decision_sync(path)
        return self.build_url(path)

    def _run_open_decision_sync(self, path: str = "") -> dict[str, Any]:
        """同步桥：把决策协程丢回内核 loop 跑（托盘在独立线程）。"""
        # 有意 getattr 防御：托盘线程可在 start()（赋 _loop）之前调用本方法，None 是合法态
        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self.open_or_focus_decision(path), loop)
                return future.result(timeout=web_tab_presence.RECONNECT_GRACE_SECONDS + 2.0)
            except Exception as exc:  # noqa: BLE001 — 决策失败不能挡住「开窗」本身
                logger.debug(f"open decision via loop failed, falling back to open: {exc}")
        # 兜底（loop 不在/不可用）：直接开窗，至少不让用户点了没反应
        url = self.build_url(path, bust=True)
        _open_web_ui_window(url)
        _try_raise_coara_browser_windows()
        return {"action": web_tab_presence.ACTION_OPEN, "reason": "fallback", "opened": True}

    def _request_browser_focus(self, path: str = "") -> None:
        """向活跃标签推 focus_window（托盘线程安全）。"""
        route = path if (not path or path.startswith("/")) else f"/{path}"
        msg: dict[str, Any] = {"type": "focus_window"}
        if route:
            msg["path"] = route
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            self._spawn_bg_task(self.registry.send_to_active(msg))
            return
        # 托盘等外线程：把协程丢进内核 loop（有意 getattr 防御：start 前 _loop 未赋，None 合法）
        stored = getattr(self, "_loop", None)
        if stored is not None and getattr(stored, "is_running", lambda: False)():
            asyncio.run_coroutine_threadsafe(self.registry.send_to_active(msg), stored)
            return
        # 兜底：nowait（仅当已在 loop 线程但 get_running_loop 异常时）
        self.registry.send_to_active_nowait(msg)

    async def _handle_ui_focus(self, request: web.Request) -> web.Response:
        """GET /api/ui/focus — 兼容入口：只做唤起判定，绝不开新窗。"""
        self._check_token(request)
        path = str(request.query.get("path") or "").strip()
        result = await self.open_or_focus_decision(path)
        # focused 语义（与旧外进程调用者兼容）：是否找到了已有标签并唤起过它。
        return web.json_response(
            {
                "focused": result.get("action") != web_tab_presence.ACTION_OPEN,
                **result,
            }
        )

    async def _handle_ui_open(self, request: web.Request) -> web.Response:
        """GET /api/ui/open — 打开或唤起 Web UI 的唯一入口（托盘/协议/外进程都走它）。"""
        self._check_token(request)
        path = str(request.query.get("path") or "").strip()
        return web.json_response(await self.open_or_focus_decision(path))

    async def _handle_presence_ping(self, request: web.Request) -> web.Response:
        """GET /api/ui/presence/ping — 前端心跳：这个标签还在。"""
        self._check_token(request)
        web_tab_presence.mark_tab_seen(self.workspace_dir, coara_home=self.coara_home)
        return web.json_response({"ok": True, "ttl_seconds": web_tab_presence.FRESH_TTL_SECONDS})

    async def _handle_presence_bye(self, request: web.Request) -> web.Response:
        """POST /api/ui/presence/bye — 标签关闭时的告别（前端 sendBeacon）。"""
        self._check_token(request)
        web_tab_presence.mark_tab_left(self.workspace_dir, coara_home=self.coara_home)
        return web.json_response({"ok": True})

    async def open_or_focus_decision(self, path: str = "") -> dict[str, Any]:
        """打开/唤起 Web UI：有活跃标签则导航唤起；否则有 presence 先等重连；仍没有则开新标签。"""
        if self.registry.has_active():
            web_tab_presence.mark_tab_seen(self.workspace_dir, coara_home=self.coara_home)
            self._request_browser_focus(path)
            raised = await asyncio.to_thread(_try_raise_coara_browser_windows)
            if raised:
                return {"action": web_tab_presence.ACTION_FOCUS_ACTIVE, "reason": "active", "opened": False}
            # 标签在后台时窗口标题是别的页（不含 coara），OS 抬不到；浏览器又禁止无手势
            # focus() 切 tab → 体感「点托盘没反应」。再开入口 URL：Chrome/Edge 起新标签，
            # SingleTabGuard 让旧标签让路，用户落到前台 coara。
            url = self.build_url(path, bust=True)
            await asyncio.to_thread(_open_web_ui_window, url)
            await asyncio.to_thread(_try_raise_coara_browser_windows)
            return {
                "action": web_tab_presence.ACTION_FOCUS_ACTIVE,
                "reason": "active-background-nudge",
                "opened": True,
            }

        presence = web_tab_presence.read_presence(self.workspace_dir, coara_home=self.coara_home)
        action = web_tab_presence.decide_open_action(
            has_active=False,
            fresh=web_tab_presence.is_tab_fresh(presence),
        )
        if action == web_tab_presence.ACTION_FOCUS_RECENT:
            # 刚有标签：先唤起 + 等重连；成功则只导航。等不到（浏览器已关 / 标签已死）再开新窗，
            # 避免「点托盘没反应」。新开带 token 时 SingleTabGuard 会让旧僵死标签让路。
            self._request_browser_focus(path)
            raised = await asyncio.to_thread(_try_raise_coara_browser_windows)
            # 抬不到窗＝眼前没有 coara 页，别空等满额 grace（托盘会卡约数秒才开页）。
            grace = web_tab_presence.RECONNECT_GRACE_SECONDS if raised else web_tab_presence.RECONNECT_PROBE_SECONDS
            if await self._wait_for_tab_reconnect(grace):
                self._request_browser_focus(path)
                if raised or await asyncio.to_thread(_try_raise_coara_browser_windows):
                    return {"action": action, "reason": "recent-reconnected", "opened": False}
                url = self.build_url(path, bust=True)
                await asyncio.to_thread(_open_web_ui_window, url)
                await asyncio.to_thread(_try_raise_coara_browser_windows)
                return {"action": action, "reason": "recent-background-nudge", "opened": True}

        url = self.build_url(path, bust=True)
        await asyncio.to_thread(_open_web_ui_window, url)
        await asyncio.to_thread(_try_raise_coara_browser_windows)
        return {
            "action": web_tab_presence.ACTION_OPEN,
            "reason": "no-tab" if action == web_tab_presence.ACTION_OPEN else "recent-gone",
            "opened": True,
        }

    async def _wait_for_tab_reconnect(self, seconds: float) -> bool:
        """等标签重连：内核重启后标签会自动重连，通常几百毫秒内到位。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.registry.has_active():
                return True
            await asyncio.sleep(0.05)
        return self.registry.has_active()

    async def _handle_loading_phrases_custom(self, request: web.Request) -> web.Response:
        """GET /api/loading-phrases/custom — 当日 daily 定制轮播词（Web spinner）"""
        self._check_token(request)
        from src.records.loading_phrases import CUSTOM_PHRASES_FILENAME, custom_payload

        agent_dir: Path | None = None
        store = getattr(self.root, "records_store", None)
        if store is not None:
            agent_root = getattr(store, "agent_root", None)
            if agent_root is not None:
                candidate = Path(agent_root)
                if candidate.is_dir():
                    agent_dir = candidate
        if agent_dir is None and self.coara_home:
            fallback = Path(self.coara_home) / "users" / "default" / "records" / "agent"
            if (fallback / CUSTOM_PHRASES_FILENAME).is_file():
                agent_dir = fallback
        return web.json_response(custom_payload(agent_dir=agent_dir))

    async def stop(self) -> None:
        # Bound the whole stop so CLI cancel + gather never waits on a sync join.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(self._stop_inner(), timeout=6.0)

    async def _stop_inner(self) -> None:
        # wait_for cannot fire.
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._trace_flush_task:
            self._trace_flush_task.cancel()
            self._trace_flush_task = None

        # Cancel tracked fire-and-forget tasks (command handlers, state pushes).
        bg_tasks = list(self._bg_tasks)
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(*bg_tasks, return_exceptions=True),
                    timeout=1.0,
                )
        self._bg_tasks.clear()

        # Cancel in-flight Web chat turns (root + 模块主体) before module-root teardown.
        for tasks in list(self._chat_tasks.values()):
            for task in tasks:
                task.cancel()
        flat_chat = [t for ts in self._chat_tasks.values() for t in ts]
        self._chat_tasks.clear()
        if flat_chat:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(*flat_chat, return_exceptions=True),
                    timeout=1.0,
                )

        # Shutdown 全部模块级独立主体（FlowRoot 等）：先打断进行中回合，再有界释放图。
        module_roots, self._module_roots = list(self._module_roots.values()), {}
        for module_root in module_roots:
            with contextlib.suppress(Exception):
                module_root.interrupt_current_turn("shutdown", interrupt_source="web_server_stop")
            coordinator = getattr(module_root, "flow_coordinator", None)
            if coordinator is not None:
                try:
                    await asyncio.wait_for(coordinator.reset(), timeout=2.0)
                except TimeoutError:
                    logger.warning("module-root coordinator reset timed out on stop")
                except Exception as exc:
                    logger.warning(f"module-root coordinator reset on stop failed: {exc}")
            try:
                await asyncio.wait_for(module_root.shutdown(), timeout=1.5)
            except TimeoutError:
                logger.warning("module-root shutdown timed out on stop")
            except Exception as exc:
                logger.warning(f"module-root shutdown on stop failed: {exc}")

        # Flush any remaining batched trace events.
        with contextlib.suppress(Exception):
            await self._flush_trace_batch()

        for sub in list(self._subscriptions):
            with contextlib.suppress(Exception):
                sub.unsubscribe()
        self._subscriptions.clear()

        # Always offload: a sync join on the event loop freezes Ctrl+C exit.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(self.trace_store.close), timeout=2.0)

        # Close web 会话视图存储 writer（flush 剩余帧）。 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_view_store
        # 可能不存在
        view_store = getattr(self, "_view_store", None)
        if view_store is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.to_thread(view_store.close), timeout=2.0)

        if self.site:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.site.stop(), timeout=1.5)
        if self.runner:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.runner.cleanup(), timeout=1.5)
        self.site = None
        self.runner = None
        self.app = None

        # Clear the back-reference on root so tools (save_draft / manage) stop pushing WS navigation messages to this
        # stopped server.
        self.root._web_server = None
        global _ACTIVE_WEB_SERVER
        if _ACTIVE_WEB_SERVER is self:
            _ACTIVE_WEB_SERVER = None
        self._loop = None

        # Shutdown RootCoara ONLY if the server owns it
        if self._owns_root:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.root.shutdown(), timeout=5.0)

    def _spawn_bg_task(self, coro: Any) -> None:
        """Run a fire-and-forget coroutine as a tracked task"""
        task = asyncio.create_task(coro)
        # 测试夹具（__new__ 不跑 __init__）没有 _bg_tasks：任务照跑，只是不追踪
        bg = getattr(self, "_bg_tasks", None)
        if bg is not None:
            bg.add(task)
            task.add_done_callback(bg.discard)

    # Middleware

    @web.middleware
    async def _version_header_middleware(self, request: web.Request, handler: Any) -> web.Response:
        # /attachments is served by add_static which has no per-request handler
        if request.path.startswith("/attachments"):
            self._check_token(request)
        response = await handler(request)
        from src.ui.control_plane import coara_package_version, config_revision

        response.headers["X-Coara-Version"] = coara_package_version()
        response.headers["X-Coara-Config-Revision"] = config_revision()
        # Auth token lives in the URL query (?token=...)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # Auth

    async def _handle_kernel_busy(self, request: web.Request) -> web.Response:
        """GET /api/v1/kernel/busy — 内核忙闲探询（供自动更新 pending 应用前置守卫）。"""
        self._check_token(request)
        from src.coara.background_activity import background_work_active

        active_turns = 0
        sessions = getattr(self.root, "_sessions", None)
        if isinstance(sessions, dict):
            for session in sessions.values():
                coara = getattr(session, "coara", None)
                if coara is not None and coara.has_active_turn():
                    active_turns += 1
        if not sessions and getattr(self.root, "has_active_turn", lambda: False)():
            active_turns = 1

        from src.background.bash_runner import BashBackgroundRunner
        from src.coara.background_agent import BackgroundAgentManager
        from src.tools.builtin.delegate.delegate import _RUNNING_SUBAGENTS

        background_tasks = len(_RUNNING_SUBAGENTS)
        background_tasks += sum(1 for t in BashBackgroundRunner()._tasks.values() if t is not None and not t.done())
        background_tasks += sum(1 for t in BackgroundAgentManager()._tasks.values() if t is not None and not t.done())
        busy = active_turns > 0 or background_work_active()
        return web.json_response({"busy": busy, "active_turns": active_turns, "background_tasks": background_tasks})

    async def _handle_kernel_shutdown(self, request: web.Request) -> web.Response:
        """POST /api/v1/kernel/shutdown — supervisor 托盘退出通道。

        本机回环 + token 双闸。唤醒 stop_event 走与信号退出完全同一条优雅收尾
        路径（trace flush / matrix 游标落盘 / 实例锁释放都在其中）；supervisor
        拿不到响应才兜底 terminate。
        """
        peer = request.remote or ""
        if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return web.json_response({"error": "localhost only"}, status=403)
        self._check_token(request)
        from src.runtime.restart import wake_stop_event

        if wake_stop_event():
            return web.json_response({"stopping": True})
        return web.json_response({"stopping": False, "reason": "no stop event bound"}, status=409)

    def _check_token(self, request: web.Request) -> None:
        """Validate the auth token from the request; raises HTTPUnauthorized on mismatch."""
        check_dashboard_token(
            request,
            workspace_dir=self.workspace_dir,
            coara_home=self.coara_home,
        )

    async def _handle_client_error(self, request: web.Request) -> web.Response:
        """POST /api/client-error — 前端自检异常上报（重复气泡等渲染违例）"""
        self._check_token(request)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "bad payload"}, status=400)
        event = str(data.get("event") or "client_render_violation")[:80]
        message = str(data.get("message") or "")[:2000]
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else None
        from src.core.error_log import log_session_error_event

        log_session_error_event(
            workspace_dir=Path(str(self.workspace_dir or ".")),
            session_id=str(data.get("session_id") or ""),
            coara_id="web",
            event=event,
            message=message,
            metadata=metadata,
            coara_home=self.coara_home,
        )
        return web.json_response({"ok": True})

    # 账户许可（邮箱验证码登录）

    # 手机接入（代理 gomatrix 数据接口：连接状态 + 配对二维码）

    # 遥测中继（Android 端事件经 PC 上报；凭证只在 PC）

    # SPA index

    async def _handle_index(self, request: web.Request) -> web.Response:
        # deep links to work without the token in the URL.
        dist_index = Path(__file__).resolve().parent / "static" / "dist" / "index.html"
        html = dist_index.read_text(encoding="utf-8") if dist_index.is_file() else self._placeholder_html()
        return web.Response(
            text=self._inject_referrer_policy_meta(html),
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @staticmethod
    def _inject_referrer_policy_meta(html: str) -> str:
        """Ensure index HTML carries <meta name="referrer" content="no-referrer">"""
        if 'name="referrer"' in html or "name='referrer'" in html:
            return html
        meta = '<meta name="referrer" content="no-referrer" />'
        head_open = html.find("<head>")
        if head_open != -1:
            insert_at = head_open + len("<head>")
            return html[:insert_at] + meta + html[insert_at:]
        return meta + html

    async def _handle_spa_fallback(self, request: web.Request) -> web.Response:
        """Catch-all for unmatched GET requests — serve index.html for SPA routes"""
        # No token check — same reasoning as _handle_index.
        path = request.path
        # Skip API and static prefixes (already matched, but guard anyway).
        if path.startswith(("/api/", "/static/", "/attachments/")):
            raise web.HTTPNotFound()
        # Don't intercept files with extensions (e.g. favicon.ico, *.js, *.css).
        if "." in path.rsplit("/", 1)[-1]:
            raise web.HTTPNotFound()
        return await self._handle_index(request)

    def _placeholder_html(self) -> str:
        """Minimal HTML shown when the Vite build hasn't been produced yet."""
        return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>coara</title></head>
<body style="font-family:system-ui;padding:2em;color:#333">
<h1>coara Web UI</h1>
<p>Frontend build not found. Run <code>npm run build</code> in <code>src/ui/web/</code>.</p>
<p>WebSocket endpoint: <code>/ws</code></p>
</body></html>"""

    # REST routes (reused from dashboard_handlers)

    def _register_rest_routes(self) -> None:
        """Register REST API routes, reusing dashboard_handlers' handlers"""
        # Borrow the dashboard REST handlers, sharing our in-process store so REST reads live data.
        dash = DashboardRestHandlers(
            self.workspace_dir,
            coara_home=self.coara_home,
            store=self.trace_store,
            root=self.root,
        )
        self._dash_ref = dash  # keep alive

        r = self.app.router  # type: ignore[union-attr]  # app is set in start() before _register_rest_routes
        dash.register_routes(r)

        # 设置中心 CRUD（reminders / event-sources / workspaces） 走活 service，即时生效
        settings = SettingsHandlers(
            self.workspace_dir,
            coara_home=self.coara_home,
            root=self.root,
            auth_token=dash.auth_token,
        )
        self._settings_ref = settings  # keep alive
        settings.register_routes(r)

        # Native web-server endpoints: file system + workspace + session.
        r.add_get("/api/workspace/list", self._handle_workspace_list)
        r.add_get("/api/workspace/file", self._handle_workspace_file)
        r.add_get("/api/workspace/file-raw", self._handle_workspace_file_raw)
        r.add_get("/api/outbound-files/{file_id}", self._handle_outbound_file)
        r.add_post("/api/workspace/switch", self._handle_workspace_switch)
        # 消息中心：工作空间动态收件箱
        r.add_get("/api/updates/summary", self._handle_updates_summary)
        r.add_get("/api/updates/list", self._handle_updates_list)
        r.add_get("/api/updates/pending", self._handle_updates_pending)
        r.add_post("/api/updates/read", self._handle_updates_read)
        r.add_post("/api/updates/archive", self._handle_updates_archive)
        r.add_post("/api/updates/mark_read", self._handle_updates_mark_read)
        r.add_post("/api/updates/review", self._handle_updates_review)
        r.add_post("/api/upload", self._handle_upload)
        r.add_post("/api/client-error", self._handle_client_error)
        r.add_get("/api/recent-files", self._handle_recent_files)
        r.add_get("/api/session/messages", self._handle_session_messages)
        r.add_post("/api/session/new", self._handle_session_new)
        r.add_post("/api/session/model", self._handle_session_model)
        r.add_get("/api/flow-session/messages", self._handle_flow_session_messages)
        r.add_get("/api/module-session/messages", self._handle_module_session_messages)
        r.add_get("/api/commands", self._handle_command_list)
        r.add_get("/api/trace/events", self._handle_trace_events)
        r.add_get("/api/v1/tool-call", self._handle_tool_call_lookup)
        # 已有标签则只唤起：托盘/外进程先探此接口，避免 webbrowser 新开再被关掉。
        r.add_get("/api/ui/focus", self._handle_ui_focus)
        # 打开/唤起的唯一入口：判定在服务端做（活跃连接→唤起；刚有标签→等重连；否则开新窗）
        r.add_get("/api/ui/open", self._handle_ui_open)
        # 标签存在性信号：前端心跳（30s）与关闭时的 bye，落盘跨内核重启可读
        r.add_get("/api/ui/presence/ping", self._handle_presence_ping)
        r.add_post("/api/ui/presence/bye", self._handle_presence_bye)
        r.add_get("/api/v1/kernel/busy", self._handle_kernel_busy)

        # 账户与遥测接口属可选能力：装了实现包才注册，开源发行版自然没有这两组
        from src.ext import register_account_web, register_telemetry_web

        register_account_web(r, self)
        register_telemetry_web(r, self)

        # 遥测中继（Android） 遥测中继接口属可选能力，见上方 register_telemetry_web

        # Workflow editor endpoints
        r.add_post("/api/workflow-wdl/parse", self._handle_wdl_parse)
        r.add_post("/api/workflow-wdl/emit", self._handle_wdl_emit)
        # 工作台/编辑器当前打开的草案：编排写穿复用它，不新开草案
        r.add_post("/api/workflow-active-draft", self._handle_workflow_active_draft)

        # Records management (agent notes under records/agent/; collect → user)
        r.add_get("/api/records", self._handle_records_list)
        r.add_get("/api/records/item", self._handle_records_get)
        r.add_get("/api/records/file", self._handle_records_file)
        r.add_get("/api/usage/dashboard", self._handle_usage_dashboard)
        r.add_get("/api/usage/range", self._handle_usage_range)
        r.add_get("/api/usage/detail", self._handle_usage_detail)
        r.add_get("/api/usage/pricing", self._handle_usage_pricing)
        r.add_put("/api/usage/pricing", self._handle_usage_pricing_update)
        r.add_delete("/api/records/item", self._handle_records_delete)
        r.add_post("/api/records/archive", self._handle_records_archive)
        r.add_post("/api/records/unarchive", self._handle_records_unarchive)
        r.add_post("/api/records/collect", self._handle_records_collect)
        r.add_post("/api/records/collect-file", self._handle_records_collect_file)

        # 当日 daily 定制轮播词（Web spinner 用；一日抛由后端校验）
        r.add_get("/api/loading-phrases/custom", self._handle_loading_phrases_custom)

    # WebSocket handler — the heart of the web UI

    @staticmethod
    def _emit_end_frame(stream: TurnStream, frame: dict) -> None:
        """内核端帧 → TurnStream 帧：web 聊天区唯一出口的映射，只此一处。"""
        kind = frame.get("kind")
        from src.core.message_tags import is_preformatted_injection

        _head = str(frame.get("text") or "")
        if _head.strip() and is_preformatted_injection(_head):
            logger.debug("web end frame dropped: injected envelope kind=%s head=%r", kind, _head[:40])
            return
        if kind == "subagent_chunk":
            # 子智能体正文：折叠在它的 delegate 工具行里（帧照常落带，读端按父标识归集、不投影）。
            tool_call_id = str(frame.get("tool_call_id") or "")
            if not tool_call_id:
                logger.warning("web end frame dropped: subagent_chunk without parent call id")
                return
            stream.emit(
                "subagent_chunk",
                text=str(frame.get("text") or ""),
                tool_call_id=tool_call_id,
                coara_id=str(frame.get("coara_id") or ""),
                subagent_id=str(frame.get("subagent_id") or ""),
            )
            return
        if kind == "subagent_result":
            # 子智能体的最终答复：同样折叠在它的 delegate 工具行里。
            tool_call_id = str(frame.get("tool_call_id") or "")
            if not tool_call_id:
                logger.warning("web end frame dropped: subagent_result without parent call id")
                return
            stream.emit(
                "subagent_result",
                text=str(frame.get("text") or ""),
                tool_call_id=tool_call_id,
                coara_id=str(frame.get("coara_id") or ""),
            )
            return
        if kind == "diff":
            # 那条 delegate 工具行的展开区，不落在正文流里。
            stream.emit(
                "diff",
                display_blocks=frame.get("display_blocks"),
                diff_lines=frame.get("diff_lines"),
                tool_name=frame.get("tool_name", ""),
                **_diff_frame_kwargs(frame),
            )
            return
        if kind == "tool":
            # 工具行（✓ tool(...)）：进聊天流插在正文段落之间，随 TurnStream persist 落视图文件 → 刷新回放位置不变。
            stream.emit(
                "tool",
                text=str(frame.get("text") or ""),
                ok=not bool(frame.get("is_error", False)),
                tool_name=frame.get("tool_name", ""),
                tool_call_id=frame.get("tool_call_id", ""),
                duration_ms=frame.get("duration_ms"),
                **_parent_tool_call_kwargs(frame),
            )
            return
        text = str(frame.get("text") or "")
        if not text.strip():
            return
        # 子智能体产的正文兜底：内核若因归属判据分叉把它当主会话正文发来（带 parent_tool_call_id 即证），
        # 一律折进发起它的 delegate 行，绝不进主流。
        parent_tool_call_id = str(frame.get("parent_tool_call_id") or "")
        if parent_tool_call_id:
            stream.emit(
                "subagent_chunk",
                text=text,
                tool_call_id=parent_tool_call_id,
                coara_id=str(frame.get("coara_id") or ""),
                subagent_id=str(frame.get("subagent_id") or ""),
            )
            return
        stream.emit("chunk", text=text, block=bool(frame.get("block")))
        logger.debug("web end frame: chunk len=%d head=%r", len(text), text[:40])

    def _web_end_sender(self, stream: TurnStream) -> Any:
        """本回合的端通道 sender（精确槽 (web, session) → 这一条回合流）"""

        def sender(frame: dict) -> bool:
            self._emit_end_frame(stream, frame)
            return True

        return sender

    def _stream_for_frame(self, frame: dict) -> TurnStream | None:
        """按帧归属（turn_id / session_id）找它该去的那条回合流"""
        tid = str(frame.get("turn_id") or "")
        sess = str(frame.get("session_id") or "")
        alive = [s for s in self._turns.values() if not getattr(s, "done", True)]

        def _prefer_web(candidates: list[TurnStream]) -> TurnStream | None:
            if not candidates:
                return None
            for stream in candidates:
                src = str(getattr(stream, "source", "") or "")
                ch = str(getattr(getattr(stream, "route", None), "channel_id", "") or "")
                if src == "web" or ch in ("followup-web", "awakened-web"):
                    return stream
            non_attach = [s for s in candidates if str(getattr(s, "source", "") or "") != "cli-attached"]
            return (non_attach or candidates)[-1]

        if tid:
            matched = [s for s in alive if str(getattr(s, "turn_id", "") or "") == tid]
            hit = _prefer_web(matched)
            if hit is not None:
                return hit
        if sess:
            matched = [s for s in alive if str(getattr(s, "session_id", "") or "") == sess]
            return _prefer_web(matched)
        return None

    def _connection_end_sender(self) -> Any:
        """连接级兜底通道（全局槽 (web, "")）：按帧归属投给对应的回合流"""

        def sender(frame: dict) -> bool:
            stream = self._stream_for_frame(frame)
            if stream is None:
                stream = self._standby_stream_for(frame)
                # WARNING 覆盖。
                logger.debug(
                    "web standby stream for frame kind={} session={} turn={}",
                    frame.get("kind"),
                    frame.get("session_id"),
                    frame.get("turn_id"),
                )
            self._emit_end_frame(stream, frame)
            return True

        sender._end_connection_scope = True  # type: ignore[attr-defined]
        return sender

    def _standby_stream_for(self, frame: dict) -> TurnStream:
        """无在飞回合流时，为帧的 (session, workspace) 建/复用一条 standby 流"""
        from src.ui.turn_stream import TurnStream

        session_id = str(frame.get("session_id") or "")
        # 有意 getattr 防御：standby 帧可在夹具/异常态到达，两个归属字段都可能缺席
        workspace = str(frame.get("workspace_dir") or "") or str(
            getattr(self, "_view_store_workspace", None) or getattr(self, "workspace_dir", "") or ""
        )
        key = (session_id, workspace)
        stream = self._standby_streams.get(key)
        if stream is None:
            # 落带由 TurnStream 缺省走内核录制器，按帧自身的空间归属解析（切空间后 到达的帧落回它自己那个空间）
            # ——落错带等于把内容搬了家。
            stream = TurnStream(
                str(frame.get("turn_id") or ""),
                "web",
                "root",
                self,
                channel_id="standby-web",
                session_id=session_id,
                workspace_dir=workspace,
            )
            self._standby_streams[key] = stream
            if len(self._standby_streams) > self._MAX_STANDBY_STREAMS:
                # 会话/空间切换不断产生新键：超限时按**插入顺序**淘汰最旧的键
                self._standby_streams.pop(next(iter(self._standby_streams)), None)
        stream.turn_id = str(frame.get("turn_id") or "")
        return stream

    def _restore_web_end_senders(self) -> None:
        """WS（重）连后重建在飞 web 回合的端通道（正文/diff/工具行的出口）。"""
        end_registry = getattr(self.root, "end_registry", None)
        if end_registry is None:
            return
        try:
            # 有意 getattr 防御：夹具/异常态下 _turns property 也可能抛错
            turns = list(getattr(self, "_turns", {}).values())
        except Exception:  # noqa: BLE001 — 夹具/异常态下不阻断连接
            return
        for stream in turns:
            if stream.done or stream.source != "web" or stream.subject != "root":
                continue
            with contextlib.suppress(Exception):
                end_registry.register("web", self._web_end_sender(stream), str(getattr(stream, "session_id", "") or ""))

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30.0)
        await ws.prepare(request)
        try:
            self._check_token(request)
        except web.HTTPUnauthorized:
            # Close with an app-defined auth code so the browser client stops reconnecting (HTTP 401 before upgrade
            # surfaces as 1006 otherwise).
            await ws.close(code=4001, message=b"Unauthorized")
            return ws

        conn_id = uuid.uuid4().hex
        await self.registry.register(ws, conn_id)
        # 标签报到：跨内核重启记住「刚才有标签」，供打开/唤起决策使用
        web_tab_presence.mark_tab_seen(self.workspace_dir, coara_home=self.coara_home)
        # 重连即补回在飞回合的端通道：回合与连接解耦但在飞回合的 sender 随旧 连接注销，
        # 不补则回合中途刷新后的正文/diff/工具行全丢。
        self._restore_web_end_senders()
        # 连接级兜底通道（全局槽，只注册不注销）：精确槽被误注销时接住帧。
        end_registry = getattr(self.root, "end_registry", None)
        if end_registry is not None:
            with contextlib.suppress(Exception):
                end_registry.register("web", self._connection_end_sender(), "")

        try:
            # 连上即确保 web view 已 pin（启动 normally 已 pin；兜底未绑定/被清的异常态）
            pinned = getattr(self.root, "pinned_view_id", None)
            if not (callable(pinned) and pinned("web")):
                try:
                    name = ""
                    wm = self.root.workspace_manager
                    if wm is not None:
                        # 缺省跟 cli view（兼容启动前连上的时序）
                        cli_pinned = pinned("cli") if callable(pinned) else None
                        cli_id = cli_pinned or getattr(self.root, "_foreground_session_id", None)
                        entry = wm.registry.get_by_id(str(cli_id)) if cli_id else None
                        name = str(getattr(entry, "name", "") or "") or (wm.active_name or "")
                    if name:
                        await self.root.set_view_workspace("web", name, emit_event=False)
                except Exception as exc:
                    logger.debug(f"web view pin on connect skipped: {exc}")

            # Initial state snapshot (inside try — client may drop right after prepare)
            state = await asyncio.to_thread(self._build_state_snapshot)
            await ws.send_str(json.dumps({"type": "state", "data": state}, ensure_ascii=False))

            # 已结束回合兜底「刚发完就刷新输入/输出都不显示」的窗口。
            for stream in list(self._turns.values()):
                src = str(getattr(stream, "source", "") or "")
                if src != "web":
                    continue
                for frame in stream.replay():
                    replayed = dict(frame)
                    replayed["replayed"] = True
                    await ws.send_str(json.dumps(replayed, ensure_ascii=False))

            # 重连重发挂起的审批提示：断连不再取消 pending approval（挂起保留）， 新连接重放同一 approval_id 的提示帧，
            # 浏览器答复照常 resolve。
            try:
                await self.interaction_channel.redeliver_pending()
            except Exception as exc:
                logger.debug(f"redeliver pending prompts failed: {exc}")

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await self._send_error(ws, "Invalid JSON")
                        continue
                    await self._handle_ws_message(data, ws, conn_id)
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    # After an ERROR frame the connection is broken
                    break
        except ClientConnectionResetError:
            pass
        finally:
            # 仅清理本连接的 task 跟踪表与待处理交互提示。
            self._chat_tasks.pop(conn_id, None)
            # 断连不取消挂起的审批（future 继续等，超时/回合结束兜底）； 重连后在回放区块经 redeliver_pending
            # 重发提示帧。
            self.interaction_channel.mark_connection_disconnected(conn_id)
            await self.registry.unregister(conn_id)
        return ws

    # 外挂 CLI（coara attach）专用 WebSocket 端点

    # /ws/attach 外挂 CLI 端点全家 — 实现见 src/ui/attach_ws.py（AttachWsHandlers）。

    async def _handle_ws_message(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """Route a single WS message to the appropriate handler"""
        msg_type = data.get("type", "")
        subject = str(data.get("subject") or "root")  # "root" 主会话 / 模块主体 subject（如 "flow"）

        if msg_type == "chat":
            if self._is_module_subject(subject):
                # Pending /report 只属于主会话
                task = asyncio.create_task(self._handle_chat(data, ws, conn_id, subject=subject))
                self._chat_tasks.setdefault(conn_id, set()).add(task)

                def _forget_module_chat_task(t: asyncio.Task[Any], cid: str = conn_id) -> None:
                    self._chat_tasks.get(cid, set()).discard(t)

                task.add_done_callback(_forget_module_chat_task)
                return
            # Pending /report descriptions must NOT refresh idle clocks (like slash).
            from src.coara.commands.report import has_pending_report

            if not has_pending_report(self.root):
                wid = ""
                try:
                    wid = self.root.view_workspace_id("web")
                except Exception:
                    wid = ""
                self.root.record_user_activity(workspace_id=wid or None)
            # Track the task so we can cancel it if the WS disconnects
            task = asyncio.create_task(self._handle_chat(data, ws, conn_id))
            self._chat_tasks.setdefault(conn_id, set()).add(task)

            def _forget_chat_task(t: asyncio.Task[Any], cid: str = conn_id) -> None:
                self._chat_tasks.get(cid, set()).discard(t)

            task.add_done_callback(_forget_chat_task)
        elif msg_type == "interrupt":
            module_root = self._module_roots.get(subject) if subject != "root" else None
            if module_root is not None:
                module_root.interrupt_current_turn("user_stop", interrupt_source="stop_command")
            else:
                # 打断目标钉在帧归属空间（与 chat/command 同口径），不读「点停止那 一刻」
                # 的视图指针——切空间窗口内否则会打到无关空间，本端停不掉。
                target = self._view_coara()
                frame_dir = str(data.get("workspace_dir") or "").strip()
                if frame_dir:
                    try:
                        from src.core.coara_home import workspace_id_for

                        bound = self.root.resolve_workspace_coara(workspace_id_for(Path(frame_dir)))
                        if bound is not None:
                            target = bound
                    except Exception:  # noqa: BLE001
                        logger.warning("interrupt frame workspace_dir unresolved: %s", frame_dir, exc_info=True)
                # 征求同意门要等用户在同一连接上回 approval_reply——接收循环不能 inline 等门
                # （等门期间读不到回执＝死锁，只能 300s 超时）。门与打断整体调为后台任务。
                self._spawn_bg_task(self._run_web_interrupt(target))
        elif msg_type == "flow_snapshot":
            # Flow 实时视图：请求指定 flow 的全量图快照
            await self._handle_flow_snapshot(data, ws)
        elif msg_type == "command":
            # Slash commands (incl. /ws switch, /new) must NOT refresh activity clocks.
            self._spawn_bg_task(self._handle_command(data, ws))
        elif msg_type == "approval_reply":
            # 回执统一进 ApprovalCenter 做幂等终态转换；通道只是哑管道
            from src.coara.approval_center import get_approval_center

            approval_id = str(data.get("approval_id") or "")
            approved = bool(data.get("approved", False))
            if approval_id:
                get_approval_center().resolve(
                    approval_id,
                    approved=approved,
                    resolved_by="web",
                    actor=conn_id,
                )
        elif msg_type == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
        else:
            await self._send_error(ws, f"Unknown message type: {msg_type}")

    async def _run_web_interrupt(self, target: Any) -> None:
        """web interrupt 的后台执行体：征求同意门 → 获批打断并转移所有权（接收循环不阻塞）"""
        from src.coara.commands.registry import confirm_cross_end_action

        approved = await confirm_cross_end_action(
            target,
            origin_source="web",
            verb="打断当前回合",
            detail="打断后本端将获得该会话的所有权。",
            interaction_channel=getattr(self, "interaction_channel", None),
        )
        if not approved:
            _registry = getattr(self, "registry", None)
            if _registry is not None:
                await _registry.send_to_active(
                    {"type": "interrupt_result", "interrupted": False, "reason": "cancelled"}
                )
            return
        target.interrupt_current_turn(
            "user_stop",
            interrupt_source="stop_command",
            take_ownership_source="web",
        )

    # Chat turn handler — streams root.process_message chunks to browser

    @staticmethod
    def _is_module_subject(subject: str) -> bool:
        """subject 是否解析到一个启用 agentic 会话的模块（"root" 主会话除外）。"""
        if not subject or subject == "root":
            return False
        from src.coara.module_registry import module_registry

        return module_registry.by_subject(subject) is not None

    async def _create_module_root(self, spec: Any) -> Any:
        """按模块声明创建会话主体。workflow 走专属 create_flow_root"""
        if spec.subject == "flow":
            from src.coara.flow_root import create_flow_root, switch_flow_draft_session
            from src.workflow import draft_sessions

            flow_root = await create_flow_root(self.root)
            # 上报时若一致则 switch 是 no-op。
            active: str | None = None
            try:
                active = getattr(self, "active_workflow_draft_id", None) or draft_sessions.last_active(
                    getattr(self, "coara_home", None)
                )
                if active:
                    switch_flow_draft_session(flow_root, active, coara_home=getattr(self, "coara_home", None))
            except Exception:
                logger.exception("flow draft session cold restore failed (draft=%s)", active)
            return flow_root
        from src.coara.module_root import create_module_root

        return await create_module_root(self.root, spec)

    async def _get_module_root(self, subject: str) -> Any:
        """按 subject 惰性创建/复用模块专属会话主体（module_registry 驱动）"""
        from src.coara.module_registry import module_registry

        spec = module_registry.by_subject(subject)
        if spec is None:
            raise ValueError(f"未知或未启用 agentic 会话的模块主体：subject={subject!r}")
        cached = self._module_roots.get(subject)
        if cached is not None:
            if subject == "flow":
                from src.coara.flow_root import bind_flow_workspace

                # 跟 web view，不跟 cli 前台（多端共视时 spawn 落用户正在看的空间）
                fg_ws = Path(self._view_coara().workspace_dir).expanduser().resolve()
                bind_flow_workspace(cached, fg_ws)
            return cached
        async with self._module_roots_lock:
            cached = self._module_roots.get(subject)
            if cached is not None:
                return cached
            try:
                module_root = await self._create_module_root(spec)
                module_root._web_server = self
                self._module_roots[subject] = module_root
            except Exception:
                logger.exception(
                    "module root creation failed (subject=%s, workspace=%s)",
                    subject,
                    self.root.foreground_coara.workspace_dir,
                )
                raise
            return module_root

    async def _get_flow_root(self) -> Any:
        """兼容入口：FlowRoot = subject "flow" 的模块主体。"""
        return await self._get_module_root("flow")

    async def _handle_chat(
        self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str, *, subject: str = "root"
    ) -> None:
        """Process a chat message: run root.process_message, stream chunks."""
        text = str(data.get("text", "")).strip()
        # 端上生成的消息标识：原样带回 user_message 权威帧，端上据此精确认领乐观气泡 （不靠乐观标记/文本比对，
        # 重挂载净化或文本被改写都不会配错）
        client_msg_id = str(data.get("client_msg_id") or "").strip()
        if not text:
            await self._send_error(ws, "Empty message")
            return

        # Pending /report description — consume before LLM / slash routing. （主会话专属机制，FlowRoot 不参与）
        pending_result = None
        if subject == "root":
            from src.coara.commands.report import try_consume_pending_report_async

            pending_result = await try_consume_pending_report_async(self.root, text)
        if pending_result is not None:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "command_result",
                        "result": {
                            "output": pending_result.output,
                            "action": pending_result.action,
                            "data": pending_result.data,
                            "exit_session": pending_result.exit_session,
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return

        # Image blocks (for vision)
        image_refs = data.get("image_refs") or []
        from src.matrix_client.remote_vision import MAX_IMAGE_BATCH_COUNT

        # 单次图片输入张数上限（三端同 MAX_IMAGE_BATCH_COUNT）：超出部分丢弃并提示
        if len(image_refs) > MAX_IMAGE_BATCH_COUNT:
            image_refs = image_refs[:MAX_IMAGE_BATCH_COUNT]
            await ws.send_str(
                json.dumps(
                    self._error_frame(
                        f"一次最多发送 {MAX_IMAGE_BATCH_COUNT} 张图片，超出的已忽略。",
                        turn_id="",
                        subject=subject,
                    ),
                    ensure_ascii=False,
                )
            )
        file_refs = data.get("file_refs") or []
        if file_refs:
            text = await self._append_text_file_refs(text, file_refs)
        image_blocks: list[dict[str, Any]] | None = None
        if image_refs:
            image_blocks = await self._resolve_image_refs(image_refs)
        # 附件展示元数据（气泡渲染 + 回放）：图片标记 is_image，前端按此渲染缩略图。
        attachments = self._build_upload_attachments(image_refs, file_refs)

        # 模块主体（FlowRoot 等）不绑前台会话（独立于工作空间切换语义）。
        if subject != "root":
            try:
                turn_coara = await self._get_module_root(subject)
            except Exception as exc:
                logger.exception("module root creation failed (subject=%s)", subject)
                with contextlib.suppress(Exception):
                    await ws.send_str(
                        json.dumps(
                            self._error_frame(
                                f"模块主体创建失败（subject={subject}）：{exc}",
                                turn_id="",
                                subject=subject,
                            ),
                            ensure_ascii=False,
                        )
                    )
                return
            turn_ws_id = None
        else:
            # web 独立视图（D6）：聊天回合路由到本端视图空间，与全局前台解耦。
            turn_coara = self._view_coara()
            turn_ws_id = getattr(self.root, "web_view_workspace_id", None) or self.root._foreground_session_id
            # 归属会落到另一个空间，空间之间就不再互不影响。
            frame_dir = str(data.get("workspace_dir") or "").strip()
            if frame_dir:
                try:
                    from src.core.coara_home import workspace_id_for

                    bound_id = workspace_id_for(Path(frame_dir))
                    bound = self.root.resolve_workspace_coara(bound_id)
                    if bound is not None:
                        turn_coara = bound
                        turn_ws_id = bound_id
                except Exception:  # noqa: BLE001 — 归属解析失败退回视图指针
                    # 端报来的 workspace_dir 解析不出=异常而非正常降级，留 WARNING 便于定位（回退行为本身不变：
                    # 退回视图指针照常开回合）。
                    logger.warning("chat frame workspace_dir unresolved: %s", frame_dir, exc_info=True)

        # 无可用 provider 早失败：新装未配 API key 时发消息不该起回合空转 （spinner 一直转、回合结束后才看到错误）。
        # 入口即校验并给配置引导。
        try:
            from src.core.config import config_manager as _cfg_mgr
            from src.llm.model_catalog import _enabled_provider_names

            if not _enabled_provider_names(_cfg_mgr):
                await ws.send_str(
                    json.dumps(
                        self._error_frame(
                            ("还没有配置模型 API Key。请到侧栏「配置 → 模型」添加厂商密钥后再发消息。"),
                            turn_id="",
                            subject=subject,
                            data={"navigate": "/config?focus=models"},
                        ),
                        ensure_ascii=False,
                    )
                )
                return
        except Exception:
            # 校验本身失败不拦消息（fail-open，避免误判阻断正常对话）
            logger.debug("provider key gate check failed, message allowed through (fail-open)", exc_info=True)

        # fail-closed 占位。丢图留文：正文照常发送。
        if image_blocks:
            from src.core.config import config_manager
            from src.llm.vision import model_vision_explicit

            _pname = str(getattr(turn_coara, "provider_name", "") or "")
            _mname = str(getattr(turn_coara, "model_name", "") or "")
            if _mname and model_vision_explicit(_mname, provider_name=_pname, config_manager=config_manager) is False:
                image_blocks = None
                await ws.send_str(
                    json.dumps(
                        self._error_frame(
                            (
                                f"当前模型 {_mname or '未知'} 不支持图像输入，图片已省略；"
                                f"可 /model 切换到支持图像的模型。"
                            ),
                            turn_id="",
                            subject=subject,
                        ),
                        ensure_ascii=False,
                    )
                )

        # 保持原注入语义（构建对话需要回合内上下文，流式由其自有连接承担）。
        _chain_active = False
        if subject == "root":
            _tail_key = str(turn_ws_id or turn_coara.session_id)
            _tail = self._turn_tails.get(_tail_key)
            _chain_active = _tail is not None and not _tail.done()
        if (turn_coara.has_active_turn() or _chain_active) and not text.startswith("/"):
            if subject != "root":
                turn_coara.submit_continuation_input(text, image_blocks=image_blocks, source="web")
                return

            # deferred send_text：审批/交互门回投本端（非聊天正文路径）。
            async def _send_deferred_interactive(_channel_id: str, chunk: str) -> None:
                if not chunk or not chunk.strip():
                    return
                await self.registry.send_to_active(
                    {
                        "type": "chunk",
                        "text": chunk,
                        "source": "web",
                        "subject": subject,
                        "workspace_dir": str(getattr(self, "workspace_dir", "") or ""),
                    }
                )

            turn_coara.set_deferred_remote_ctx(
                conn_id, _send_deferred_interactive, self.interaction_channel, source="web", actor=conn_id
            )
            # 跟话 user_message 与后续正文：正式 TurnStream（同开局 emit_user_message）， 微批+buffer+视图落盘。
            end_registry = getattr(self.root, "end_registry", None)
            if end_registry is not None and subject == "root":
                _sess_id = str(getattr(turn_coara, "session_id", "") or "")
                _turn_id = str(getattr(getattr(turn_coara, "_active_turn", None), "turn_id", "") or "")
                _tape_ws = str(getattr(turn_coara, "workspace_dir", "") or self.workspace_dir or "")

                _target_stream = None
                for _s in self._turns.values():
                    if (
                        str(getattr(_s, "session_id", "") or "") == _sess_id
                        and str(getattr(_s, "source", "") or "") == "web"
                        and str(getattr(getattr(_s, "route", None), "channel_id", "") or "") != "awakened-web"
                        and not getattr(_s, "done", True)
                    ):
                        _target_stream = _s
                        break

                if _target_stream is None:
                    old_sender = end_registry.sender_for("web", _sess_id)
                    if old_sender is not None:
                        end_registry.unregister("web", old_sender, _sess_id)

                    from src.ui.turn_stream import TurnStream

                    _follow_tid = _turn_id or uuid.uuid4().hex
                    _target_stream = TurnStream(
                        _follow_tid,
                        "web",
                        "root",
                        self,
                        channel_id="followup-web",
                        session_id=_sess_id,
                        workspace_dir=_tape_ws,
                    )
                    # 键与内核 turn_id 区分，避免与它端/唤醒流同 id 覆盖
                    self._turns[f"followup-{_follow_tid}"] = _target_stream
                    _key = (_sess_id, _follow_tid)
                    if _key not in self._web_followup_view_turns:
                        _target_stream.emit("turn_start")
                        self._web_followup_view_turns.add(_key)
                    # 注入窗口内回合已结束/换号：turn_end 可能已错过，当场收尾，避免流永挂
                    _live = getattr(turn_coara, "_active_turn", None)
                    _live_tid = str(getattr(_live, "turn_id", "") or "") if _live is not None else ""
                    if (not turn_coara.has_active_turn()) or (_turn_id and _live_tid and _live_tid != _turn_id):
                        _target_stream.emit("turn_end", reason="complete")
                        _target_stream.finish()
                        self._web_followup_view_turns.discard(_key)
                        _target_stream = None

                if _target_stream is not None:
                    # 已有 web 流时也要确保 ("web", session) 指向它——stale sender 或它端回合无通道时段切到 web 会静默丢
                    # chunk。
                    _follow_stream: TurnStream = _target_stream

                    def _route_sender(frame: dict, _stream: TurnStream = _follow_stream) -> None:
                        # 唯一出口：跟话流同样走 _emit_end_frame（kind 分派 + 注入信封闸 + 父标识折叠）。
                        # 这里曾经手写窄化映射（只认 diff/tool，其余一律当正文），子智能体帧会被
                        # 当成主会话正文发进主流。
                        self._emit_end_frame(_stream, frame)

                    end_registry.register("web", _route_sender, _sess_id, kind="followup")
                    _target_stream.emit_user_message(
                        text,
                        attachments=attachments or [],
                        client_msg_id=client_msg_id,
                    )
            turn_coara.submit_continuation_input(
                text, image_blocks=image_blocks, source="web", client_msg_id=client_msg_id
            )
            return

        # 回合已与连接解耦，断连不取消 task，无需 cancel-salvage 兜底。
        tail_key = (f"{subject}:" if subject != "root" else "") + str(turn_ws_id or turn_coara.session_id)
        prev_tail = self._turn_tails.get(tail_key)
        my_tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._turn_tails[tail_key] = my_tail
        try:
            if prev_tail is not None:
                await prev_tail
            await self._stream_chat_turn(
                text,
                ws,
                conn_id,
                image_blocks=image_blocks,
                attachments=attachments,
                bind_coara=turn_coara,
                bind_ws_id=turn_ws_id,
                subject=subject,
                client_msg_id=client_msg_id,
            )

            # Leftover：回合结束后仍在队列的项 = 新回合启动输入（不再是接续）。 必须按每条 ContinuationInput.source
            # 开回合，禁止绑死本 web 连接。
            leftover = turn_coara.drain_continuation_inputs()
            while leftover:
                from src.coara.continuation_leftover import dispatch_leftover_item

                for cont_item in leftover:
                    # 时把标识原样带回权威帧，端上认领原气泡——同一句话不再铺第二条
                    _item_cmid = (
                        str(getattr(cont_item, "client_msg_id", "") or "").strip()
                        if not isinstance(cont_item, str)
                        else ""
                    )

                    async def _run_web(text: str, images: list | None, _cmid: str = _item_cmid) -> None:
                        await self._stream_chat_turn(
                            text,
                            ws,
                            conn_id,
                            image_blocks=images,
                            bind_coara=turn_coara,
                            bind_ws_id=turn_ws_id,
                            subject=subject,
                            client_msg_id=_cmid,
                        )

                    async def _run_matrix(text: str, images: list | None) -> None:
                        from src.coara.continuation_leftover import _dispatch_matrix_via_root

                        await _dispatch_matrix_via_root(
                            self.root,
                            turn_coara,
                            text,
                            images,
                            bind_ws_id=turn_ws_id,
                            trust_level="owner",
                        )

                    await dispatch_leftover_item(
                        self.root,
                        turn_coara,
                        cont_item,
                        finishing_source="web" if subject == "root" else f"web-{subject}",
                        bind_ws_id=turn_ws_id,
                        run_web_turn=_run_web,
                        run_matrix_turn=_run_matrix,
                    )
                leftover = turn_coara.drain_continuation_inputs()
        finally:
            if self._turn_tails.get(tail_key) is my_tail:
                del self._turn_tails[tail_key]
            if not my_tail.done():
                my_tail.set_result(None)

    async def _stream_chat_turn(
        self,
        text: str,
        ws: web.WebSocketResponse,
        conn_id: str,
        *,
        image_blocks: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
        subject: str = "root",
        client_msg_id: str = "",
    ) -> None:
        """Run a single chat turn: send turn_start, stream process_message, send turn_end."""
        turn_id = uuid.uuid4().hex
        source = "web" if subject == "root" else f"web-{subject}"
        tape_ws = str(getattr(bind_coara, "workspace_dir", "") or self.workspace_dir or "")
        stream = TurnStream(
            turn_id,
            source,
            subject,
            self,
            session_id=str(getattr(bind_coara, "session_id", "") or "") if bind_coara is not None else "",
            workspace_dir=tape_ws,
        )
        self._turns[turn_id] = stream
        # 正式用户行先于 turn_start（与中途跟话共用 emit_user_message）。 下方 process_message 仍收未剥的
        # text——模型读完整指令。
        stream.emit_user_message(
            text,
            attachments=attachments or [],
            client_msg_id=client_msg_id,
        )
        stream.emit("turn_start")
        # 主会话回合串行（_process_lock）：CLI/Matrix 占着回合时 Web 消息只是 静默排队
        # 用户看着像没反应——显式告知前端「排队中」让气泡有占位提示。
        if subject == "root":
            queued_coara = bind_coara if bind_coara is not None else self.root.foreground_coara
            if queued_coara.has_active_turn():
                stream.emit("turn_queued")

        # 连接级兜底通道（全局槽，只注册不注销）：精确槽被误注销时接住帧。
        end_registry = getattr(self.root, "end_registry", None)
        sender: Any = None
        # 会把这条连接的兜底一起删掉，此后该连接所有未精确命中的帧全部静默丢弃。
        _bind = bind_coara
        if _bind is None:
            try:
                _bind = self._view_coara()
            except Exception:  # noqa: BLE001 — 夹具/异常态下不阻断回合
                _bind = None
        _sess_id = str(getattr(_bind, "session_id", "") or "")
        if subject == "root" and end_registry is not None:
            if _sess_id:
                sender = self._web_end_sender(stream)
                end_registry.register("web", sender, _sess_id)
            else:
                logger.warning("web turn: session id unresolved; end channel not registered")
        reason = "complete"
        error_message: str | None = None
        try:
            async with turn(
                source,
                channel_id=conn_id,
                send_text=None,
                interaction_channel=self.interaction_channel,
            ):
                turn_coara = bind_coara if bind_coara is not None else self._view_coara()
                turn_ws_id = (
                    bind_ws_id
                    if bind_ws_id is not None
                    else (getattr(self.root, "web_view_workspace_id", None) or self.root._foreground_session_id)
                )
                agen = turn_coara.process_message(
                    text,
                    trust_level="owner",
                    show_tool_summary=True,
                    image_blocks=image_blocks,
                    source=source,
                    turn_id=turn_id,
                )
                if subject != "root":
                    # 模块主体独立于前台工作空间切换语义，直接流式消费
                    iterator = agen
                else:
                    from src.coara.turn_detach import iter_while_foreground

                    # web 独立视图（D6）：detach 判定按本端视图空间——CLI/matrix 切全局前台不再 detach web 正在看的回合。
                    def _still_web_view() -> bool:
                        current = getattr(self.root, "web_view_workspace_id", None) or self.root._foreground_session_id
                        return current == turn_ws_id

                    iterator = iter_while_foreground(
                        agen,
                        _still_web_view,
                        drain_name="web-detached-workspace-turn",
                    )
                if subject == "root" and end_registry is not None:
                    # 在此注销会让 drain 中的回合后续 chunk 查无通道纯丢弃
                    async for _ in iterator:
                        pass
                else:
                    async for chunk in iterator:
                        if not chunk.strip():
                            continue
                        stream.emit("chunk", text=chunk)
        except asyncio.CancelledError:
            reason = "interrupted"
        except Exception as exc:
            reason = "error"
            logger.exception("Chat turn error: {}", exc)
            from src.coara.turn_orchestrator import _user_facing_turn_error

            error_message = _user_facing_turn_error(exc)
            stream.emit("chunk", text=error_message)
            stream.emit("error", message=error_message)
        finally:
            # 清理显示通道（防异常路径残留）；仅当仍指向本回合 sender 才删。
            if end_registry is not None and sender is not None:
                end_registry.unregister("web", sender, _sess_id)
            # turn_end 进 buffer：前端按 seq 对账不会因 trace 竞态丢失而卡 turnActive。
            if reason == "error":
                stream.emit("turn_end", reason=reason, message=error_message or "Error: 回合失败")
            else:
                stream.emit("turn_end", reason=reason)
            stream.finish()
            # buffer 保留供重连回放；长期驻留由 _gc_finished_turns 上限回收。
            self._gc_finished_turns()

    _FINISHED_TURNS_KEEP = 20
    # standby 流上限：会话/空间切换不断产生新键，超限按插入顺序淘汰最旧的键（FIFO）
    _MAX_STANDBY_STREAMS = 8

    async def stream_leftover_web_turn(
        self,
        text: str,
        *,
        image_blocks: list[dict[str, Any]] | None = None,
        bind_coara: Any | None = None,
        bind_ws_id: str | None = None,
        subject: str = "root",
    ) -> None:
        """它端收尾时把 source=web 的 leftover 开成 web 新回合（无发起 WS）"""
        # 复用活跃连接池：无浏览器时仍落 web_views，重连可 hydrate。
        await self._stream_chat_turn(
            text,
            ws=None,  # type: ignore[arg-type]
            conn_id="leftover-web",
            image_blocks=image_blocks,
            bind_coara=bind_coara,
            bind_ws_id=bind_ws_id,
            subject=subject,
        )

    def _bind_view_store(self, workspace_dir: Path) -> None:
        """记录 web 当前视图空间（视图切换时随 trace_store 刷新）"""
        self._view_store_workspace = workspace_dir

    async def stream_awakened_turn(
        self,
        target_coara: Any,
        text: str,
        *,
        origin_source: str,
        task_id: str,
        workspace_dir: Any = None,
    ) -> None:
        """后台完成唤醒回合：把输出经 TurnStream 流回发起端浏览器"""
        turn_id = uuid.uuid4().hex
        # 视图落盘按发起空间解析，不用当前视图 persist——用户已切走空间时
        _ws_dir = workspace_dir or getattr(target_coara, "workspace_dir", None) or self.workspace_dir
        stream = TurnStream(
            turn_id,
            "web",
            "root",
            self,
            # 被本回合同 key 通道覆盖属预期（跟随最新注入端）。
            channel_id="awakened-web",
            session_id=str(getattr(target_coara, "session_id", "") or ""),
            workspace_dir=str(_ws_dir),
        )
        self._turns[turn_id] = stream
        # 唤醒回合无人「提问」：以系统注记开场，让浏览器端知道这是一条后台 结果回投而非普通对话，回放时也能还原上下文。
        stream.emit("turn_start", awakened=True, task_id=task_id, origin_source=origin_source)

        # 不再塌成 background（否则 web 心跳把本端发起的唤醒回合误判他端占用）。
        end_registry = getattr(self.root, "end_registry", None)
        _sess_id = str(getattr(target_coara, "session_id", "") or "")
        sender: Any = None
        if end_registry is not None:

            def sender(frame: dict) -> None:
                # 唯一出口：唤醒回合同样走 _emit_end_frame（与普通回合/跟话同一把尺）
                self._emit_end_frame(stream, frame)

            sender._end_channel_id = "awakened-web"  # type: ignore[attr-defined]
            end_registry.register("web", sender, _sess_id)
        reason = "complete"
        error_message: str | None = None
        try:
            async with turn(
                "web",
                channel_id="awakened-web",
                send_text=None,
                interaction_channel=self.interaction_channel,
            ):
                # 唤醒回合打标：turn_end payload 带 awakened=True，活动时钟据此 不刷新空闲钟（唤醒不代表用户在场；
                # source 已收敛三端，无法再靠它区分）。
                target_coara._awakened_turn_active = True
                agen = target_coara.process_message(
                    text,
                    trust_level="owner",
                    show_tool_summary=True,
                    # source 沿用发起端 web：段归属=web，runtime turn_source 正确， 帧路由键与上方 ("web", session)
                    # 注册通道匹配
                    source="web",
                    turn_id=turn_id,
                )
                # 纯驱动循环：正文已由 EndRegistry 路由投递（✓/✗ 工具行被 base 路由前的 ✓/✗ 前缀过滤拦截，不进 chunk
                # 通道）。
                async for _ in agen:
                    pass
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:
            reason = "error"
            logger.exception("Awakened web turn error: {}", exc)
            from src.coara.turn_orchestrator import _user_facing_turn_error

            error_message = _user_facing_turn_error(exc)
            stream.emit("chunk", text=error_message)
            stream.emit("error", message=error_message)
        finally:
            if end_registry is not None and sender is not None:
                end_registry.unregister("web", sender, _sess_id)
            if reason == "error":
                stream.emit("turn_end", reason=reason, message=error_message or "Error: 回合失败")
            else:
                stream.emit("turn_end", reason=reason)
            stream.finish()
            self._gc_finished_turns()

    @property
    def _turns(self) -> dict[str, TurnStream]:
        """惰性初始化：测试夹具经 __new__ 绕过 __init__ 时也能用。"""
        turns = self.__dict__.get("_WebServer__turns")
        if turns is None:
            turns = {}
            self.__turns = turns
        return turns

    @property
    def _standby_streams(self) -> dict[tuple[str, str], TurnStream]:
        """惰性初始化（同 _turns）：无在飞回合时的帧出口 (session, workspace) → 流。"""
        streams = self.__dict__.get("_WebServer__standby_streams")
        if streams is None:
            streams = {}
            self.__standby_streams = streams
        return streams

    def _gc_finished_turns(self) -> None:
        """回收已结束回合：只保留最近 N 个 done 的 TurnStream（供刷新回放），其余删除"""
        done = [tid for tid, s in self._turns.items() if s.done]
        excess = len(done) - self._FINISHED_TURNS_KEEP
        if excess <= 0:
            return
        for tid in done[:excess]:
            self._turns.pop(tid, None)

    _MAX_TEXT_FILE_CHARS = 100_000
    # Pillow 无法解码的图允许原始 base64 回退的大小上限（防巨串永久驻留上下文）
    _FALLBACK_RAW_IMAGE_MAX_BYTES = 5 * 1024 * 1024

    # Slash command handler

    async def _handle_flow_snapshot(self, data: dict[str, Any], ws: web.WebSocketResponse) -> None:
        """Flow 实时视图：返回指定 flow 的全量图快照（节点/边/状态）。"""
        subject = str(data.get("subject") or "root")
        if subject != "root":
            module_root = self._module_roots.get(subject)
            coord = module_root.flow_coordinator if module_root is not None else None
        else:
            coord = self.root.foreground_coara.flow_coordinator
        flow = str(data.get("flow") or "").strip()
        snap = coord.snapshot(flow) if (coord is not None and flow) else None
        await ws.send_str(
            json.dumps(
                {"type": "flow_graph_snapshot", "flow": flow, "snapshot": snap, "subject": subject},
                ensure_ascii=False,
            )
        )

    # 三端共用同一命令层（成功即静默交棒，失败才回一条错误）。
    _WEB_COMMAND_BLOCKLIST = frozenset({"exit", "quit", "login", "status", "ws", "new", "model"})

    async def _handle_command(self, data: dict[str, Any], ws: web.WebSocketResponse) -> None:
        """Execute a slash command via the unified commands service layer."""
        raw = str(data.get("text", "")).strip()
        if not raw:
            await self._send_error(ws, "Empty command")
            return
        from src.coara.commands.registry import parse_command

        parsed = parse_command(raw)
        if parsed is not None and parsed.name in self._WEB_COMMAND_BLOCKLIST:
            await self._send_error(ws, f"Web 端请用界面操作代替 /{parsed.name}（该命令在此不可用）。")
            return

        subject = str(data.get("subject") or "root")
        # 模块会话（FlowRoot 构建对话等）里的会话级命令作用在模块主体上， 而不是主会话前台实例——否则 /compact
        # 会压错会话、看起来「不起作用」。
        target_coara = None
        if subject != "root" and self._is_module_subject(subject):
            from src.coara.commands.registry import parse_command as _parse

            parsed = _parse(raw)
            if parsed is not None and parsed.name in _MODULE_SESSION_COMMANDS:
                target_coara = await self._get_module_root(subject)
        else:
            # 普通 web 会话：命令作用于 web 当前视图空间（端独立——/model 切的 是自己看的空间并绑定到该空间，不再漂到
            # CLI 前台/active_id）。
            try:
                target_coara = self._view_coara()
            except Exception:
                target_coara = None
            # 命令同样按归属办事：端带上「这条命令在哪个空间发出」时认归属，
            # 不认到达那一刻的视图指针——切空间窗口内发出的命令才不会作用到别处。
            frame_dir = str(data.get("workspace_dir") or "").strip()
            if frame_dir:
                try:
                    from src.core.coara_home import workspace_id_for

                    bound = self.root.resolve_workspace_coara(workspace_id_for(Path(frame_dir)))
                    if bound is not None:
                        target_coara = bound
                except Exception:  # noqa: BLE001 — 归属解析失败退回视图空间
                    # 同 chat 帧：解析失败是异常，留 WARNING（回退）。
                    logger.warning("command frame workspace_dir unresolved: %s", frame_dir, exc_info=True)
        result = await execute_command(
            self.root, raw, target_coara=target_coara, interaction_channel=self.interaction_channel
        )
        if result is None:
            await self._send_error(ws, "Not a command")
            return

        # 压缩成功落「已压缩」分隔线帧（09-26 口径：替代原命令卡）：刷新回放与实时一致。
        view_seq = 0
        if (
            parsed is not None
            and parsed.name in _MODULE_SESSION_COMMANDS
            and getattr(result, "data", None)
            and result.data.get("compressed")
        ):
            view_seq = self._persist_timeline_divider("已压缩", subject=subject) or 0

        # /model 切换模型：往空间线落分隔标记（刷新回放与实时一致），并同步模块主体。
        if (
            raw.lower().startswith("/model")
            and isinstance(result.data, dict)
            and result.data.get("provider")
            and not result.data.get("error")
        ):
            if subject == "root":
                _prov = str(result.data.get("provider") or "").strip()
                _mdl = str(result.data.get("model") or "").strip()
                _key = f"{_prov}·{_mdl}" if _prov and _mdl else (_mdl or _prov)
                if _key:
                    # 模型切换不再落分隔线：顶栏模型名就是可见结果，线上多一行 只会添噪。web
                    # 端唯一的分隔线来自「新会话」。
                    logger.debug("model switched in web view: %s", _key)
            if self._module_roots:
                for module_root in list(self._module_roots.values()):
                    try:
                        module_root.switch_llm(
                            str(result.data["provider"]),
                            str(result.data.get("model") or "") or None,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("Failed to sync module root LLM after /model")

        # command_result 是本连接的单播回执：必须盖 workspace_dir / session_id， 否则端上 _frameInBoundary
        # 会把结果整帧丢掉（pendingCommand 永不清除， /compact 看起来像「卡住且没执行」）。
        target = target_coara if target_coara is not None else getattr(self.root, "foreground_coara", None)
        try:
            target = target or self._view_coara()
        except Exception:  # noqa: BLE001
            target = getattr(self.root, "foreground_coara", None)
        cmd_ws = str(getattr(target, "workspace_dir", "") or getattr(self, "workspace_dir", "") or "")
        cmd_sid = str(getattr(target, "session_id", "") or "")
        frame: dict[str, Any] = {
            "type": "command_result",
            "subject": subject,
            "result": {
                "output": result.output,
                "action": result.action,
                "data": result.data,
                "exit_session": result.exit_session,
            },
        }
        if cmd_ws:
            frame["workspace_dir"] = cmd_ws
        if cmd_sid:
            frame["session_id"] = cmd_sid
        if view_seq > 0:
            frame["view_seq"] = int(view_seq)
        await ws.send_str(json.dumps(frame, ensure_ascii=False))

    def _persist_timeline_divider(self, label: str, *, subject: str = "root") -> int | None:
        """往空间这条线落一个分隔标记帧（新会话/压缩成功时调用），返回 view_seq"""
        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_view_store 可能不存在
        view_store = getattr(self, "_view_store", None)
        label = (label or "").strip()
        if view_store is None or not label:
            return None
        try:
            # 归属本端视图空间，不读全局前台（单例指针）：分隔帧要落在你正在看的 那个空间的线上，会话键也用它自己的。
            target: Any
            try:
                target = self.root.resolve_web_view_coara()
            except Exception:  # noqa: BLE001
                target = getattr(self.root, "foreground_coara", None)
            sess_id = str(getattr(target, "session_id", "") or "")
            from src.ui.view_recorder import record_view_frame

            return record_view_frame(
                {
                    "kind": "divider",
                    "turn_id": "",
                    "source": "web",
                    "subject": subject,
                    "session_id": sess_id,
                    "workspace_dir": str(self.workspace_dir or ""),
                    "payload": {"label": label},
                },
                coara_home=self.coara_home,
            )
        except Exception:  # noqa: BLE001 — 视图落盘故障不影响主流程
            logger.exception("Failed to persist timeline divider to web view")
            return None

    def _persist_command_result_view(self, coara: Any | None, subject: str, output: str) -> int:
        """把会话状态变更命令的结果写入 web 视图存储（刷新后与实时一致）"""
        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_view_store 可能不存在
        view_store = getattr(self, "_view_store", None)
        target = coara if coara is not None else getattr(self.root, "foreground_coara", None)
        sess_id = str(getattr(target, "session_id", "") or "")
        ws_dir = getattr(target, "workspace_dir", None) or getattr(self, "workspace_dir", None)
        if view_store is None or not sess_id or not output or ws_dir is None:
            return 0
        try:
            import uuid

            from src.ui.view_recorder import record_view_frame

            turn_id = uuid.uuid4().hex
            base = {
                "source": "web",
                "subject": subject,
                "session_id": sess_id,
                "workspace_dir": str(ws_dir or ""),
            }
            record_view_frame({**base, "kind": "turn_start", "turn_id": turn_id}, coara_home=self.coara_home)
            return int(
                record_view_frame(
                    {
                        **base,
                        "kind": "chunk",
                        "turn_id": turn_id,
                        "payload": {"text": output, "is_command_result": True},
                    },
                    coara_home=self.coara_home,
                )
                or 0
            )
        except Exception:  # noqa: BLE001 — 视图落盘故障不影响命令本身
            logger.exception("Failed to persist command result to web view")
            return 0

    # Trace event broadcast (EventBus → WS) — 实现见 src/ui/trace_broadcast.py

    # File system REST endpoints

    # Updates inbox REST endpoints（消息中心）

    async def _handle_trace_events(self, request: web.Request) -> web.Response:
        """Return recent tool-activity events for the Web StatusSidebar hydrate"""
        self._check_token(request)
        try:
            limit = max(1, min(200, int(request.query.get("limit", "80"))))
        except ValueError:
            limit = 80
        kinds = str(request.query.get("kinds") or "tool").strip().lower()

        # Align with live WS allow-list; tool mode is what the sidebar needs.
        if kinds in {"tool", "sidebar", "activity"}:
            relevant = {
                "user_message",
                "llm_turn_start",
                "tool_start",
                "tool_call",
                "tool_result",
                "tool_complete",
                "turn_end",
            }
        else:
            relevant = {
                "tool_start",
                "tool_call",
                "tool_result",
                "tool_complete",
                "turn_start",
                "turn_end",
                "user_message",
                "chat_chunk",
                "chat_turn_retracted",
                "llm_turn_start",
                "error",
                "session_auto_new",
            }

        # 尾部倒读：只需最近 limit 条相关事件，不全量加载整个 trace 文件 （大文件下全量读+解析每次数百 ms 到数秒，
        # 是切空间卡顿的主因）。
        def _match(row: dict[str, Any]) -> bool:
            event_type = row.get("event_type", "")
            if event_type not in relevant:
                return False
            payload = row.get("payload") or {}
            # 无 source 的旧事件保留（兼容期，宁多勿丢）。
            if event_type.startswith("tool_"):
                src = str(payload.get("source") or row.get("source") or "").strip()
                if src and src != "web":
                    return False
            return True

        events = await asyncio.to_thread(self.trace_store.load_recent_events_tail, _match, limit)

        mapped: list[dict[str, Any]] = []
        for row in events:
            event_type = row.get("event_type", "")
            payload = row.get("payload") or {}
            entry: dict[str, Any] = {"type": event_type}
            ts = row.get("timestamp")
            if ts:
                entry["timestamp"] = ts
            for key in (
                "tool",
                "call_id",
                "summary",
                "ok",
                "args",
                "turn_id",
                "reason",
                "message",
                "source",
                "text",
                "content",
                "origin_scope",
                "session_id",
                "subject",
            ):
                if key in row and row[key] is not None:
                    entry[key] = row[key]
            for key, val in payload.items():
                if key not in entry and val is not None:
                    entry[key] = val
            self._normalize_tool_ws_fields(event_type, entry, payload)
            mapped.append(entry)
        return web.json_response({"events": mapped, "total": len(mapped)})

    async def _handle_tool_call_lookup(self, request: web.Request) -> web.Response:
        """GET /api/v1/tool-call?call_id=<id> — 详情页内存缓冲查不到时的服务端回退"""
        self._check_token(request)
        call_id = (request.query.get("call_id") or "").strip()
        if not call_id:
            return web.json_response({"error": "missing call_id"}, status=400)
        rows = await asyncio.to_thread(self.trace_store.find_tool_call_events, call_id)
        activity = self._aggregate_tool_call_events(call_id, rows)
        if activity is None:
            return web.json_response({"error": "tool call not found"}, status=404)
        return web.json_response({"activity": activity})

    @staticmethod
    def _aggregate_tool_call_events(call_id: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """把同一 call_id 的落盘 trace 事件（时间升序）聚合为 ToolActivity 同构 dict"""
        if not rows:
            return None
        activity: dict[str, Any] | None = None

        def _base(payload: dict[str, Any], ts: str, *, done: bool) -> dict[str, Any]:
            tool = str(payload.get("tool_name") or payload.get("tool") or "(tool)")
            entry: dict[str, Any] = {
                "call_id": call_id,
                "tool": tool,
                "done": done,
                "timestamp": ts,
                "turn_id": str(payload.get("turn_id") or ""),
            }
            args = payload.get("arguments")
            if isinstance(args, dict) and args:
                entry["args"] = args
            return entry

        for row in rows:
            event_type = row.get("event_type", "")
            payload = row.get("payload") or {}
            ts = str(row.get("timestamp") or "")
            if event_type == "tool_start":
                if activity is None:
                    activity = _base(payload, ts, done=False)
                elif not activity.get("args"):
                    args = payload.get("arguments")
                    if isinstance(args, dict) and args:
                        activity["args"] = args
                continue
            if event_type == "tool_complete":
                if activity is None:
                    activity = _base(payload, ts, done=True)
                diff_lines = payload.get("diff_lines")
                if diff_lines:
                    activity["diff_lines"] = diff_lines
                output = payload.get("tool_output")
                if output:
                    activity["tool_output"] = output
                    activity["tool_output_truncated"] = bool(payload.get("tool_output_truncated"))
                    ref = str(payload.get("tool_output_ref") or "")
                    if ref:
                        activity["tool_output_ref"] = ref
                else:
                    ref = str(payload.get("tool_output_ref") or "")
                    if ref:
                        activity["tool_output_ref"] = ref
                        activity["tool_output_truncated"] = bool(payload.get("tool_output_truncated"))
                duration = payload.get("duration_ms")
                if isinstance(duration, (int, float)):
                    activity["duration_ms"] = duration
                if payload.get("is_error") is not None:
                    activity["is_error"] = bool(payload.get("is_error"))
                tool_name = str(payload.get("tool_name") or "")
                if tool_name:
                    activity["tool"] = tool_name
                activity["done"] = True
                continue
            if event_type == "tool_call":
                if activity is None:
                    activity = _base(payload, ts, done=True)
                else:
                    activity["done"] = True
                    args = payload.get("arguments")
                    if isinstance(args, dict) and args:
                        activity["args"] = args
                if "tool_output" not in activity and payload.get("tool_output"):
                    activity["tool_output"] = payload.get("tool_output")
                ref = str(payload.get("output_ref") or "")
                if ref and "tool_output_ref" not in activity:
                    activity["tool_output_ref"] = ref
                if "is_error" not in activity and payload.get("is_error") is not None:
                    activity["is_error"] = bool(payload.get("is_error"))
                tool_name = str(payload.get("tool_name") or "")
                if tool_name:
                    activity["tool"] = tool_name
                continue
            if event_type == "tool_result":
                if activity is None:
                    activity = _base(payload, ts, done=True)
                summary = str(payload.get("summary") or "")
                if summary:
                    activity["summary"] = summary
                if payload.get("ok") is not None:
                    activity["ok"] = bool(payload.get("ok"))
                tool_name = str(payload.get("tool") or "")
                if tool_name:
                    activity["tool"] = tool_name
                activity["done"] = True
                continue
        return activity

    def _error_frame(self, message: str, **extra: Any) -> dict[str, Any]:
        """错误帧统一形状：一律带当前 runtime 的归属（session_id / workspace_dir）"""
        frame: dict[str, Any] = {"type": "error", "message": message}
        try:
            runtime = self._current_runtime() or {}
        except Exception:  # noqa: BLE001 — 台账异常也必须能把错误发出去
            runtime = {}
        session_id = str(runtime.get("session_id") or "")
        workspace_dir = str(runtime.get("workspace_dir") or "")
        if session_id:
            frame["session_id"] = session_id
        if workspace_dir:
            frame["workspace_dir"] = workspace_dir
        frame.update(extra)
        return frame

    async def _send_error(self, ws: web.WebSocketResponse, message: str, **extra: Any) -> None:
        with contextlib.suppress(ConnectionResetError, ClientConnectionResetError, RuntimeError):
            await ws.send_str(json.dumps(self._error_frame(message, **extra), ensure_ascii=False))

    def _build_state_snapshot(self) -> dict[str, Any]:
        """Build the initial WS state frame — runtime only. 内容都有自己的权威来源（消息线由视图带快照给，工具活动由
        trace 接口给）， 不在这个帧里重复投喂"""
        runtime = self._current_runtime()
        self._last_runtime_snapshot = runtime
        return {"runtime": runtime}

    async def _heartbeat_loop(self) -> None:
        """Periodically flush trace store and push lightweight runtime status"""
        try:
            while True:
                await asyncio.sleep(5)
                await self.trace_store.heartbeat()
                # Push lightweight runtime update only — no JSONL reads.
                if self.registry.has_active():
                    runtime = self._build_lightweight_runtime()
                    if runtime is not None:
                        await self.registry.send_to_active({"type": "state", "data": {"runtime": runtime}})
                # 后台任务指示：服务端是任务表唯一事实源，attach 客户端本地摸不到
                await self._push_background_tasks_to_attach()
                # 回合运行态（running + turn_source）：attach 端他端运行指示的唯一来源
                await self._push_attach_runtime()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception(f"Heartbeat error: {exc}")

    async def _push_background_tasks_to_attach(self) -> None:
        """把各 pin 空间的后台任务摘要推给 attach 连接（变化才发，无变化省帧）。"""
        registry = getattr(self, "attach_registry", None)
        if registry is None or not registry.has_connections():
            return
        from src.cli.background_tasks_display import snapshot_running_tasks

        # 缓存：上一帧各连接的摘要指纹，没变不重发
        last = getattr(self, "_attach_bg_last", None)
        if last is None:
            last = {}
            self._attach_bg_last = last
        current_conns = set()
        for conn_id, workspace_id in registry.workspace_targets():
            current_conns.add(conn_id)
            session = self.root._sessions.get(workspace_id)
            coara = getattr(session, "coara", None) if session is not None else None
            if coara is None:
                continue
            summary = snapshot_running_tasks(coara, origin_end="cli-attached").summary()
            if last.get(conn_id) == summary:
                continue
            last[conn_id] = summary
            await registry.send_to(conn_id, {"type": "background_tasks", **summary})
        # 清掉已断开连接的缓存
        for stale in [cid for cid in last if cid not in current_conns]:
            last.pop(stale, None)

    async def _push_attach_runtime(self) -> None:
        """把各 pin 空间的回合运行态（running + turn_source）推给 attach 连接"""
        registry = getattr(self, "attach_registry", None)
        if registry is None or not registry.has_connections():
            return
        last = getattr(self, "_attach_runtime_last", None)
        if last is None:
            last = {}
            self._attach_runtime_last = last
        current_conns = set()
        for conn_id, workspace_id in registry.workspace_targets():
            current_conns.add(conn_id)
            session = self.root._sessions.get(workspace_id)
            coara = getattr(session, "coara", None) if session is not None else None
            if coara is None:
                continue
            # 发起端——与 web _current_runtime 同一取值口径（finally 清段与新回合 开首段之间 running 仍为真，
            # 取不到段名会误报「无归属」）。
            payload = {
                "running": bool(coara.has_active_turn()),
                "turn_source": str(getattr(getattr(coara, "_segments", None), "source", "") or "")
                or str(getattr(coara, "_active_turn_source", "") or ""),
            }
            if last.get(conn_id) == payload:
                continue
            last[conn_id] = payload
            await registry.send_to(conn_id, {"type": "runtime", **payload})
        for stale in [cid for cid in last if cid not in current_conns]:
            last.pop(stale, None)

    def _view_coara(self) -> Any:
        """web 视图空间 coara（D6）；未独立绑定（视图 id 非真 str）时回退前台。"""
        pinned = getattr(self.root, "pinned_view_id", None)
        view_id = pinned("web") if callable(pinned) else None
        if isinstance(view_id, str) and view_id:
            resolver = getattr(self.root, "resolve_web_view_coara", None)
            if callable(resolver):
                try:
                    return resolver()
                except Exception:
                    logger.debug("web view coara resolver failed, falling back to foreground", exc_info=True)
        return self.root.foreground_coara

    def _current_runtime(self) -> dict[str, Any]:
        """web 视图 session runtime fields for WS state / heartbeat（D6 按视图出）。"""
        fg = self._view_coara()
        workspace_name = ""
        if self.root.workspace_manager is not None:
            from src.workspace.catalog import resolve_foreground_active_name

            workspace_name = resolve_foreground_active_name(fg, self.root.workspace_manager) or ""
        from src.coara.background_activity import active_delegation_rows

        runtime = {
            "session_id": fg.session_id,
            "status": fg.status.value,
            "provider": fg.provider_name,
            "model": fg.model_name,
            "running": fg.has_active_turn(),
            # 占用归属（跟话注入后段切到跟话端）；回合外段已清空。
            "turn_source": str(getattr(getattr(fg, "_segments", None), "source", "") or "")
            or str(getattr(fg, "_active_turn_source", "") or ""),
            # 回合开始墙钟时刻（epoch 秒）：刷新/重连后 spinner 靠它接续计时
            "turn_started_at": float(getattr(getattr(fg, "_active_turn", None), "started_at", 0.0) or 0.0),
            "alive": True,
            "workspace_name": workspace_name,
            "workspace_dir": str(fg.workspace_dir),
            # 后台任务计数：分段原则——只算本视图空间且发起端为 web 的任务， 他端发起的任务不出现在 web
            # spinner（与消息/工具帧同一把尺）
            "background_tasks": self._web_view_background_task_count(fg),
            # 「回合已结束、子智能体还在跑」窗口里工具行首圆点静止。
            "active_delegations": active_delegation_rows(str(fg.workspace_dir)),
        }
        runtime.update(self._foreground_context_usage())
        return runtime

    @staticmethod
    def _web_view_background_task_count(fg: Any) -> int:
        """web 视图的后台任务计数：本空间 + 发起端为 web 族（出站当下收窄）"""
        from src.cli.background_tasks_display import snapshot_running_tasks

        return snapshot_running_tasks(fg, origin_end="web").count

    def _foreground_context_usage(self) -> dict[str, Any]:
        """Same accounting as CLI bottom toolbar (estimate flagged with context_estimated)."""
        from src.llm.usage import total_prompt_tokens

        fg = self._view_coara()
        used = 0
        estimated = False
        hit_ratio: float | None = None
        snap = getattr(fg, "_llm_usage_snapshot", None)
        session_cost = 0.0
        session_prompt = 0
        session_output = 0
        if snap is not None:
            if getattr(snap, "has_reported_input", False):
                usage = getattr(snap, "usage", None) or {}
                used = total_prompt_tokens(usage) + int(usage.get("output_tokens") or 0)
                estimated = bool(getattr(snap, "estimated", False))
            try:
                hit_ratio = snap.cache_hit_ratio
            except Exception:
                hit_ratio = None
            session_cost = float(getattr(snap, "cumulative_cost", 0.0) or 0.0)
            session_prompt = int(getattr(snap, "cumulative_prompt_tokens", 0) or 0)
            session_output = int(getattr(snap, "cumulative_output_tokens", 0) or 0)
        ctx_window = 0
        try:
            provider_obj = getattr(fg, "provider", None)
            if provider_obj is not None:
                ctx_window = int(provider_obj.get_context_window(getattr(fg, "model_name", None)) or 0)
        except Exception:
            ctx_window = 0
        return {
            "context_used_tokens": used,
            "context_window_tokens": ctx_window,
            "context_cache_hit_ratio": hit_ratio,
            "context_estimated": estimated,
            "session_cost": session_cost,
            "session_prompt_tokens": session_prompt,
            "session_output_tokens": session_output,
        }

    def _build_lightweight_runtime(self) -> dict[str, Any] | None:
        """Build a small runtime dict without reading any JSONL files"""
        runtime = self._current_runtime()
        # Skip push if nothing changed since last heartbeat.
        if self._last_runtime_snapshot == runtime:
            return None
        self._last_runtime_snapshot = runtime
        return runtime


# Entrypoint


# 本进程内活跃 WebServer（托盘 / open_or_focus 同进程直调）。
_ACTIVE_WEB_SERVER: WebServer | None = None


# 内核化后 CLI 是平等 attach 客户端：渲染经 RootShim + 事件通道，无服务端镜像。


def _request_server_open(*, host: str, port: int, token: str, path: str = "", timeout_s: float = 6.0) -> bool:
    """请内核执行打开/唤起决策（``/api/ui/open``）。成功返回 True。"""
    if not token:
        return False
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode({"token": token, **({"path": path} if path else {})})
    url = f"http://{host}:{port}/api/ui/open?{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            json.loads(resp.read().decode("utf-8"))
        return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return False


def open_or_focus_web_ui(
    *,
    url: str = "",
    path: str = "",
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str = "",
) -> str:
    """打开或唤起 Web UI，返回入口 URL"""
    server = _ACTIVE_WEB_SERVER
    if server is not None:
        return server.open_window(path)
    route = path if (not path or path.startswith("/")) else f"/{path}"
    # 与 WebServer.build_url 同口径：路由已含 ? 时用 & 续接 token
    sep = "&" if "?" in route else "?"
    entry = f"http://{host}:{port}{route}" + (f"{sep}token={token}" if token else "")
    if _request_server_open(host=host, port=port, token=token, path=path):
        _try_raise_coara_browser_windows()
        return entry
    target = url.strip() or entry
    _open_web_ui_window(target)
    _try_raise_coara_browser_windows()
    return target


def _open_web_ui_window(url: str) -> None:
    """Open the Web UI in the system default browser (same as clicking the URL in terminal)"""
    try:
        webbrowser.open(url, new=0, autoraise=True)
    except Exception as exc:
        logger.debug(f"Failed to open browser: {exc}")


def _try_raise_coara_browser_windows() -> bool:
    """Best-effort: 把标题含 coara 的浏览器窗口抬到前台（Windows）。

    返回是否找到并尝试抬起过至少一个窗口。找不到时（常见：coara 在后台标签，
    窗口标题是别的页）调用方应改走开新标签路径，否则点托盘会没反应。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 就抛 "int too long to convert"。
        sw_restore = 9
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM,
        ]
        user32.EnumWindows.restype = wintypes.BOOL
        browser_markers = (
            "chrome",
            "edge",
            "firefox",
            "brave",
            "opera",
            "chromium",
            "msedge",
            "safari",
        )
        targets: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip().lower()
            if "coara" not in title:
                return True
            # Prefer browser chrome titles; also accept bare page title "coara".
            if title == "coara" or any(m in title for m in browser_markers):
                targets.append(int(hwnd))
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in targets:
            # 否则对最大化窗口调 SW_RESTORE 会把它还原成普通大小（看着像自动缩小）。
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, sw_restore)
            user32.SetForegroundWindow(hwnd)
        return bool(targets)
    except Exception as exc:
        logger.debug(f"Failed to raise coara browser window: {exc}")
        return False


async def _should_open_web_ui_window(server: WebServer, *, probe_s: float = 5.0) -> bool:
    """自动识别：已有标签就不再自动开新窗"""
    deadline = time.monotonic() + probe_s
    while time.monotonic() < deadline:
        if server.registry.has_active():
            return False
        await asyncio.sleep(0.2)
    try:
        presence = web_tab_presence.read_presence(
            server.workspace_dir,
            coara_home=getattr(server, "coara_home", None),
        )
        if web_tab_presence.is_tab_fresh(presence):
            logger.info("Web UI 标签仍在（presence 未过期），跳过自动打开")
            return False
    except Exception as exc:  # noqa: BLE001 — 判据故障不阻塞启动，退回旧行为（宁可开也不黑屏）
        logger.debug(f"tab presence read failed: {exc}")
    return True


async def _wait_until_ready(host: str, port: int, *, timeout_s: float = 5.0, retries: int = 3) -> bool:
    """Probe ``http://{host}:{port}/`` until the server responds or we give up"""
    from aiohttp import ClientSession, ClientTimeout

    delay = 0.5
    for attempt in range(1, retries + 1):
        try:
            async with (
                ClientSession(timeout=ClientTimeout(total=timeout_s), trust_env=False) as session,
                session.get(f"http://{host}:{port}/") as resp,
            ):
                if resp.status < 500:
                    return True
        except Exception as exc:
            logger.debug(f"Web UI readiness probe attempt {attempt}/{retries} failed: {exc}")
        if attempt < retries:
            await asyncio.sleep(delay)
    return False


def _resolve_banner_provider_model(
    root: Any,
    provider: str | None,
    model: str | None,
) -> tuple[str, str]:
    """横幅显示的前台会话实际模型：优先 foreground_coara，退回传入参数"""
    fg_provider = ""
    fg_model = ""
    try:
        fg = root.foreground_coara
        fg_provider = getattr(fg, "provider_name", "") or ""
        fg_model = getattr(fg, "model_name", "") or ""
    except Exception:
        logger.debug("startup banner: foreground provider/model probe failed, using bootstrap defaults")
    return fg_provider or provider or "", fg_model or model or ""


async def run_web_server(
    *,
    workspace: Path | str,
    port: int = 8080,
    provider: str | None = None,
    model: str | None = None,
    workspace_alias: str | None = None,
    auto_open: bool = True,
    root: Any = None,
) -> None:
    """Start the embedded web server"""
    if config_manager._config is None:
        await config_manager.load()
    coara_home = config_manager.config.coara_home

    if root is None:
        from src.coara.root import create_root_coara
        from src.llm.registry import initialize_providers

        await initialize_providers(config_manager)
        resolved_provider = provider or config_manager.config.default_provider or None
        resolved_model = model or config_manager.config.default_model or None
        root = await create_root_coara(
            workspace_dir=Path(workspace),
            provider_name=resolved_provider,
            model=resolved_model,
            workspace_alias=workspace_alias,
        )
    else:
        resolved_provider, resolved_model = _resolve_banner_provider_model(root, provider, model)

    server = WebServer(
        root,
        workspace_dir=Path(workspace),
        coara_home=coara_home,
        port=port,
        skip_trace_persistence=(root is not None),
    )
    from src.cli.theme import fg as theme_fg

    await server.start()

    # 与底部 spinner 状态栏同色；OSC-8 可点击，显式关掉下划线（终端默认真线）
    from rich.style import Style
    from rich.text import Text

    provider_c = theme_fg("text.user")
    webui_url = server.build_url()
    banner = Text.assemble(
        ("Web UI", Style(color=provider_c, link=webui_url, underline=False)),
        (f" 打开 coara Web UI（端口 {server.port}）", "dim"),
    )
    console.print(banner)

    if auto_open:
        ready = await _wait_until_ready(server.host, server.port)
        if not ready:
            logger.warning("Web UI 就绪探测未通过，仍照常打开标签")
        if await _should_open_web_ui_window(server):
            server.open_window()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        # Flush any remaining batched trace events.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(), timeout=8.0)
