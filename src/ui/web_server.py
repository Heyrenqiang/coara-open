"""Embedded web server — Root lives inside the aiohttp process.

This is the production-grade replacement for the dual-process
(CLI + standalone dashboard) model. Instead of CLI pushing trace events to a
separate read-only dashboard process, Root runs directly inside the web
server process. The browser drives Root through a single WebSocket with
typed JSON messages; trace observation reuses the same in-process TraceStore.

Architecture::

    ┌─ coara --web (single process) ──────────────────────┐
    │  RootCoara (embedded)                                │
    │   ├─ EventBus / TraceStore (direct subscribe)        │
    │   ├─ WebRemoteInteractionChannel                     │
    │   └─ Commands Service (src.coara.commands)           │
    │                                                       │
    │  aiohttp server                                       │
    │   ├─ GET /          → SPA index.html                 │
    │   ├─ /static/*      → Vite build output              │
    │   ├─ /ws            → typed WS (chat/cmd/interaction) │
    │   └─ /api/*         → REST (state/trace/config/...)   │
    └───────────────────────────────────────────────────────┘

REST 域处理器按主题拆在 ``src/ui/handlers/``（mixin 组合进 ``WebServer``）。

WS protocol (single connection, messages routed by ``type``):

    Client → Server:
      {"type":"chat","text":"...","image_refs":["..."],"file_refs":["..."]}
      {"type":"interrupt"}
      {"type":"command","text":"/new"}  (raw slash command string)
      {"type":"approval_reply","approval_id":"...","approved":true}

    Server → Client:
      {"type":"turn_start","turn_id":"..."}
      {"type":"chunk","text":"..."}
      {"type":"tool_start","tool":"...","call_id":"...","args":{...}}
      {"type":"tool_call","tool":"...","args":{...},"call_id":"..."}
      {"type":"tool_result","call_id":"...","summary":"...","ok":true}
      {"type":"turn_end","turn_id":"...","reason":"complete"}
      {"type":"command_result","result":{...}}
      {"type":"approval_request","approval_id":"...","question":"...","options":[...]}
      {"type":"error","message":"..."}
      {"type":"state","data":{"runtime":{...}}}  (lightweight runtime update)
      {"type":"trace_batch","events":[...]}  (batched trace events, flushed every 100ms)
"""

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


# App key for storing the server instance on the aiohttp Application.
WEB_SERVER_APP_KEY = web.AppKey("web_server", "WebServer")

# 模块会话（FlowRoot 构建对话等）里支持作用在模块主体上的会话级命令；
# 其余命令维持原语义（作用在主会话前台实例）。
_MODULE_SESSION_COMMANDS = frozenset({"compact"})


def _parent_tool_call_kwargs(frame: dict[str, Any]) -> dict[str, str]:
    """子智能体帧的父工具行标识（``parent_tool_call_id``）；主会话帧不添字段。

    端上按它把子智能体产生的 diff / 工具行折进发起那条 delegate 工具行，
    而不是当主会话正文渲染。空值一律不带，避免给每个主会话帧添噪声字段。
    """
    parent_tool_call_id = str(frame.get("parent_tool_call_id") or "")
    return {"parent_tool_call_id": parent_tool_call_id} if parent_tool_call_id else {}


def _diff_frame_kwargs(frame: dict[str, Any]) -> dict[str, str]:
    """diff 帧的归属字段：父工具行（子智能体产）+ 产生它的工具调用 id。

    ``tool_call_id`` 与同一工具 ``tool`` 帧上的值相同——端上据此把 diff 精确挂到
    那次工具调用之后（相邻关系不可靠：中间可能夹正文，或同一回合有多个工具）。
    非空才带，主会话 diff 也因此拿到这个 id。
    """
    return {
        key: str(frame.get(key))
        for key in ("parent_tool_call_id", "tool_call_id")
        if str(frame.get(key) or "")
    }


class WebServer(*HANDLER_MIXINS):
    """Embedded web server that holds RootCoara and serves the SPA + WS + REST.

    The REST API handlers come from :class:`~src.ui.dashboard_handlers.DashboardRestHandlers`,
    registered directly on this server's aiohttp app and operating on the
    shared in-process TraceStore. Root is created in-process and driven
    directly via WebSocket commands.
    """

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
        # web 独立视图（D6）：浏览器端看哪个空间，与全局前台解耦。缺省跟随前台；
        # 经 set_web_view_workspace 切视图后，聊天/REST/心跳都读视图空间。
        # trace_store 按视图经 trace_persistence.store_for(view_dir) 取（读路径）。
        self.host = host
        self.port = port
        self.skip_trace_persistence = skip_trace_persistence
        # When True, the server does NOT own the RootCoara lifecycle: the caller
        # (the kernel daemon / tray host) is responsible for root.shutdown().
        # When False, the server owns the root and shuts it down in stop().
        self._owns_root = not skip_trace_persistence

        # Trace store — in-process, directly subscribed to root.event_bus.
        self.trace_store = TraceStore(workspace_dir, coara_home=coara_home)
        self._trace_persistence_sub: Subscription | None = None
        self.auth_token = load_or_create_dashboard_token(workspace_dir, coara_home)

        # WS connection registry + interaction channel.
        self.registry = WebSocketRegistry()
        # 工作空间↔端占用唯一事实源（方案 D6）：attach 占用与后续 web/matrix/CLI
        # 视图占用共享同一张表。attach 注册表只管连接生命周期，占用经 occupancy。
        from src.coara.workspace_occupancy import WorkspaceOccupancy

        self.workspace_occupancy = WorkspaceOccupancy()
        # 外挂 CLI（coara attach）专用：多连接并存。与 webui 单活跃 registry
        # 完全独立，互不顶替；空间占用经共享 occupancy 表。
        self.attach_registry = AttachRegistry(self.workspace_occupancy)
        self.interaction_channel = WebRemoteInteractionChannel(self.registry)
        # attach 端专属审批通道：按 conn_id 定向，替代 attach 回合此前复用
        # 浏览器 registry 的错投路径（无浏览器=静默拒/有浏览器=弹错端）。
        from src.ui.attach_interaction_channel import AttachRemoteInteractionChannel

        self.attach_interaction_channel = AttachRemoteInteractionChannel(self)
        # 断连只标记不取消（send_to_active 失败自动注销等非 finally 路径同样）：
        # pending prompt 挂起保留，重连后 redeliver_pending 重发。
        self.registry.on_disconnect = self.interaction_channel.mark_connection_disconnected

        # Back-reference on root so tools (save_draft / manage) can push WS
        # navigation messages to the browser without a global registry.
        root._web_server = self

        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._subscriptions: list[Any] = []
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Trace event batching: collect events and flush every 100ms to avoid
        # flooding the WS with hundreds of create_task calls during streaming.
        self._trace_batch: list[dict[str, Any]] = []
        self._trace_batch_lock = asyncio.Lock()
        self._trace_flush_task: asyncio.Task[None] | None = None

        # Cached runtime state for lightweight heartbeat (avoids reading
        # entire JSONL files every 2 seconds).
        self._last_runtime_snapshot: dict[str, Any] | None = None

        # Chat turn tasks per WS connection — tracked so we can cancel them
        # when the WS disconnects. Without this, an orphaned turn keeps
        # holding _process_lock and the next message from a new connection
        # would queue forever.
        self._chat_tasks: dict[str, set[asyncio.Task]] = {}
        # attach 跟话通道跟踪：conn_id → [(session_id, sender)]。跟话 sender
        # 只注册不随回合注销（它不属于单个回合），改由连接断开时统一注销，
        # 防旧闭包霸占 (cli-attached, session) 键顶掉后续回合 sender。
        self._followup_senders: dict[str, list[tuple[str, Any]]] = {}

        # 回合流注册表：回合与 WS 连接解耦的单一事实源。
        # 回合一经创建即与发起连接无关，输出进 TurnStream.buffer 并广播给
        # 当前活跃连接；刷新/断连只是退订，回合照跑，重连回放 buffer 接续。
        # （经 __new__ 构造的测试夹具不跑 __init__，由 _turns property 惰性兜底。）
        self.__turns: dict[str, TurnStream] | None = None
        # standby 流：无在飞回合时的帧出口（落带 + 带 view_seq 的广播）。
        # 不挂 task、不进 __turns（不参与回合收尾注销）。
        self.__standby_streams: dict[tuple[str, str], TurnStream] | None = None

        # Per-session turn-chain tails. Every full-turn _handle_chat registers
        # its completion future here; image messages (which cannot ride the
        # str-only continuation queue) await the previous tail so they run
        # after the current turn AND its drained leftovers — FIFO instead of
        # jumping the _process_lock queue ahead of earlier text continuations.
        self._turn_tails: dict[str, asyncio.Future[None]] = {}

        # 模块级独立会话主体缓存（key=subject，如 "flow" 工作流构建对话）。
        # 惰性创建，全局常驻（不随工作空间切换重建）；module_registry 驱动。
        self._module_roots: dict[str, Any] = {}
        self._module_roots_lock = asyncio.Lock()

        # 工作台/编辑器当前打开的草案 id（前端经 /api/workflow-active-draft 上报）：
        # 编排写穿未绑定草案的 flow 时复用它，不新开草案
        self.active_workflow_draft_id: str | None = None

        # Fire-and-forget background tasks (slash-command handler, state
        # pushes from sync EventBus callbacks) — tracked so stop() can
        # cancel them instead of leaving them dangling.
        self._bg_tasks: set[asyncio.Task[Any]] = set()

        # Web outbound file delivery (send_file → browser). Wired in start().
        self._web_file_bridge: Any | None = None

        # web 会话视图存储：web 聊天区的服务端唯一数据源（实时帧与刷新恢复
        # 同读它）。bind_workspace 惰性重建，指向当前视图空间。
        from src.ui.view_recorder import shared_view_store

        # 与内核录制器共用同一份存储实例：落带与快照读的是同一条线。
        self._view_store = shared_view_store()
        self._view_store_workspace: Path | None = None
        self._bind_view_store(workspace_dir)
        # web 跟话参与的它端回合在 web 视图已开过起点（(session_id, turn_id)）：
        # 落盘需保证「刷新与实时一致」——回合起点、跟话 user 消息、后续 chunk/diff
        # 都写入视图存储，刷新后不缺失。
        self._web_followup_view_turns: set[tuple[str, str]] = set()

        # 无人接收的帧兜底落带：内核只持抽象回调，落带实现在这里注入。端在接的帧
        # 由该端显示流落带并回填 view_seq；没人接的帧必须由这条兜底进带，否则
        # 「没有端在听」就等于丢数据。
        _registry = getattr(self.root, "end_registry", None)
        if _registry is not None and hasattr(_registry, "set_tape_sink"):
            from src.ui.view_recorder import record_view_frame

            _registry.set_tape_sink(
                lambda frame: record_view_frame(frame, coara_home=getattr(self, "coara_home", None))
            )

    def _has_active_web_turn_stream(self, session_id: str) -> bool:
        """True when a web-originated TurnStream for this session is still in flight.

        唤醒回合流（channel_id="awakened-web"）不算：它的正文经 background 通道
        送入本流，从未注册 ("web", session) 通道。把它算进去会让 web 跟话跳过
        通道注册，段切到 web 后 chunk 查无通道静默丢弃（2026-09-07 实发事故）。
        """
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _raise_if_port_in_use(self) -> None:
        """Detect another process already listening on ``self.port``.

        On Windows, ``psutil.net_connections`` can raise ``AccessDenied`` for
        some system processes; we skip those entries and only report what we
        can see. If the port is occupied by the current process (e.g. a restart
        within the same process), we allow it.
        """
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

    async def yield_listening(self) -> bool:
        """交棒让位：只释放监听端口，不拆 app/runner 与 root（失败可恢复）。

        重启时新进程必须先在端口上可连才算健康，而同一端口不可能两个进程同时
        监听——旧进程因此先让位。返回 False 表示让位失败（端口仍被占），此时
        调用方必须放弃重启。
        """
        site = self.site
        if site is None:
            return True
        self.site = None
        try:
            await asyncio.wait_for(site.stop(), timeout=1.5)
        except Exception:  # noqa: BLE001 — 让位失败要复原状态，避免回退路径误判
            logger.warning("[restart] releasing listening socket failed", exc_info=True)
            self.site = site
            return False
        logger.info(f"[restart] listening released ({self.host}:{self.port})")
        return True

    async def resume_listening(self) -> None:
        """让位后回退：把监听端口重新拿回来（新实例起不来时旧进程继续服务）。"""
        if self.site is not None or self.runner is None:
            return
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"[restart] listening resumed ({self.host}:{self.port})")

    async def start(self) -> str:
        """Start the server. Returns the URL with auth token."""
        # Let publish helpers resolve COARA_HOME / persisted dev_repo.txt.
        if self.coara_home:
            import os

            os.environ.setdefault("COARA_HOME", str(self.coara_home))

        # Defensive: refuse to start if another process is already listening on
        # our port. This prevents the confusing situation where an old coara
        # process did not fully exit and the browser stays connected to it while
        # a new process starts, causing duplicated messages / stale state.
        self._raise_if_port_in_use()

        # Subscribe trace persistence (root.event_bus → TraceStore).
        # Skip if CLI already installed MultiWorkspaceTracePersistence (CLI+Web).
        from src.ui.trace_recording import (
            MultiWorkspaceTracePersistence,
            install_multi_workspace_trace_persistence,
        )

        existing = getattr(self.root, "trace_persistence", None)
        if isinstance(existing, MultiWorkspaceTracePersistence):
            self.trace_store = existing.store
        elif not self.skip_trace_persistence:
            # Foreground session may not be bound yet (tests / early boot);
            # fall back to the constructor workspace_dir.
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

        # web 跟话参与的它端回合结束：补 turn_end 帧到 web 视图（刷新不标中断）
        # 并清理跟话视图标记。
        followup_end_sub = self.root.event_bus.subscribe(
            callback=self._on_web_followup_turn_end,
            topic="turn_end",
        )
        self._subscriptions.append(followup_end_sub)

        # 子智能体最终答复的落带不走事件镜像：它作为 kind=subagent_result 的帧
        # 随实时帧同路进视图带（delegate._route_subagent_result → web 端
        # TurnStream → persist），build_messages 不投影成消息，只经快照的
        # subagent_results 映射回端上的折叠输出。旧的「合成 turn_start+chunk
        # 落盘」路径已删除——它在刷新后把子智能体答复复活成 assistant 气泡。

        # Subscribe to workspace_switched events so that workspace switches
        # initiated from CLI (/ws switch) or LLM (ws tool) also refresh
        # this server's trace_store. Without this, REST handlers
        # (/api/session/messages, /api/state, etc.) would read stale data
        # from the OLD workspace's data dir after a non-Web-initiated switch.
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

        # Registry add/remove/rename: notify the browser to refetch its workspace
        # list (covers CLI-initiated changes; ghost entries otherwise linger).
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
        # 内核忙闲探询：自动更新 pending 应用前置守卫；挂在 app 级路由，
        # 即使 REST 注册因故跳过，此端点也始终可用。
        self.app.router.add_get("/api/v1/kernel/busy", self._handle_kernel_busy)
        self.app.router.add_get("/ws", self._handle_websocket)
        # 外挂 CLI（coara attach）专用端点：多连接并存，按发起连接路由回合输出。
        # 与 webui 单活跃 /ws 完全独立，互不顶替。
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

        # SPA fallback: unmatched GET requests (e.g. /workflow/editor/:draftId)
        # serve index.html so client-side routing works on deep links.
        # Must be registered last so it doesn't shadow /api, /static, /ws, etc.
        self.app.router.add_get("/{tail:.*}", self._handle_spa_fallback)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        # 托盘线程推 focus 用；跨进程 open_or_focus 也能找到本实例。
        self._loop = asyncio.get_running_loop()
        global _ACTIVE_WEB_SERVER
        _ACTIVE_WEB_SERVER = self

        # Heartbeat: periodically flush trace store + push state updates.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        url = self.build_url()
        return url

    def build_url(self, path: str = "") -> str:
        """Return the Web UI entry URL with the auth token baked in.

        ``path`` 为 SPA 路由（如 ``/login``），缺省进首页。
        """
        route = path if (not path or path.startswith("/")) else f"/{path}"
        return f"http://{self.host}:{self.port}{route}?token={self.auth_token}"

    def open_window(self, path: str = "") -> str:
        """打开或唤起 Web UI，返回入口 URL（托盘线程可调）。

        判定统一在 :meth:`open_or_focus_decision`：有活跃连接或「刚有标签」（跨内核
        重启也记得）时只唤起，**不开**新标签（否则新标签会立刻被 SingleTabGuard 判定为
        后来者自我关闭，用户看到「开一下又关掉」）；确实没有标签才打开浏览器。
        """
        self._run_open_decision_sync(path)
        return self.build_url(path)

    def _run_open_decision_sync(self, path: str = "") -> dict[str, Any]:
        """同步桥：把决策协程丢回内核 loop 跑（托盘在独立线程）。"""
        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self.open_or_focus_decision(path), loop)
                return future.result(timeout=web_tab_presence.RECONNECT_GRACE_SECONDS + 3.0)
            except Exception as exc:  # noqa: BLE001 — 决策失败不能挡住「开窗」本身
                logger.debug(f"open decision via loop failed, falling back to open: {exc}")
        # 兜底（loop 不在/不可用）：直接开窗，至少不让用户点了没反应
        url = self.build_url(path)
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
            loop.create_task(self.registry.send_to_active(msg))
            return
        # 托盘等外线程：把协程丢进内核 loop
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
        result = await self.open_or_focus_decision(path, allow_open=False)
        # focused 语义（与旧外进程调用者兼容）：是否找到了已有标签并唤起过它。
        return web.json_response({
            "focused": result.get("action") != web_tab_presence.ACTION_OPEN,
            **result,
        })

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

    async def open_or_focus_decision(self, path: str = "", *, allow_open: bool = True) -> dict[str, Any]:
        """打开/唤起 Web UI 的三态决策（唯一真源）。

        focus-active：本进程有活跃 WS 连接 → 推 focus 帧 + 系统层置前
        focus-recent：没有连接但最近有标签（含内核刚重启、标签被浏览器冻结）
                      → 只置前 + 等它重连，**绝不开新窗**
        open        ：确实没有标签（或同一次打开窗口内的第二次点击）→ 才打开浏览器
        """
        if self.registry.has_active():
            web_tab_presence.mark_tab_seen(self.workspace_dir, coara_home=self.coara_home)
            self._request_browser_focus(path)
            await asyncio.to_thread(_try_raise_coara_browser_windows)
            return {"action": web_tab_presence.ACTION_FOCUS_ACTIVE, "reason": "active", "opened": False}

        second_click, presence = web_tab_presence.note_open_attempt(
            self.workspace_dir,
            coara_home=self.coara_home,
        )
        action = web_tab_presence.decide_open_action(
            has_active=False,
            fresh=web_tab_presence.is_tab_fresh(presence),
            second_click=second_click,
        )
        if action == web_tab_presence.ACTION_FOCUS_RECENT:
            # 先把浏览器窗口抬起来并推 focus（标签重连后即可收到），再留一小段重连窗口
            self._request_browser_focus(path)
            await asyncio.to_thread(_try_raise_coara_browser_windows)
            if await self._wait_for_tab_reconnect(web_tab_presence.RECONNECT_GRACE_SECONDS):
                self._request_browser_focus(path)
                return {"action": action, "reason": "recent-reconnected", "opened": False}
            # 等不到也不开新窗：宁可让用户再点一下（窗口内二次点击会强制开），
            # 也不制造「开一个又关掉」。
            return {"action": action, "reason": "recent-waiting", "opened": False}

        if not allow_open:
            return {"action": web_tab_presence.ACTION_OPEN, "reason": "no-tab", "opened": False}
        url = self.build_url(path)
        await asyncio.to_thread(_open_web_ui_window, url)
        await asyncio.to_thread(_try_raise_coara_browser_windows)
        return {
            "action": web_tab_presence.ACTION_OPEN,
            "reason": "second-click" if second_click else "no-tab",
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
        """GET /api/loading-phrases/custom — 当日 daily 定制轮播词（Web spinner）。

        复用 records agent 目录下的 loading_phrases_custom.json，一日抛校验由
        后端统一做（过期/缺失返回空 phrases），Web 端不各自判日期。
        """
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
        # Cancel heartbeat + trace flush first so they don't fight shutdown.
        # Do not await: if they are stuck in sync work they block the loop and
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

        # Close trace store — flushes pending writes and joins the background
        # writer thread. Critical when WebServer owns the trace_store (when the
        # kernel daemon owns the root, it owns the store's lifecycle instead).
        # Always offload: a sync join on the event loop freezes Ctrl+C exit.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(self.trace_store.close), timeout=2.0)

        # Close web 会话视图存储 writer（flush 剩余帧）。
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

        # Clear the back-reference on root so tools (save_draft / manage)
        # stop pushing WS navigation messages to this stopped server.
        self.root._web_server = None
        global _ACTIVE_WEB_SERVER
        if _ACTIVE_WEB_SERVER is self:
            _ACTIVE_WEB_SERVER = None
        self._loop = None

        # Shutdown RootCoara ONLY if the server owns it. When the kernel daemon
        # owns the root it calls shutdown itself; calling root.shutdown() here
        # would cause a double-shutdown that wastes 3-7s (workflow engine join,
        # provider close, etc.) on every exit.
        if self._owns_root:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.root.shutdown(), timeout=5.0)

    def _spawn_bg_task(self, coro: Any) -> None:
        """Run a fire-and-forget coroutine as a tracked task.

        Tracked in ``_bg_tasks`` (with done-discard) so ``stop()`` can cancel
        pending tasks instead of leaving them dangling after shutdown.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------
    # Middleware
    # ------------------------------------------------------------------

    @web.middleware
    async def _version_header_middleware(self, request: web.Request, handler: Any) -> web.Response:
        # /attachments is served by add_static which has no per-request handler
        # hook, so the token check runs here. Browsers loading <img> cannot set
        # headers — token via query string is already the established convention.
        if request.path.startswith("/attachments"):
            self._check_token(request)
        response = await handler(request)
        from src.ui.control_plane import coara_package_version, config_revision

        response.headers["X-Coara-Version"] = coara_package_version()
        response.headers["X-Coara-Config-Revision"] = config_revision()
        # 前端构建指纹：标签页长驻时跑的是「加载那一刻」的 JS，前端更新后不会自动生效。
        # 前端在心跳里比对它，变了就在安全时机自行重载（省掉手动强制刷新）。
        build = self._ui_build_fingerprint()
        if build:
            response.headers["X-Coara-UI-Build"] = build
        # Auth token lives in the URL query (?token=...); never leak it to
        # third-party sites via the Referer header on cross-origin subresources.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @staticmethod
    def _ui_build_fingerprint() -> str:
        """当前前端构建指纹（dist/index.html 的 mtime + 大小），没有构建产物时为空串。

        只认构建产物本身，与包版本号无关：改了前端并重新构建，这个值就变；
        前端在心跳里比对它，发现变了就在安全时机自行重载，不必手动强制刷新。
        """
        index = Path(__file__).resolve().parent / "static" / "dist" / "index.html"
        try:
            stat = index.stat()
        except OSError:
            return ""
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

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

    def _check_token(self, request: web.Request) -> None:
        """Validate the auth token from the request; raises HTTPUnauthorized on mismatch."""
        check_dashboard_token(
            request,
            workspace_dir=self.workspace_dir,
            coara_home=self.coara_home,
        )

    # ------------------------------------------------------------------
    # 账户许可（邮箱验证码登录）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 手机接入（代理 gomatrix 数据接口：连接状态 + 配对二维码）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 遥测中继（Android 端事件经 PC 上报；凭证只在 PC）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 宝箱（配置页：开关 / 密码管理 / 重置）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # SPA index
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        # SPA HTML is served without token check — the frontend JS reads the
        # token from sessionStorage (set on first visit via ?token=xxx in URL)
        # and includes it in all API/WS calls. This allows page refreshes and
        # deep links to work without the token in the URL.
        # Serve the Vite build's index.html if it exists, else a placeholder.
        dist_index = Path(__file__).resolve().parent / "static" / "dist" / "index.html"
        html = dist_index.read_text(encoding="utf-8") if dist_index.is_file() else self._placeholder_html()
        return web.Response(
            text=self._inject_referrer_policy_meta(html),
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    @staticmethod
    def _inject_referrer_policy_meta(html: str) -> str:
        """Ensure index HTML carries <meta name="referrer" content="no-referrer">.

        Belt-and-suspenders with the response-header middleware: the header
        covers every response, this meta keeps coverage even if the header is
        stripped by an intermediary. Injected at serve time so built dist
        artifacts don't need a rebuild.
        """
        if 'name="referrer"' in html or "name='referrer'" in html:
            return html
        meta = '<meta name="referrer" content="no-referrer" />'
        head_open = html.find("<head>")
        if head_open != -1:
            insert_at = head_open + len("<head>")
            return html[:insert_at] + meta + html[insert_at:]
        return meta + html

    async def _handle_spa_fallback(self, request: web.Request) -> web.Response:
        """Catch-all for unmatched GET requests — serve index.html for SPA routes.

        Only handles paths that look like SPA routes (no file extension, not
        under /api/, /static/, /attachments/). Those prefixes are already
        matched by their own routes, but we guard here for safety.
        """
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

    # ------------------------------------------------------------------
    # REST routes (reused from dashboard_handlers)
    # ------------------------------------------------------------------

    def _register_rest_routes(self) -> None:
        """Register REST API routes, reusing dashboard_handlers' handlers.

        We create a DashboardRestHandlers instance purely to borrow its
        battle-tested REST handlers (state, trace-detail, workflows, config,
        etc.). The handlers operate on this server's in-process TraceStore
        (passed in at construction), so they read live data.

        In addition, we register native file-system endpoints (workspace
        listing, directory browsing, file reading, image upload) that are
        unique to the embedded web server model.
        """
        # Borrow the dashboard REST handlers, sharing our in-process store so
        # REST reads live data.
        dash = DashboardRestHandlers(
            self.workspace_dir,
            coara_home=self.coara_home,
            store=self.trace_store,
            root=self.root,
        )
        self._dash_ref = dash  # keep alive

        r = self.app.router  # type: ignore[union-attr]  # app is set in start() before _register_rest_routes
        dash.register_routes(r)

        # 设置中心 CRUD（reminders / event-sources / workspaces）
        # 走活 service，即时生效
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
        r.add_get("/api/recent-files", self._handle_recent_files)
        r.add_get("/api/session/messages", self._handle_session_messages)
        r.add_post("/api/session/new", self._handle_session_new)
        r.add_post("/api/session/model", self._handle_session_model)
        r.add_get("/api/flow-session/messages", self._handle_flow_session_messages)
        r.add_get("/api/module-session/messages", self._handle_module_session_messages)
        r.add_get("/api/commands", self._handle_command_list)
        r.add_get("/api/trace/events", self._handle_trace_events)
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

        # 宝箱（配置页管理）
        r.add_get("/api/v1/vault/status", self._handle_vault_status)
        r.add_post("/api/v1/vault/enabled", self._handle_vault_enabled)
        r.add_post("/api/v1/vault/password", self._handle_vault_password_set)
        r.add_post("/api/v1/vault/password/change", self._handle_vault_password_change)
        r.add_post("/api/v1/vault/lock", self._handle_vault_lock)
        r.add_post("/api/v1/vault/reset", self._handle_vault_reset)

        # 遥测中继（Android）
        # 遥测中继接口属可选能力，见上方 register_telemetry_web

        # Workflow editor endpoints — WDL parse/emit only（WDL 执行层已剥离
        # 为独立软件：实例 list/get/run/cancel/resume 与 workflow-settings
        # 全部移除，2026-09-08）。
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

    # ------------------------------------------------------------------
    # WebSocket handler — the heart of the web UI
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_end_frame(stream: TurnStream, frame: dict) -> None:
        """内核端帧 → TurnStream 帧：web 聊天区唯一出口的映射，只此一处。"""
        kind = frame.get("kind")
        if kind == "subagent_chunk":
            # 子智能体正文：折叠在它的 delegate 工具行里（只实时投递，不落带）。
            stream.emit(
                "subagent_chunk",
                text=str(frame.get("text") or ""),
                tool_call_id=str(frame.get("tool_call_id") or ""),
                coara_id=str(frame.get("coara_id") or ""),
                subagent_id=str(frame.get("subagent_id") or ""),
            )
            return
        if kind == "subagent_result":
            # 子智能体的最终答复：同样折叠在它的 delegate 工具行里。
            stream.emit(
                "subagent_result",
                text=str(frame.get("text") or ""),
                tool_call_id=str(frame.get("tool_call_id") or ""),
                coara_id=str(frame.get("coara_id") or ""),
            )
            return
        if kind == "diff":
            # 展开传（不包 payload 键）：TurnStream 广播帧顶层带
            # display_blocks/diff_lines，前端 store case "diff" 直接读。
            # parent_tool_call_id（子智能体产的帧）随帧透传：端上折进发起它
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
            # 工具行（✓ tool(...)）：进聊天流插在正文段落之间，随
            # TurnStream persist 落视图文件 → 刷新回放位置不变。
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
        if text.strip():
            stream.emit("chunk", text=text)

    def _web_end_sender(self, stream: TurnStream) -> Any:
        """本回合的端通道 sender（精确槽 (web, session) → 这一条回合流）。

        命中判据由 EndRegistry.deliver 的 RouteResult.hit 表达（查到 sender 即
        命中），sender 的返回值不再承担判据职责；这里返回 True 只是让
        ``RouteResult.value`` 也有个显式值，便于排障时看清「投出去了」。
        """

        def sender(frame: dict) -> bool:
            self._emit_end_frame(stream, frame)
            return True

        return sender

    def _stream_for_frame(self, frame: dict) -> TurnStream | None:
        """按帧归属（turn_id / session_id）找它该去的那条回合流。

        同 turn_id 可能并存 cli-attached（persist=None）与 web followup（落带）：
        连接兜底通道必须优先 web——否则正文只广播到 attach、hydrate 永远看不见。
        """
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
        """连接级兜底通道（全局槽 (web, "")）：按帧归属投给对应的回合流。

        回合级 sender 注册在 (web, session) 精确槽，正常路径走它；一旦精确槽
        被误注销，或回合中途刷新后还没重建，EndRegistry 会落到全局槽——没有
        这条兜底，帧就静默丢弃（内核日志表现为「route ...: no sender for
        end 'web'」）。切空间 / 刷新后 diff、工具行乃至正文丢失都源于此。

        无在飞回合流时不再「落带 + 无序号直推」两路并行，而是经该 (session,
        workspace) 的 standby 流出去：落带与广播同源同序，帧上带 view_seq 与
        归属，端侧可对账。带 ``_end_connection_scope`` 标：回合收尾的 unregister
        不得删掉它，否则这条连接此后永远没有兜底。
        """

        def sender(frame: dict) -> bool:
            stream = self._stream_for_frame(frame)
            if stream is None:
                stream = self._standby_stream_for(frame)
                # 走 standby 是**正常**路径（后台/收尾后到达的帧都走它），每帧都会
                # 命中一次，故只记 debug——抬高到 warning 会把正常流量刷成告警，
                # 真正要看的「查不到 sender」已由 EndRegistry.deliver 的 miss
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
        """无在飞回合流时，为帧的 (session, workspace) 建/复用一条 standby 流。

        standby 流是「没有回合可归属的帧」的出口：不挂 task、不进 _turns、不参与
        回合收尾注销，但走与回合流同一条 _record 通道——落带（persist 分配
        view_seq）→ 写进帧 → 广播。端侧因此不会再收到无序号、无归属的直推帧。

        归属（turn_id / session_id / workspace_dir）取自帧本身：standby 流跨回合
        承载多帧，流自身的 turn_id 每帧按帧刷新。
        """
        from src.ui.turn_stream import TurnStream

        session_id = str(frame.get("session_id") or "")
        workspace = str(frame.get("workspace_dir") or "") or str(
            getattr(self, "_view_store_workspace", None) or getattr(self, "workspace_dir", "") or ""
        )
        key = (session_id, workspace)
        stream = self._standby_streams.get(key)
        if stream is None:
            # 落带由 TurnStream 缺省走内核录制器，按帧自身的空间归属解析（切空间后
            # 到达的帧落回它自己那个空间）——落错带等于把内容搬了家。
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
                # （FIFO，不是 LRU——命中已有键不会把它移到队尾）。丢的只是
                # 重连回放 buffer；落带内容在视图文件里，端上 hydrate 仍取得到。
                self._standby_streams.pop(next(iter(self._standby_streams)), None)
        stream.turn_id = str(frame.get("turn_id") or "")
        return stream

    def _restore_web_end_senders(self) -> None:
        """WS（重）连后重建在飞 web 回合的端通道（正文/diff/工具行的出口）。"""
        end_registry = getattr(self.root, "end_registry", None)
        if end_registry is None:
            return
        try:
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
            # Close with an app-defined auth code so the browser client stops
            # reconnecting (HTTP 401 before upgrade surfaces as 1006 otherwise).
            await ws.close(code=4001, message=b"Unauthorized")
            return ws

        conn_id = uuid.uuid4().hex
        await self.registry.register(ws, conn_id)
        # 标签报到：跨内核重启记住「刚才有标签」，供打开/唤起决策使用
        web_tab_presence.mark_tab_seen(self.workspace_dir, coara_home=self.coara_home)
        # 重连即补回在飞回合的端通道：回合与连接解耦但在飞回合的 sender 随旧
        # 连接注销，不补则回合中途刷新后的正文/diff/工具行全丢。
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

            # Send initial state snapshot (inside try — client may disconnect
            # immediately after prepare, e.g. page refresh).
            state = await asyncio.to_thread(self._build_state_snapshot)
            await ws.send_str(json.dumps({"type": "state", "data": state}, ensure_ascii=False))

            # 回放回合 buffer：刷新/重连后前端无缝接上（F5）。
            # 进行中回合重放后半段；已结束回合重放全文（含用户输入）——REST
            # hydrate 从视图存储补齐本会话全部历史，但视图落盘/写入有延迟，回放
            # 已结束回合兜底「刚发完就刷新输入/输出都不显示」的窗口。
            # 只回放主会话 web TurnStream——cli-attached / web-* 模块进浏览器会误亮 spinner。
            # 帧带 replayed 标记：前端 hydrate（视图存储权威）会与回放叠加双显，
            # 消费端对 replayed 帧按内容去重（同文/同回合跳过），刷新呈稳态。
            for stream in list(self._turns.values()):
                src = str(getattr(stream, "source", "") or "")
                if src != "web":
                    continue
                for frame in stream.replay():
                    replayed = dict(frame)
                    replayed["replayed"] = True
                    await ws.send_str(json.dumps(replayed, ensure_ascii=False))

            # 重连重发挂起的审批提示：断连不再取消 pending approval（挂起保留），
            # 新连接重放同一 approval_id 的提示帧，浏览器答复照常 resolve。
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
                    # After an ERROR frame the connection is broken — aiohttp
                    # ends the iterator right after this message anyway.
                    break
        except ClientConnectionResetError:
            pass
        finally:
            # 回合已与连接解耦（TurnStream）：断连不再 cancel 进行中的回合，
            # 回合在服务端照跑，输出进 buffer，前端重连后回放接续（F1/F5）。
            # 仅清理本连接的 task 跟踪表与待处理交互提示。
            self._chat_tasks.pop(conn_id, None)
            # 断连不取消挂起的审批（future 继续等，超时/回合结束兜底）；
            # 重连后在回放区块经 redeliver_pending 重发提示帧。
            self.interaction_channel.mark_connection_disconnected(conn_id)
            await self.registry.unregister(conn_id)
        return ws

    # ------------------------------------------------------------------
    # 外挂 CLI（coara attach）专用 WebSocket 端点
    # ------------------------------------------------------------------

    # /ws/attach 外挂 CLI 端点全家 — 实现见 src/ui/attach_ws.py（AttachWsHandlers）。

    async def _handle_ws_message(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """Route a single WS message to the appropriate handler.

        Chat messages are dispatched as a background task so the WS loop
        stays responsive to interrupt/approval/ping messages during a turn.
        Root's ``_process_lock`` naturally serializes concurrent messages —
        if a turn is already running, the second message queues behind it.
        """
        msg_type = data.get("type", "")
        subject = str(data.get("subject") or "root")  # "root" 主会话 / 模块主体 subject（如 "flow"）

        if msg_type == "chat":
            if self._is_module_subject(subject):
                # Pending /report 只属于主会话
                task = asyncio.create_task(self._handle_chat(data, ws, conn_id, subject=subject))
                self._chat_tasks.setdefault(conn_id, set()).add(task)
                task.add_done_callback(lambda t, cid=conn_id: self._chat_tasks.get(cid, set()).discard(t))
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
            # Fire-and-forget: the turn streams chunks back via the WS.
            # We don't await it here so the WS loop can receive interrupts.
            # Track the task so we can cancel it if the WS disconnects
            # (prevents orphaned turns from holding _process_lock forever).
            task = asyncio.create_task(self._handle_chat(data, ws, conn_id))
            self._chat_tasks.setdefault(conn_id, set()).add(task)
            task.add_done_callback(lambda t, cid=conn_id: self._chat_tasks.get(cid, set()).discard(t))
        elif msg_type == "interrupt":
            module_root = self._module_roots.get(subject) if subject != "root" else None
            if module_root is not None:
                module_root.interrupt_current_turn("user_stop", interrupt_source="stop_command")
            else:
                # 打断目标钉在帧归属空间（与 chat/command 同口径），不读「点停止那
                # 一刻」的视图指针——切空间窗口内否则会打到无关空间，本端停不掉。
                target = self._view_coara()
                frame_dir = str(data.get("workspace_dir") or "").strip()
                if frame_dir:
                    try:
                        from src.core.coara_home import workspace_id_for

                        bound = self.root.resolve_workspace_coara(workspace_id_for(Path(frame_dir)))
                        if bound is not None:
                            target = bound
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "interrupt frame workspace_dir unresolved: %s", frame_dir, exc_info=True
                        )
                target.interrupt_current_turn("user_stop", interrupt_source="stop_command")
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
        elif msg_type == "vault_reply":
            # Vault password from the Web VaultPromptCard — never forwarded
            # to the agent. Unlock the vault in-process and reply with
            # vault_result so the card can dismiss / show errors.
            password = str(data.get("password", "") or "")
            from src.ui.web_vault_bridge import handle_vault_reply

            result = await handle_vault_reply(self.root, password)
            await self.registry.send_to_active({"type": "vault_result", **result})
        elif msg_type == "vault_cancel":
            # User dismissed the vault prompt; resolve the pending future so
            # the blocked vault tool call returns immediately.
            from src.ui.web_vault_bridge import handle_vault_cancel

            result = handle_vault_cancel(self.root)
            await self.registry.send_to_active({"type": "vault_result", **result})
        elif msg_type == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
        else:
            await self._send_error(ws, f"Unknown message type: {msg_type}")

    # ------------------------------------------------------------------
    # Chat turn handler — streams root.process_message chunks to browser
    # ------------------------------------------------------------------

    @staticmethod
    def _is_module_subject(subject: str) -> bool:
        """subject 是否解析到一个启用 agentic 会话的模块（"root" 主会话除外）。"""
        if not subject or subject == "root":
            return False
        from src.coara.module_registry import module_registry

        return module_registry.by_subject(subject) is not None

    async def _create_module_root(self, spec: Any) -> Any:
        """按模块声明创建会话主体。workflow 走专属 create_flow_root，
        其余 agentic 模块走通用工厂 create_module_root。"""
        if spec.subject == "flow":
            from src.coara.flow_root import create_flow_root, switch_flow_draft_session
            from src.workflow import draft_sessions

            flow_root = await create_flow_root(self.root)
            # 冷启动恢复到当前草案的 section：优先用前端最新上报的 active id
            # （用户可能刚新建/切换了草案，此时 flow_root 尚未创建，后端没走到
            # switch_flow_draft_session），否则回退到上次离开的草案。前端随后
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
        """按 subject 惰性创建/复用模块专属会话主体（module_registry 驱动）。

        模块主体是全局工作台：会话历史与固化资产均不随工作空间切换销毁。
        flow（工作流构建对话）走现有 create_flow_root，行为与泛化前完全一致：
        workspace_dir 跟随当前前台工作空间——新 spawn 的节点默认跑在用户
        当前所在空间，已存在的图与节点不受影响。
        """
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
        """Process a chat message: run root.process_message, stream chunks.

        After the main turn completes, drains leftover continuation inputs
        (e.g. background notifications that arrived late) and processes each
        as a new turn. subject 非 "root" 时路由到对应模块主体（如 FlowRoot）。
        """
        text = str(data.get("text", "")).strip()
        # 端上生成的消息标识：原样带回 user_message 权威帧，端上据此精确认领乐观气泡
        # （不靠乐观标记/文本比对，重挂载净化或文本被改写都不会配错）
        client_msg_id = str(data.get("client_msg_id") or "").strip()
        if not text:
            await self._send_error(ws, "Empty message")
            return

        # Pending /report description — consume before LLM / slash routing.
        # （主会话专属机制，FlowRoot 不参与）
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

        # Image blocks (for vision) — refs are uploaded via REST first.
        # Text file refs are inlined into the message body (not vision blocks).
        image_refs = data.get("image_refs") or []
        file_refs = data.get("file_refs") or []
        if file_refs:
            text = await self._append_text_file_refs(text, file_refs)
        image_blocks: list[dict[str, Any]] | None = None
        if image_refs:
            image_blocks = await self._resolve_image_refs(image_refs)
        # 附件展示元数据（气泡渲染 + 回放）：图片标记 is_image，前端按此渲染缩略图。
        attachments = self._build_upload_attachments(image_refs, file_refs)

        # Pin this chat turn (and its leftover continuations) to the session
        # that is foreground *now*; a mid-turn /ws switch must not reroute the
        # queued remainders onto the new foreground session.
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
            # 端会把「消息在哪个空间发出」一并带上：带归属时按归属绑定，不再读
            # 「到达那一刻的视图指针」——切空间是两次异步往返，中间那句消息按指针
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
                    # 端报来的 workspace_dir 解析不出=异常而非正常降级，留 WARNING
                    # 便于定位（回退行为本身不变：退回视图指针照常开回合）。
                    logger.warning("chat frame workspace_dir unresolved: %s", frame_dir, exc_info=True)

        # 无可用 provider 早失败：新装未配 API key 时发消息不该起回合空转
        # （spinner 一直转、回合结束后才看到错误）。入口即校验并给配置引导。
        try:
            from src.core.config import config_manager as _cfg_mgr
            from src.llm.model_catalog import _enabled_provider_names

            if not _enabled_provider_names(_cfg_mgr):
                await ws.send_str(
                    json.dumps(
                        self._error_frame(
                            (
                                "还没有配置模型 API Key。请到侧栏「配置」页添加模型"
                                "（选厂商填 API key 即可用），或在 CLI 输入 /model --add。"
                            ),
                            turn_id="",
                            subject=subject,
                            data={"navigate": "/config"},
                        ),
                        ensure_ascii=False,
                    )
                )
                return
        except Exception:
            # 校验本身失败不拦消息（fail-open，避免误判阻断正常对话）
            pass

        # 视觉门控：当前模型不支持图像时，图片省略并提示，避免误导"已附加 N 张图"。
        if image_blocks:
            from src.core.config import config_manager
            from src.llm.vision import model_supports_vision

            _pname = str(getattr(turn_coara, "provider_name", "") or "")
            _mname = str(getattr(turn_coara, "model_name", "") or "")
            # fail-open：model 无法判定（空/未解析）时不拦截图片，避免误伤。
            if _mname and not model_supports_vision(_mname, provider_name=_pname, config_manager=config_manager):
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

        # 活跃回合期间的跟话（含图）注入接续队列，与 CLI Plan B / 手机端对齐。
        # 主会话（subject=root）与手机端同待遇：包装远端标记 + 登记回投镜像（回合
        # 后续输出经 turn 流回本 Web 客户端）。模块主体（subject=flow 等）
        # 保持原注入语义（构建对话需要回合内上下文，流式由其自有连接承担）。
        if turn_coara.has_active_turn() and not text.startswith("/"):
            if subject != "root":
                turn_coara.submit_continuation_input(text, image_blocks=image_blocks, source="web")
                return

            # deferred send_text：审批/交互门回投本端（非聊天正文路径）。
            # 聊天 chunk / user_message 一律走下方 TurnStream → EndRegistry。
            # 入站不再包内容标签：内核按 source=web 现包（turn_orchestrator 接续现包）。
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
            # 跟话 user_message 与后续正文：正式 TurnStream（同开局 emit_user_message），
            # 微批+buffer+视图落盘。无 web 通道时补登记，否则段切到 web 后 chunk 静默丢。
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
                    if (not turn_coara.has_active_turn()) or (
                        _turn_id and _live_tid and _live_tid != _turn_id
                    ):
                        _target_stream.emit("turn_end", reason="complete")
                        _target_stream.finish()
                        self._web_followup_view_turns.discard(_key)
                        _target_stream = None

                if _target_stream is not None:
                    # 已有 web 流时也要确保 ("web", session) 指向它——stale sender
                    # 或它端回合无通道时段切到 web 会静默丢 chunk。
                    def _route_sender(frame: dict, _stream: TurnStream = _target_stream) -> None:
                        if frame.get("kind") == "diff":
                            _stream.emit(
                                "diff",
                                display_blocks=frame.get("display_blocks"),
                                diff_lines=frame.get("diff_lines"),
                                tool_name=frame.get("tool_name", ""),
                                **_diff_frame_kwargs(frame),
                            )
                            return
                        if frame.get("kind") == "tool":
                            _stream.emit(
                                "tool",
                                text=str(frame.get("text") or ""),
                                ok=not bool(frame.get("is_error", False)),
                                tool_name=frame.get("tool_name", ""),
                                tool_call_id=frame.get("tool_call_id", ""),
                                duration_ms=frame.get("duration_ms"),
                                **_parent_tool_call_kwargs(frame),
                            )
                            return
                        chunk_text = str(frame.get("text") or "")
                        if chunk_text.strip():
                            _stream.emit("chunk", text=chunk_text)

                    end_registry.register("web", _route_sender, _sess_id)
                    _target_stream.emit_user_message(
                        text,
                        attachments=attachments or [],
                        client_msg_id=client_msg_id,
                    )
            turn_coara.submit_continuation_input(text, image_blocks=image_blocks, source="web")
            return

        # Turn-chain registration. Every full-turn _handle_chat registers its
        # completion future here; each message awaits the previous tail so turns
        # run strictly FIFO（防并发 turn_start 抢先切气泡，两条回复合并显示）。
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

            # Leftover：回合结束后仍在队列的项 = 新回合启动输入（不再是接续）。
            # 必须按每条 ContinuationInput.source 开回合，禁止绑死本 web 连接。
            leftover = turn_coara.drain_continuation_inputs()
            while leftover:
                from src.coara.continuation_leftover import dispatch_leftover_item

                for cont_item in leftover:

                    async def _run_web(text: str, images: list | None) -> None:
                        await self._stream_chat_turn(
                            text,
                            ws,
                            conn_id,
                            image_blocks=images,
                            bind_coara=turn_coara,
                            bind_ws_id=turn_ws_id,
                            subject=subject,
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
        """Run a single chat turn: send turn_start, stream process_message, send turn_end.

        *bind_coara* / *bind_ws_id* pin the turn to a specific session; without
        them the foreground is read at call time. *subject* tags downstream WS
        events ("root" main chat / 模块主体如 "flow" workflow workbench) and
        picks the per-module source tag ("web-<subject>") for persisted history.

        回合输出全部进 TurnStream（与连接解耦）：turn_start/chunk/turn_end 一律
        emit 进 buffer + 广播给当前活跃连接，刷新/断连不杀回合，重连回放接续。
        """
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
        # 正式用户行先于 turn_start（与中途跟话共用 emit_user_message）。
        # 下方 process_message 仍收未剥的 text——模型读完整指令。
        stream.emit_user_message(
            text,
            attachments=attachments or [],
            client_msg_id=client_msg_id,
        )
        stream.emit("turn_start")
        # 主会话回合串行（_process_lock）：CLI/Matrix 占着回合时 Web 消息只是
        # 静默排队 用户看着像没反应——显式告知前端「排队中」让气泡有占位提示。
        if subject == "root":
            queued_coara = bind_coara if bind_coara is not None else self.root.foreground_coara
            if queued_coara.has_active_turn():
                stream.emit("turn_queued")

        # 注册本回合显示通道到 EndRegistry：正文 chunk 由 base.py 统一路由
        # 投递（sender 闭包捕获本回合 stream，微批+进 buffer 供重连回放）。
        # 仅主会话注册——模块主体（web-flow 等）保持循环自渲（构建对话需要
        # 回合内上下文，且 source 与主会话不同键）。中途切走（视图 detach）
        # 后通道**保留**：输出进 buffer/视图存储，切回实时恢复、刷新回放接续。
        end_registry = getattr(self.root, "end_registry", None)
        sender: Any = None
        # 注册键必须与内核路由键同源：内核拿「处理本回合的 coara」的 session_id 查表，
        # 这里就得用同一个 coara 的 session_id；未显式绑定时退回本端视图 coara。
        # 绝不能用空串注册——空串是连接级兜底槽，回合 sender 占上去、回合收尾再注销，
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

                    # web 独立视图（D6）：detach 判定按本端视图空间——CLI/matrix
                    # 切全局前台不再 detach web 正在看的回合。
                    def _still_web_view() -> bool:
                        current = getattr(self.root, "web_view_workspace_id", None) or self.root._foreground_session_id
                        return current == turn_ws_id

                    iterator = iter_while_foreground(
                        agen,
                        _still_web_view,
                        drain_name="web-detached-workspace-turn",
                    )
                if subject == "root" and end_registry is not None:
                    # 主会话：正文已由 EndRegistry 路由投递，循环只做观察消费。
                    # 切走（视图 detach）时**保留**显示通道：正文经 sender 进
                    # TurnStream buffer 并落视图存储——无活跃浏览器时广播天然
                    # 跳过，切回时 chunk 实时恢复、刷新经 buffer 回放接续。
                    # 在此注销会让 drain 中的回合后续 chunk 查无通道纯丢弃
                    # （route chunk: no sender for end 'web'），且切回不恢复。
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
            logger.exception(f"Chat turn error: {exc}")
            from src.coara.turn_orchestrator import _user_facing_turn_error

            error_message = _user_facing_turn_error(exc)
            stream.emit("chunk", text=error_message)
            stream.emit("error", message=error_message)
        finally:
            # 清理显示通道（防异常路径残留）；仅当仍指向本回合 sender 才删。
            if end_registry is not None and sender is not None:
                end_registry.unregister("web", sender, _sess_id)
            # turn_end 进 buffer：前端按 seq 对账不会因 trace 竞态丢失而卡 turnActive。
            # message 挂在 turn_end 上，避免客户端只看到 reason=error。
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
        """它端收尾时把 source=web 的 leftover 开成 web 新回合（无发起 WS）。

        与 ``_stream_chat_turn`` 同构，conn 用合成 id；正文经 TurnStream +
        EndRegistry 进活跃浏览器。
        """
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
        """记录 web 当前视图空间（视图切换时随 trace_store 刷新）。

        落带不再需要端侧回调：TurnStream 缺省走内核录制器，按每帧自身的
        workspace + session 归属定线——切空间后到达的帧也落回它自己那个空间。
        """
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
        """后台完成唤醒回合：把输出经 TurnStream 流回发起端浏览器。

        与 ``_stream_chat_turn`` 同构，但由内核（root）在后台任务完成时驱动，
        无发起 WS 连接。origin_source="web" 时才走这里——matrix/event 由 root
        在唤醒时注册 EndRegistry 通道逐帧投递，cli 经段路由兜底到
        session_origin。帧进 buffer，浏览器断连/刷新后重连回放无缝接续。
        """
        turn_id = uuid.uuid4().hex
        # 视图落盘按发起空间解析，不用当前视图 persist——用户已切走空间时
        # 唤醒回合帧必须落回发起空间的视图文件（P1-3b），切回才能回放。
        _ws_dir = workspace_dir or getattr(target_coara, "workspace_dir", None) or self.workspace_dir
        stream = TurnStream(
            turn_id,
            "web",
            "root",
            self,
            # 标记唤醒流：正文经 background 通道送入（路由键 ("background", session)），
            # 本流并不持有 ("web", session) 通道——_has_active_web_turn_stream 据此
            # 把它排除，否则 web 跟话会误判「web 通道已在跑」而跳过注册（chunk 静默丢弃）
            channel_id="awakened-web",
            session_id=str(getattr(target_coara, "session_id", "") or ""),
            workspace_dir=str(_ws_dir),
        )
        self._turns[turn_id] = stream
        # 唤醒回合无人「提问」：以系统注记开场，让浏览器端知道这是一条后台
        # 结果回投而非普通对话，回放时也能还原上下文。
        stream.emit("turn_start", awakened=True, task_id=task_id, origin_source=origin_source)

        # 唤醒回合同样经 EndRegistry 路由：键 ("background", session_id)，
        # 与 process_message(source="background") 的 seg_source 匹配。
        end_registry = getattr(self.root, "end_registry", None)
        _sess_id = str(getattr(target_coara, "session_id", "") or "")
        sender: Any = None
        if end_registry is not None:

            def sender(frame: dict) -> None:
                if frame.get("kind") == "diff":
                    # 展开传（不包 payload 键）：TurnStream 广播帧顶层带
                    # display_blocks/diff_lines，前端 store case "diff" 直接读。
                    stream.emit(
                        "diff",
                        display_blocks=frame.get("display_blocks"),
                        diff_lines=frame.get("diff_lines"),
                        tool_name=frame.get("tool_name", ""),
                        **_diff_frame_kwargs(frame),
                    )
                    return
                if frame.get("kind") == "tool":
                    # 工具行（✓ tool(...)）：进聊天流插在正文段落之间，随
                    # TurnStream persist 落视图文件 → 刷新回放位置不变。
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
                if text.strip():
                    stream.emit("chunk", text=text)

            end_registry.register("background", sender, _sess_id)
        reason = "complete"
        error_message: str | None = None
        try:
            async with turn(
                "web",
                channel_id="awakened-web",
                send_text=None,
                interaction_channel=self.interaction_channel,
            ):
                agen = target_coara.process_message(
                    text,
                    trust_level="owner",
                    show_tool_summary=True,
                    source="background",
                    turn_id=turn_id,
                )
                # 纯驱动循环：正文已由 EndRegistry 路由投递（✓/✗ 工具行被
                # base 路由前的 ✓/✗ 前缀过滤拦截，不进 chunk 通道）。
                async for _ in agen:
                    pass
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:
            reason = "error"
            logger.exception(f"Awakened web turn error: {exc}")
            from src.coara.turn_orchestrator import _user_facing_turn_error

            error_message = _user_facing_turn_error(exc)
            stream.emit("chunk", text=error_message)
            stream.emit("error", message=error_message)
        finally:
            if end_registry is not None and sender is not None:
                end_registry.unregister("background", sender, _sess_id)
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
        """回收已结束回合：只保留最近 N 个 done 的 TurnStream（供刷新回放），其余删除。

        进行中的回合从不在此清理；已结束回合的 buffer 仅服务于「刷新后立即重连」
        的短窗口，长期保留没有意义。
        """
        done = [tid for tid, s in self._turns.items() if s.done]
        excess = len(done) - self._FINISHED_TURNS_KEEP
        if excess <= 0:
            return
        for tid in done[:excess]:
            self._turns.pop(tid, None)

    _MAX_TEXT_FILE_CHARS = 100_000
    # Pillow 无法解码的图允许原始 base64 回退的大小上限（防巨串永久驻留上下文）
    _FALLBACK_RAW_IMAGE_MAX_BYTES = 5 * 1024 * 1024

    # ------------------------------------------------------------------
    # Slash command handler
    # ------------------------------------------------------------------

    async def _handle_flow_snapshot(self, data: dict[str, Any], ws: web.WebSocketResponse) -> None:
        """Flow 实时视图：返回指定 flow 的全量图快照（节点/边/状态）。

        subject 非 "root" 取对应模块主体的图（如 FlowRoot）；缺省取主会话前台实例的图。
        """
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

    # Web 端 slash 黑名单：这些命令在 Web 端有图形等价（点鼠标即可），不从斜杠放
    # 行。exit/quit（关标签页）、login（账号页）、status（状态侧边栏）、ws（顶栏
    # 空间切换）、new（顶栏「新会话」）、model（顶栏模型选择）。仅拦 Web 入口，
    # 不影响 CLI/Matrix/attach 的同名命令。/restart 不在此列：它没有图形等价，
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
        # 模块会话（FlowRoot 构建对话等）里的会话级命令作用在模块主体上，
        # 而不是主会话前台实例——否则 /compact 会压错会话、看起来「不起作用」。
        target_coara = None
        if subject != "root" and self._is_module_subject(subject):
            from src.coara.commands.registry import parse_command as _parse

            parsed = _parse(raw)
            if parsed is not None and parsed.name in _MODULE_SESSION_COMMANDS:
                target_coara = await self._get_module_root(subject)
        else:
            # 普通 web 会话：命令作用于 web 当前视图空间（端独立——/model 切的
            # 是自己看的空间并绑定到该空间，不再漂到 CLI 前台/active_id）。
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
                    # 同 chat 帧：解析失败是异常，留 WARNING（回退行为不变）。
                    logger.warning("command frame workspace_dir unresolved: %s", frame_dir, exc_info=True)
        result = await execute_command(
            self.root, raw, target_coara=target_coara, interaction_channel=self.interaction_channel
        )
        if result is None:
            await self._send_error(ws, "Not a command")
            return

        # 会话状态变更类命令（/compact）的结果落视图存储：web 刷新后仍可见
        # 「已压缩」记录，与实时显示一致（其余瞬时命令维持 command_result 直发）。
        view_seq = 0
        if (
            parsed is not None
            and parsed.name in _MODULE_SESSION_COMMANDS
            and getattr(result, "output", "")
            and getattr(result, "data", None)
            and result.data.get("compressed")
        ):
            view_seq = self._persist_command_result_view(target_coara, subject, result.output)

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
                    # 模型切换不再落分隔线：顶栏模型名就是可见结果，线上多一行
                    # 只会添噪。web 端唯一的分隔线来自「新会话」。
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

        # command_result 是本连接的单播回执：必须盖 workspace_dir / session_id，
        # 否则端上 _frameInBoundary 会把结果整帧丢掉（pendingCommand 永不清除，
        # /compact 看起来像「卡住且没执行」）。
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

    def _persist_timeline_divider(self, label: str, *, subject: str = "root") -> None:
        """往空间这条线落一个分隔标记帧（只在新会话成功时调用）。

        端侧点「新会话」是本端看得见的动作，但「上下文被清空」这件事看不见——
        线上多一条带时间的线是给它的锚点。其余触发（切空间、发消息、模型切换、
        时间空档）都不落线。
        """
        view_store = getattr(self, "_view_store", None)
        label = (label or "").strip()
        if view_store is None or not label:
            return
        try:
            # 归属本端视图空间，不读全局前台（单例指针）：分隔帧要落在你正在看的
            # 那个空间的线上，会话键也用它自己的。
            try:
                target = self.root.resolve_web_view_coara()
            except Exception:  # noqa: BLE001
                target = getattr(self.root, "foreground_coara", None)
            sess_id = str(getattr(target, "session_id", "") or "")
            from src.ui.view_recorder import record_view_frame

            record_view_frame(
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

    def _persist_command_result_view(self, coara: Any | None, subject: str, output: str) -> int:
        """把会话状态变更命令的结果写入 web 视图存储（刷新后与实时一致）。

        命令本身不是回合：给结果一个独立 turn_id，先写 turn_start 再写 chunk
        （build_messages 只聚合 turn_start 之后同 turn 的帧，缺起点会被当散帧
        跳过）。chunk 带 ``is_command_result``，hydrate 仍渲染为命令卡。
        返回 chunk 的 view_seq（供实时 command_result 回填，失败返回 0）。
        """
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

    # ------------------------------------------------------------------
    # Trace event broadcast (EventBus → WS) — 实现见 src/ui/trace_broadcast.py
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # File system REST endpoints
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Updates inbox REST endpoints（消息中心）
    # ------------------------------------------------------------------

    async def _handle_trace_events(self, request: web.Request) -> web.Response:
        """Return recent tool-activity events for the Web StatusSidebar hydrate.

        Shape matches the live WS ``_on_trace_event`` forwarder
        (``{type, turn_id, tool, ...}``). Default ``kinds=tool`` returns the
        sidebar-relevant subset (tools + round markers), newest last.
        """
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

        # 尾部倒读：只需最近 limit 条相关事件，不全量加载整个 trace 文件
        # （大文件下全量读+解析每次数百 ms 到数秒，是切空间卡顿的主因）。
        def _match(row: dict[str, Any]) -> bool:
            event_type = row.get("event_type", "")
            if event_type not in relevant:
                return False
            payload = row.get("payload") or {}
            # web 侧栏只显示 web 来源回合的工具调用（与聊天区/WS 实时同一规则）：
            # 他端来源（cli/matrix/background）的工具事件不返回；
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
        # 倒读已按时间升序返回，无需 reverse；条数已由 limit 截断。
        return web.json_response({"events": mapped, "total": len(mapped)})

    def _error_frame(self, message: str, **extra: Any) -> dict[str, Any]:
        """错误帧统一形状：一律带当前 runtime 的归属（session_id / workspace_dir）。

        端侧按同一把尺做边界守卫（无归属的帧一律丢弃），不带归属的错误帧到浏览器
        会被静默丢掉——「Empty message」「Web 端请用界面操作代替 /xxx」这类提示用户
        根本看不见。模块错误另带 subject（调用方经 ``extra`` 传）。
        """
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
        """Build the initial WS state frame — runtime only.

        内容都有自己的权威来源（消息线由视图带快照给，工具活动由 trace 接口给），
        不在这个帧里重复投喂。此前这里顺带投影 sessions：全量读会话磁带 + 会话
        聚合，冷启数秒、载荷近 10MB，而端上根本不消费它——刷新要等十秒的主因就是
        它。状态帧只留 runtime（端上要靠它拿到 session_id/workspace_dir 定边界）。
        """
        runtime = self._current_runtime()
        self._last_runtime_snapshot = runtime
        return {"runtime": runtime}

    async def _heartbeat_loop(self) -> None:
        """Periodically flush trace store and push lightweight runtime status.

        IMPORTANT: This loop only sends a small runtime dict (session_id,
        status, provider, model, running) — NOT the full state snapshot.
        The full snapshot (with sessions) is only sent
        on initial WS connection via ``_build_state_snapshot``. Loading
        entire JSONL files every 2 seconds was the primary cause of UI lag.
        """
        try:
            while True:
                await asyncio.sleep(5)
                await self.trace_store.heartbeat()
                # Push lightweight runtime update only — no JSONL reads.
                if self.registry.has_active():
                    runtime = self._build_lightweight_runtime()
                    if runtime is not None:
                        await self.registry.send_to_active({"type": "state", "data": {"runtime": runtime}})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception(f"Heartbeat error: {exc}")

    def _view_coara(self) -> Any:
        """web 视图空间 coara（D6）；未独立绑定（视图 id 非真 str）时回退前台。

        与 matrix_view_coara 同构；读 pin 走 :meth:`RootCoara.pinned_view_id`。
        """
        pinned = getattr(self.root, "pinned_view_id", None)
        view_id = pinned("web") if callable(pinned) else None
        if isinstance(view_id, str) and view_id:
            resolver = getattr(self.root, "resolve_web_view_coara", None)
            if callable(resolver):
                try:
                    return resolver()
                except Exception:
                    pass
        return self.root.foreground_coara

    def _current_runtime(self) -> dict[str, Any]:
        """web 视图 session runtime fields for WS state / heartbeat（D6 按视图出）。"""
        fg = self._view_coara()
        workspace_name = ""
        if self.root.workspace_manager is not None:
            from src.workspace.catalog import resolve_foreground_active_name

            workspace_name = resolve_foreground_active_name(fg, self.root.workspace_manager) or ""
        runtime = {
            "session_id": fg.session_id,
            "status": fg.status.value,
            "provider": fg.provider_name,
            "model": fg.model_name,
            "running": fg.has_active_turn(),
            # Web spinner 只认 web 发起的回合；CLI/Matrix 同空间在跑不得亮浏览器圈
            "turn_source": str(getattr(fg, "_active_turn_source", "") or ""),
            "alive": True,
            "workspace_name": workspace_name,
            "workspace_dir": str(fg.workspace_dir),
        }
        runtime.update(self._foreground_context_usage())
        return runtime

    def _foreground_context_usage(self) -> dict[str, Any]:
        """Same accounting as CLI bottom toolbar: provider-reported tokens only."""
        from src.llm.usage import total_prompt_tokens

        fg = self._view_coara()
        used = 0
        hit_ratio: float | None = None
        snap = getattr(fg, "_llm_usage_snapshot", None)
        if snap is not None:
            if getattr(snap, "has_reported_input", False):
                usage = getattr(snap, "usage", None) or {}
                used = total_prompt_tokens(usage) + int(usage.get("output_tokens") or 0)
            try:
                hit_ratio = snap.cache_hit_ratio
            except Exception:
                hit_ratio = None
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
        }

    def _build_lightweight_runtime(self) -> dict[str, Any] | None:
        """Build a small runtime dict without reading any JSONL files.

        Compares against the last sent snapshot to skip redundant pushes
        when nothing has changed (e.g. idle session).
        """
        runtime = self._current_runtime()
        # Skip push if nothing changed since last heartbeat.
        if self._last_runtime_snapshot == runtime:
            return None
        self._last_runtime_snapshot = runtime
        return runtime


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------


# 本进程内活跃 WebServer（托盘 / open_or_focus 同进程直调）。
_ACTIVE_WEB_SERVER: WebServer | None = None


# 内核化后 CLI 是平等 attach 客户端：渲染经 RootShim + 事件通道，无服务端镜像。


def _request_server_open(*, host: str, port: int, token: str, path: str = "", timeout_s: float = 6.0) -> bool:
    """请内核执行打开/唤起决策（``/api/ui/open``）。成功返回 True。

    超时要覆盖内核侧的「等标签重连」窗口（RECONNECT_GRACE_SECONDS）。
    """
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
    """打开或唤起 Web UI，返回入口 URL。

    判定统一在内核侧：同进程直调 ``WebServer.open_window``，外进程请求
    ``/api/ui/open``（内核决定唤起还是开新窗）——这样「开一个又关掉」不会因为
    各入口各写一套判定而复现。内核不可达时才在本地回退打开。
    """
    server = _ACTIVE_WEB_SERVER
    if server is not None:
        return server.open_window(path)
    route = path if (not path or path.startswith("/")) else f"/{path}"
    entry = f"http://{host}:{port}{route}" + (f"?token={token}" if token else "")
    if _request_server_open(host=host, port=port, token=token, path=path):
        _try_raise_coara_browser_windows()
        return entry
    target = url.strip() or entry
    _open_web_ui_window(target)
    _try_raise_coara_browser_windows()
    return target


def _open_web_ui_window(url: str) -> None:
    """Open the Web UI in the system default browser (same as clicking the URL in terminal).

    ``new=0`` prefers an existing browser window; ``autoraise=True`` asks the OS
    to bring it forward. Token is still in the URL so a cleaned address bar
    (SPA strips ``?token=``) navigates again and typically focuses the tab.
    """
    try:
        webbrowser.open(url, new=0, autoraise=True)
    except Exception as exc:
        logger.debug(f"Failed to open browser: {exc}")


def _try_raise_coara_browser_windows() -> None:
    """Best-effort: bring existing coara browser windows to the foreground (Windows).

    ``webbrowser.open`` alone often leaves an already-open tab behind other
    desktop windows. Enumerate visible top-level windows whose title looks like
    a browser tab named ``coara`` and restore + activate them.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 声明 64 位安全原型：默认 restype/argtypes 是 c_int，会把指针/HWND
        # 截断成 32 位，回调拿到的 hwnd 是带符号的垃圾值，再喂回 IsWindowVisible
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
            # 只在最小化时 SW_RESTORE 还原；最大化/普通窗口只置顶不动尺寸——
            # 否则对最大化窗口调 SW_RESTORE 会把它还原成普通大小（看着像自动缩小）。
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, sw_restore)
            user32.SetForegroundWindow(hwnd)
    except Exception as exc:
        logger.debug(f"Failed to raise coara browser window: {exc}")


async def _should_open_web_ui_window(server: WebServer, *, probe_s: float = 5.0) -> bool:
    """自动识别：已有 WebUI 活跃连接则不再重复打开标签。

    重启场景下旧标签的 WS 会以指数退避自动重连（1s 起），token 跨重启持久，
    因此旧标签能无缝接管。这里给出一段探测窗口：期间检测到任何活跃浏览器
    连接，说明浏览器里已开着 Web UI，跳过自动打开；否则才打开新标签。
    """
    deadline = time.monotonic() + probe_s
    while time.monotonic() < deadline:
        if server.registry.has_active():
            return False
        await asyncio.sleep(0.2)
    return True


async def _wait_until_ready(host: str, port: int, *, timeout_s: float = 5.0, retries: int = 3) -> bool:
    """Probe ``http://{host}:{port}/`` until the server responds or we give up.

    Guards against opening the app window before the HTTP listener is actually
    serving (a blank window was observed when the window raced the server).
    ``trust_env=False`` keeps the probe off any system proxy — it targets this
    same process. Best-effort: callers proceed even when this returns False.
    """
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
    """横幅显示的前台会话实际模型：优先 foreground_coara，退回传入参数。

    内核 daemon 起 web 时传入的是 bootstrap 解析的全局默认，
    前台会话可能被空间级 LLM 绑定覆盖（entry_llm_override），横幅须显示
    前台会话实际生效的 provider/model。
    """
    fg_provider = ""
    fg_model = ""
    try:
        fg = root.foreground_coara
        fg_provider = getattr(fg, "provider_name", "") or ""
        fg_model = getattr(fg, "model_name", "") or ""
    except Exception:
        pass
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
    """Start the embedded web server.

    When *root* is None, a new RootCoara is created (standalone Web mode).
    When *root* is provided (e.g. by ``run_chat_session`` for CLI+Web
    co-existence), the existing root is reused so CLI and WebUI share
    the same runtime.
    """
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
        # 有界停服：否则 FlowRoot/aiohttp 挂住时 CLI 的 gather 超时后任务仍占着 loop，
        # asyncio.run 收尾 cancel 不掉 → 用户看到 Click「Aborted!」却退不出去。
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.stop(), timeout=8.0)
