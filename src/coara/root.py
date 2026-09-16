"""Root coara implementation.

Root absorbs the responsibilities that the former coaraOrchestrator middle
layer used to provide: it owns the EventBus and the workspace session pool.
External observers (CLI, Dashboard) talk to `root.event_bus` directly.
WDL 执行层已剥离为独立 WDL 软件——coara 只做 .wdl 的生产者与文件宿主，
Root 不再托管工作流引擎子进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.coara.base import CoaraBase
from src.coara.event_bus import EventBus, Subscription
from src.coara.janitor_scheduler import JanitorScheduler
from src.coara.matrix_notify import MatrixNotificationBridge
from src.coara.scheduler import UnifiedScheduler
from src.coara.updates_matrix_sync import format_updates_payload
from src.core.events import TraceEvent
from src.core.logger import logger
from src.core.types import (
    CoaraPersona,
    ContinuationInput,
)
from src.prompt.agent_registry import AgentRegistry
from src.workspace.updates.catalog import build_updates_workspaces_payload
from src.workspace.updates.store import WorkspaceUpdatesStore
from src.workspace.updates.types import WorkspaceUpdate

if TYPE_CHECKING:
    from src.event_sources.manager import EventSourceManager
    from src.reminders.service import ReminderService
    from src.vault.service import VaultService
    from src.workspace.manager import WorkspaceManager


class RootCoara(CoaraBase):
    """Top-level coara facing the user."""

    _MAX_TOOL_ITERATIONS = 1200

    def __init__(
        self,
        name: str = "coara",
        workspace_dir: Path | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        workspace_alias: str | None = None,
    ):

        registry = AgentRegistry()
        root_def = registry.get_agent("root")

        root_prompt = root_def.system_prompt_template if root_def else "You are the CEO of this AI system."
        root_name = root_def.name if root_def else name
        root_expertise = ["general purpose", "orchestration"]

        persona = CoaraPersona(
            name=root_name,
            role="CEO & Master Orchestrator",
            expertise_areas=root_expertise,
            system_prompt_template=root_prompt,
            yaml_config=root_def.yaml_config if root_def else None,
        )
        super().__init__(
            name=root_name,
            persona=persona,
            workspace_dir=workspace_dir,
            provider_name=provider_name,
            model=model,
            user_facing=True,
            is_owner_context=True,
            max_tool_iterations=self._MAX_TOOL_ITERATIONS,
        )
        self.scheduler = UnifiedScheduler()
        self._bg_tasks: set[asyncio.Task] = set()

        self.event_bus: EventBus = EventBus()
        self._subscriptions: list[Subscription] = []
        self.workspace_manager: WorkspaceManager | None = None
        self.event_source_manager: EventSourceManager | None = None
        self.reminder_service: ReminderService | None = None
        self.matrix_notify = MatrixNotificationBridge()
        # 端注册表：正文动态路由按「当前注入段 source」查各端当前活跃出站通道
        # （web 活跃 WS / attach 当前连接 / matrix room），替代各端闭包捕获死目标。
        from src.coara.end_registry import EndRegistry

        self.end_registry = EndRegistry()
        self._requested_workspace = workspace_alias
        self.vault_service: VaultService | None = None
        # 装配件：由 root_lifecycle 注入，缺省为 None（开源发行版没有遥测等实现）
        self.telemetry_service: Any | None = None
        self.matrix_host: Any | None = None  # GoMatrixHost，gomatrix 托管
        self.records_store: Any | None = None  # 统一 records/{agent,user}
        # Shared idle clock for CLI / Web / Matrix. Root alone auto-/new after
        # ``session.idle_timeout_seconds``; Android follows via [COARA_STATUS].
        self._last_user_activity_at: float = time.time()
        self._idle_timeout_task: asyncio.Task[Any] | None = None
        # WorkspaceSession：每个工作空间对等；Root 只做宿主。
        self._sessions: dict[str, Any] = {}
        # ensure_workspace_session 的 check-then-create 加锁：janitor 后台派发
        # 与用户切空间可能并发为同一空间建会话
        self._sessions_lock = asyncio.Lock()
        # 上次重启的交接记录：新实例启动早期把意图归档成 restart_last.json，并写下
        # 就绪标记供旧进程确认交棒（协议见 src/runtime/restart.py）。失败只记日志。
        from src.runtime.restart import consume_restart_intent

        consume_restart_intent()
        # 启动默认指针 / 兼容别名：与 cli view 同步。内核 sessions 彼此平等，
        # 「前台」只表示某端的 view，不在内核层享有特权。
        self._foreground_session_id: str | None = None
        # 每端独立 view（cli / web / matrix）。None = 尚未 pin，读时回退默认指针。
        self._cli_view_workspace_id: str | None = None
        self._matrix_view_workspace_id: str | None = None
        self._web_view_workspace_id: str | None = None
        # Per-workspace last *conversation activity* time (epoch seconds): a real
        # user message (turn start) or a finished turn (last LLM API call).
        # Seeded from disk session_state.json when a WorkspaceSession is created;
        # stamped by record_user_activity (CLI / Web / Matrix messages) and by
        # record_turn_activity (WorkspaceSession turn_end events). Switch and
        # /new must not refresh this clock.
        self._workspace_activity_at: dict[str, float] = {}
        # Result of the latest switch_workspace staleness check (for UI wording).
        self.last_switch_session_renewed: bool = False
        self.last_switch_last_active: float | None = None
        # 工作空间概况（ws.md）维护与空闲续会话：编排抽为 JanitorScheduler（台账 #5），
        # 状态（已维护 epoch / 在飞表 / 启动补扫单飞）由组件自持，下方同名转发保留触点。
        self._janitor = JanitorScheduler(self)

    # 端 view 键：attach 连接也归 cli（origin 仍可用 cli-attached）。

    @staticmethod
    def normalize_view_end(end: str) -> str:
        """Map origin_source / end labels onto a view key."""
        raw = (end or "").strip().lower()
        if raw in ("", "cli", "cli-attached", "cli_attached"):
            return "cli"
        if raw in ("web", "matrix"):
            return raw
        raise ValueError(f"unknown view end: {end!r}")

    def _view_attr(self, end: str) -> str:
        return f"_{self.normalize_view_end(end)}_view_workspace_id"

    def view_workspace_id(self, end: str) -> str:
        """该端当前 view 的 workspace_id；未 pin 时回退启动默认指针。"""
        end_key = self.normalize_view_end(end)
        vid = getattr(self, self._view_attr(end_key), None)
        if isinstance(vid, str) and vid:
            return vid
        return self._foreground_session_id or ""

    def pinned_view_id(self, end: str) -> str | None:
        """该端已 pin 的 workspace_id；未 pin 或非真 str（MagicMock）返回 None。

        读写约定：对外读 id 用 :meth:`view_workspace_id` / ``{end}_view_workspace_id``
        属性；写 pin 用 :meth:`set_view_workspace`。本方法仅供「是否已独立 pin」
        探测（替身 root 的同名存储字段是 Mock，不能当 id）。
        """
        end_key = self.normalize_view_end(end)
        vid = getattr(self, self._view_attr(end_key), None)
        return vid if isinstance(vid, str) and vid else None

    def resolve_view_coara(self, end: str) -> CoaraBase:
        """该端 view 空间的 CoaraBase。"""
        view_id = self.view_workspace_id(end)
        if view_id:
            session = self._sessions.get(view_id)
            if session is not None:
                return session.coara
        if self._foreground_session_id is None:
            raise RuntimeError("No workspace view bound")
        session = self._sessions.get(self._foreground_session_id)
        if session is None:
            raise RuntimeError(f"Default WorkspaceSession missing: {self._foreground_session_id}")
        return session.coara

    async def set_view_workspace(
        self,
        end: str,
        workspace: str,
        *,
        republish_runtime: bool = True,
        emit_event: bool = True,
    ) -> bool:
        """只换该端 view：ensure session + pin。无 vault lock / os.chdir / 占用互斥。

        cli view 额外：同步默认指针、可选 staleness 续会话、写 active.json（内核存活提示）、
        发 ``view_changed``（payload.end=cli）。web/matrix 同理发带 end 的事件，
        不拖其它端。
        """
        end_key = self.normalize_view_end(end)
        if self.workspace_manager is None:
            return False
        entry = self.workspace_manager.registry.resolve_name_or_id(workspace)
        if entry is None:
            logger.warning(f"Unknown workspace for {end_key} view: {workspace}")
            return False
        if not entry.end_allowed(end_key):
            logger.info(f"{end_key} view rejected: {entry.name} (view={entry.view})")
            return False

        previous_workspace_id = str(getattr(self, self._view_attr(end_key), None) or "")
        if end_key == "cli" and not previous_workspace_id:
            previous_workspace_id = str(self._foreground_session_id or "")

        await self.ensure_workspace_session(entry)
        setattr(self, self._view_attr(end_key), entry.id)

        session_renewed = False
        last_active: float | None = None
        if end_key == "cli":
            # cli view = 兼容层默认指针（foreground_coara / 旧调用点）
            self._foreground_session_id = entry.id
            if not self.workspace_manager.switch(entry.id):
                logger.warning(f"workspace_manager.switch failed for cli view {entry.name}")
            from src.coara.workspace_state import is_session_stale, workspace_session_has_conversation

            last_active = self._workspace_activity_at.get(entry.id)
            did_renew = False
            sess = self._sessions.get(entry.id)
            has_conversation = sess is not None and workspace_session_has_conversation(sess.coara)
            if last_active is not None and is_session_stale(last_active) and has_conversation:
                logger.info(f"Workspace {entry.name} idle beyond timeout; starting new session on cli view switch")
                await self.start_new_session(interrupt_source="workspace_stale")
                did_renew = True
            session_renewed = did_renew or not has_conversation
            self.last_switch_session_renewed = session_renewed
            self.last_switch_last_active = last_active
            # 进程级路径镜像（工具仍用 session.workspace_dir；不再 os.chdir）
            new_dir = Path(entry.resolved_path()).expanduser().resolve()
            self.workspace_dir = new_dir
            self.identity.workspace_dir = new_dir
            if republish_runtime:
                try:
                    from src.coara.workspace_runtime import load_active_runtime, publish_active_runtime

                    fg = self.resolve_view_coara("cli")
                    current = load_active_runtime(self.workspace_manager.coara_home)
                    publish_active_runtime(
                        coara_home=self.workspace_manager.coara_home,
                        workspace_path=new_dir,
                        workspace_name=entry.name,
                        session_id=fg.session_id,
                        coara_id=fg.identity.coara_id,
                        coara_name=fg.identity.name,
                        matrix_enabled=current.matrix_enabled if current else False,
                        matrix_room_id=current.matrix_room_id if current else "",
                    )
                except Exception as exc:
                    logger.warning(f"Failed to republish active runtime after cli view switch: {exc}")
            self.sync_workspace_manager_to_foreground()

        view_coara = self.resolve_view_coara(end_key)
        logger.info(f"{end_key} view switched to {entry.name} (id={entry.id})")
        if emit_event:
            plan_mode = False
            with contextlib.suppress(Exception):
                plan_mode = bool(view_coara.is_plan_mode())
            chrome = {
                "end": end_key,
                "workspace_name": entry.name,
                "workspace_id": entry.id,
                "workspace_dir": str(view_coara.workspace_dir),
                "session_id": view_coara.session_id,
                "coara_id": str(
                    getattr(view_coara, "coara_id", "")
                    or getattr(getattr(view_coara, "identity", None), "coara_id", "")
                    or ""
                ),
                "session_renewed": session_renewed if end_key == "cli" else False,
                # attach 过滤：事件入队早于 rebind，需带 previous 才能推到切换中的连接
                "previous_workspace_id": previous_workspace_id,
                # CLI 状态栏：空间名之外还要跟模型 / 计划模式
                "provider_name": str(getattr(view_coara, "provider_name", "") or ""),
                "model_name": str(getattr(view_coara, "model_name", "") or ""),
                "is_plan_mode": plan_mode,
            }
            self.event_bus.publish(
                TraceEvent(
                    coara_id=self.identity.coara_id,
                    coara_name=self.identity.name,
                    event_type="view_changed",
                    message=f"{end_key} view → {entry.name}",
                    payload=dict(chrome),
                )
            )
            # 兼容旧订阅者：仅 cli view 仍发 workspace_switched（带 end），其它端不跟。
            if end_key == "cli":
                self.event_bus.publish(
                    TraceEvent(
                        coara_id=self.identity.coara_id,
                        coara_name=self.identity.name,
                        event_type="workspace_switched",
                        message=f"Switched to workspace {entry.name}",
                        payload=dict(chrome),
                    )
                )
        return True

    @property
    def foreground_coara(self) -> CoaraBase:
        """兼容别名：CLI view 的 CoaraBase（内核无特权前台）。"""
        return self.resolve_view_coara("cli")

    async def ensure_workspace_session(self, entry: Any) -> Any:
        """Create or reuse a WorkspaceSession for ``entry``."""
        from src.coara.workspace_session import WorkspaceSession

        workspace_id = entry.id
        async with self._sessions_lock:
            session = self._sessions.get(workspace_id)
            if session is None:
                session = await WorkspaceSession.create_for_workspace(entry, self)
                self._sessions[workspace_id] = session
                self._seed_workspace_activity(entry)
                logger.info(f"Created WorkspaceSession for {entry.name} (id={workspace_id})")
        return session

    def _seed_workspace_activity(self, entry: Any) -> None:
        """Seed per-workspace activity from disk session_state.json.

        Always keep the on-disk ``last_updated`` (last message time), even when
        stale — switching or creating a session must not pretend the user spoke
        just now. Only skip seeding when disk has no timestamp.
        """
        from src.coara.workspace_state import load_session_state

        try:
            _sid, last_updated = load_session_state(
                Path(entry.resolved_path()),
                coara_home=self.workspace_manager.coara_home if self.workspace_manager else None,
            )
            if last_updated:
                self._workspace_activity_at[entry.id] = float(last_updated)
        except Exception:
            logger.warning(
                f"load session state for workspace {entry.id} failed; activity clock may under-count", exc_info=True
            )

    def _resolve_session_coara_for_event(self, payload: dict[str, Any]) -> CoaraBase | None:
        """Map an event payload to the workspace session that launched it.

        Matches by ``session_id`` first, then ``coara_id``. Events stamped with
        the Root host id (legacy tasks from before WorkspaceSession) fall back
        to the foreground session. Returns ``None`` when nothing matches — e.g.
        a stale background task from a previous process run.
        """
        session_id = str(payload.get("session_id") or "")
        coara_id = str(payload.get("coara_id") or "")
        for session in self._sessions.values():
            coara = session.coara
            if session_id and coara.session_id == session_id:
                return coara
            if coara_id and coara.identity.coara_id == coara_id:
                return coara
        # 运行中的子智能体也是合法归属：它们发起的后台任务完成通知应回到
        # 子智能体自己的会话（忙时进 continuation 队列），不该升级投到主会话。
        try:
            from src.tools.builtin.delegate.delegate import _ACTIVE_SUBAGENTS, _RUNNING_SUBAGENTS

            seen: set[int] = set()
            for registry in (_RUNNING_SUBAGENTS, _ACTIVE_SUBAGENTS):
                for sub in registry.values():
                    if id(sub) in seen:
                        continue
                    seen.add(id(sub))
                    if session_id and getattr(sub, "session_id", None) == session_id:
                        return sub
                    if coara_id and getattr(getattr(sub, "identity", None), "coara_id", None) == coara_id:
                        return sub
        except Exception:
            logger.debug("scan active subagents for event attribution failed", exc_info=True)
        if coara_id and coara_id == self.identity.coara_id:
            try:
                return self.foreground_coara
            except RuntimeError:
                return None
        # No usable stamp (e.g. launcher had no parent coara): treat as a
        # process-level event and wake the foreground session.
        if (not coara_id or coara_id == "bash-background-runner") and not session_id:
            try:
                return self.foreground_coara
            except RuntimeError:
                return None
        return None

    def sync_workspace_manager_to_foreground(self) -> bool:
        """Align ``workspace_manager`` active workspace with the foreground session."""
        if self.workspace_manager is None:
            return False
        fg_dir = Path(self.foreground_coara.workspace_dir).expanduser().resolve()
        workspace_id = self.workspace_manager.match_path_to_workspace_id(fg_dir)
        if workspace_id is None:
            logger.warning(f"No registered workspace matches foreground workspace {fg_dir}")
            return False
        if self.workspace_manager.active_id == workspace_id:
            return True
        switched = self.workspace_manager.switch(workspace_id)
        if switched:
            entry = self.workspace_manager.active_entry
            logger.info(
                f"Synced workspace_manager active workspace to foreground session "
                f"({entry.name if entry else workspace_id})"
            )
        return switched

    def switch_llm(self, provider_name: str, model: str | None = None, *, origin_source: str = "") -> tuple[str, str]:
        """切换前台 session 的 LLM，并把选择持久化到该会话所在工作空间条目。

        工作空间是 provider/model 的载体：只影响当前空间；未绑定的空间继续
        跟随全局默认（Root 不再镜像，避免把单空间选择泄漏成全局）。
        回合进行中时延迟到回合结束后生效——切换会污染当前回合的上下文格式
        （跨 driver 时 tool_calls 配对不兼容，K3 严格端点立即 400）。
        持久化在 switch_llm_target 内统一完成（写 target 自身空间 last-run）。
        """
        target = self.foreground_coara
        return self.switch_llm_target(target, provider_name, model, origin_source=origin_source)

    def switch_llm_target(
        self,
        target: CoaraBase,
        provider_name: str,
        model: str | None = None,
        *,
        origin_source: str = "",
    ) -> tuple[str, str]:
        """切换指定会话主体的 LLM，并把选择持久化到 target 所在空间条目（last-run）。

        前台与端 pin 视图空间共用（switch_llm / switch_llm_for_workspace /
        命令层 target_coara 分支都走这里）。
        回合进行中：下一次 LLM 调用延迟到回合结束才换连接；**同空间各端 chrome
        立即同步**到新模型。持久化立即写 last-run。目标空间按 target.workspace_dir
        匹配，不依赖 active_id（各端独立视图）。
        """
        from src.core.errors import ConfigError, ProviderNotFoundError
        from src.llm.registry import provider_registry

        if not provider_registry.has(provider_name):
            raise ProviderNotFoundError(provider_name)
        provider = provider_registry.get(provider_name)
        model_name = (model or provider.default_model or "").strip()
        if not model_name:
            raise ConfigError(f"Provider '{provider_name}' has no model specified")

        deferred = bool(getattr(target, "_inside_turn", False))
        has_key = bool(str(getattr(provider, "api_key", "") or "").strip())
        if deferred:
            target._pending_llm_switch = (provider_name, model_name)
            logger.info(f"LLM switch deferred to turn end: {provider_name}/{model_name} (turn in progress)")
            # 显示先同步：本回合仍用旧连接，同空间各端 chrome 立刻跟新模型
            target._emit_trace(
                "llm_switched",
                f"Switched to {provider_name}/{model_name} (deferred)",
                payload={
                    "provider": provider_name,
                    "model": model_name,
                    "deferred": True,
                    "provider_has_key": has_key,
                    "origin_source": origin_source,
                    **self._workspace_llm_chrome_fields(target),
                },
            )
            applied = (provider_name, model_name)
        else:
            applied = target.switch_llm(provider_name, model_name, origin_source=origin_source)
        self._emit_model_switched(
            applied[0],
            applied[1],
            scope="workspace",
            target=target,
            deferred=deferred,
            origin_source=origin_source,
        )
        self._persist_target_llm(target, applied[0], applied[1])
        return applied

    @staticmethod
    def _workspace_llm_chrome_fields(target: CoaraBase) -> dict[str, str]:
        from src.workspace.llm_binding import workspace_llm_chrome_fields

        return workspace_llm_chrome_fields(target)

    def _persist_target_llm(self, target: CoaraBase, provider: str, model: str) -> bool:
        """把 (provider, model) 持久化到 target 自身空间条目（last-run）。"""
        manager = self.workspace_manager
        if manager is None:
            return False
        workspace_dir = str(getattr(target, "workspace_dir", "") or "")
        if not workspace_dir:
            return False
        workspace_id = manager.match_path_to_workspace_id(Path(workspace_dir))
        if workspace_id is None:
            return False
        from src.workspace.llm_binding import bind_entry_llm

        return bind_entry_llm(manager, workspace_id, provider, model)

    def switch_llm_for_workspace(
        self,
        workspace_id: str,
        provider_name: str,
        model: str | None = None,
        *,
        origin_source: str = "",
    ) -> tuple[str, str]:
        """切换 指定工作空间 session 的 LLM（外挂 CLI /model 用），并持久化到该空间条目。

        与 switch_llm（前台语义）同构，但 target 是指定空间的 session coara——
        外挂 CLI pin 的空间非前台，不能走前台方法。回合进行中同样延迟生效。
        """
        session = self._sessions.get(workspace_id)
        if session is None:
            raise RuntimeError(f"Workspace session not found: {workspace_id}")
        return self.switch_llm_target(session.coara, provider_name, model, origin_source=origin_source)

    def switch_llm_global(
        self, provider_name: str, model: str | None = None, *, origin_source: str = ""
    ) -> tuple[str, str]:
        """切换**全局默认** LLM（``/model --global``）：写 llm_preferences 并镜像 Root。

        当前空间未绑定 provider 时前台 session 一并跟随；已绑定的保留自身绑定。
        """
        from src.core.config import config_manager
        from src.core.errors import ConfigError, ProviderNotFoundError
        from src.llm.model_persist import persist_llm_selection
        from src.llm.registry import provider_registry
        from src.workspace.llm_binding import entry_llm_override

        if not provider_registry.has(provider_name):
            raise ProviderNotFoundError(provider_name)
        provider = provider_registry.get(provider_name)
        model_name = (model or provider.default_model or "").strip()
        if not model_name:
            raise ConfigError(f"Provider '{provider_name}' has no model specified")

        manager = self.workspace_manager
        entry = None
        if manager is not None:
            # 前台语义须与 foreground_coara 同源（view pin 优先于默认指针）：
            # 后台空间回合中执行时 manager.active_entry 可能指向后台空间，错位。
            view_id = self.view_workspace_id("cli")
            if view_id:
                entry = manager.registry.get_by_id(view_id)
        if entry_llm_override(entry)[0] is None:
            self.foreground_coara.switch_llm(provider_name, model_name, origin_source=origin_source)
            self._emit_model_switched(
                provider_name,
                model_name,
                scope="global",
                target=self.foreground_coara,
                origin_source=origin_source,
            )

        # Root 镜像 = 未绑定空间新 session 的种子
        self.provider = provider
        self.provider_name = provider_name
        self.model_name = model_name

        persist_llm_selection(config_manager, provider_name, model_name)
        return provider_name, model_name

    def _emit_model_switched(
        self,
        provider_name: str,
        model_name: str,
        *,
        scope: str,
        target: CoaraBase | None = None,
        deferred: bool = False,
        origin_source: str = "",
    ) -> None:
        """Emit ``model_switched``（手机分隔线/面板）；带空间定位供按视图过滤。"""
        payload: dict[str, Any] = {
            "provider": provider_name,
            "model": model_name,
            "scope": scope,
            "deferred": deferred,
            "origin_source": origin_source,
        }
        if target is not None:
            payload.update(self._workspace_llm_chrome_fields(target))
        self.event_bus.publish(
            TraceEvent(
                coara_id=self.identity.coara_id,
                coara_name=self.identity.name,
                event_type="model_switched",
                message=f"Switched model to {provider_name}/{model_name}",
                payload=payload,
            )
        )

    def apply_tools_disabled(self, disabled: set[str] | list[str] | None) -> None:
        """主会话工具开关：应用到 Root 与所有已创建的 WorkspaceSession，运行时即时生效。"""
        names = set(disabled or [])
        self._tool_manager.set_disabled(names)
        # 禁用清单会拼进 system prompt：必须同步失效静态 prompt 缓存，
        # 否则旧缓存仍含已禁用工具（或缺失新禁用段），到期前一直生效
        self._invalidate_prompt_cache()
        for session in self._sessions.values():
            session.coara._tool_manager.set_disabled(names)
            session.coara._invalidate_prompt_cache()

    def get_status(self) -> dict[str, Any]:
        """Return runtime status for the foreground session."""
        return self.foreground_coara.get_status()

    def foreground_active_name(self) -> str | None:
        """Active workspace name aligned with the foreground session."""
        if self.workspace_manager is None:
            return None
        self.sync_workspace_manager_to_foreground()
        from src.workspace.catalog import resolve_foreground_active_name

        return resolve_foreground_active_name(self, self.workspace_manager)

    async def process_message(
        self,
        content: str,
        *,
        trust_level: str = "owner",
        show_tool_summary: bool = True,
        image_blocks: list[dict[str, Any]] | None = None,
        source: str = "",
        turn_id: str | None = None,
    ):
        """代理到前台 session 的 process_message（async generator）。"""
        self.sync_workspace_manager_to_foreground()
        async for chunk in self.foreground_coara.process_message(
            content,
            trust_level=trust_level,
            show_tool_summary=show_tool_summary,
            image_blocks=image_blocks,
            source=source,
            turn_id=turn_id,
        ):
            yield chunk

    def submit_continuation_input(
        self,
        text: str,
        image_blocks: list[dict[str, Any]] | None = None,
        *,
        source: str = "",
        agent_origin: str = "",
        agent_origin_channel: str = "",
    ) -> None:
        """代理到前台 session。"""
        CoaraBase.submit_continuation_input(
            self.foreground_coara,
            text,
            image_blocks=image_blocks,
            source=source,
            agent_origin=agent_origin,
            agent_origin_channel=agent_origin_channel,
        )

    def drain_continuation_inputs(self) -> list[ContinuationInput]:
        """代理到前台 session。"""
        return CoaraBase.drain_continuation_inputs(self.foreground_coara)

    def has_active_turn(self) -> bool:
        """代理到前台 session。"""
        return CoaraBase.has_active_turn(self.foreground_coara)

    def interrupt_current_turn(
        self,
        reason: str = "user_interrupt",
        *,
        interrupt_source: str | None = None,
        cancel_delegates: bool = True,
    ) -> bool:
        """代理到前台 session。"""
        return self.foreground_coara.interrupt_current_turn(
            reason,
            interrupt_source=interrupt_source,
            cancel_delegates=cancel_delegates,
        )

    async def _wait_for_process_lock_release(self, *, timeout_seconds: float = 30.0) -> None:
        """代理到前台 session 的 _process_lock。"""
        await CoaraBase._wait_for_process_lock_release(
            self.foreground_coara,
            timeout_seconds=timeout_seconds,
        )

    async def start_new_session(self, *, interrupt_source: str = "new_session") -> str:
        """代理到前台 session。

        手机画会话分界由 base ``_finalize_new_session`` 的 ``session_started``
        事件驱动（Matrix bot/runner 订阅后推 status），覆盖所有入口——
        不限本方法（web pin 空间 /new、外挂 CLI /new 走 target/session coara
        直调，同样经事件通道推送）。

        Does **not** call ``record_user_activity`` — idle / staleness clocks
        advance only when a real user message arrives on CLI / Web / Matrix.
        """
        return await self.foreground_coara.start_new_session(interrupt_source=interrupt_source)

    async def start_new_session_for_workspace(self, workspace_id: str, *, interrupt_source: str = "new_command") -> str:
        """重开 指定工作空间 的会话（外挂 CLI /new 用），清该空间 message_history。

        与前台 start_new_session 同构，但 target 是指定空间的 session coara——
        外挂 CLI pin 的空间非前台。不推 status（外挂对话按 source 隔离不推手机，
        手机无需感知外挂发起的会话分界）。同时释放该空间的 flow coordinator。
        """
        session = self._sessions.get(workspace_id)
        if session is None:
            raise RuntimeError(f"Workspace session not found: {workspace_id}")
        session_id = await session.coara.start_new_session(interrupt_source=interrupt_source)
        try:
            await session.coara.flow_coordinator.reset()
        except Exception as exc:
            logger.warning(f"flow coordinator reset after attach /new failed: {exc}")
        return session_id

    def resolve_workspace_coara(self, workspace_id: str) -> CoaraBase:
        """按 workspace_id 取该空间会话的 CoaraBase（各端独立视图的路由点）。

        阶段3：端携带目标空间路由消息，不再经「端无关的全局前台指针」。
        attach 链路（pin 空间直取 session.coara）的内核化等价物。
        """
        session = self._sessions.get(workspace_id)
        if session is None:
            raise RuntimeError(f"Workspace session not found: {workspace_id}")
        return session.coara

    @property
    def matrix_view_workspace_id(self) -> str:
        """matrix 手机端当前视图空间（存储字段 ``_matrix_view_workspace_id``）。"""
        return self.view_workspace_id("matrix")

    def resolve_matrix_view_coara(self) -> CoaraBase:
        """matrix 手机端视图空间的 CoaraBase。"""
        return self.resolve_view_coara("matrix")

    @property
    def web_view_workspace_id(self) -> str:
        """web 浏览器端当前视图空间（存储字段 ``_web_view_workspace_id``）。"""
        return self.view_workspace_id("web")

    def resolve_web_view_coara(self) -> CoaraBase:
        """web 浏览器端视图空间的 CoaraBase。"""
        return self.resolve_view_coara("web")

    async def set_web_view_workspace(self, workspace: str) -> bool:
        """web 独立视图切换（委托 :meth:`set_view_workspace`）。"""
        return await self.set_view_workspace("web", workspace)

    @property
    def cli_view_workspace_id(self) -> str:
        """CLI / attach 端当前视图空间（存储字段 ``_cli_view_workspace_id``）。"""
        return self.view_workspace_id("cli")

    def record_user_activity(
        self,
        *,
        push_status: bool = True,
        workspace_id: str | None = None,
    ) -> None:
        """Advance idle/staleness clocks after a real user **message**.

        **Invariant (do not regress):** message-driven clocks move only when
        CLI / Web / Matrix delivers an actual chat (or media) message —
        whichever front spoke **most recently**. Pass ``workspace_id`` of the
        speaking session (端 view / pin)；缺省回退 cli view。Never call from
        switch / ``/new`` / idle auto-renew / slash-only. See CONVENTIONS §活动计时.
        """
        self._last_user_activity_at = time.time()
        stamp_id = (workspace_id or "").strip() or self.view_workspace_id("cli")
        if stamp_id:
            self._workspace_activity_at[stamp_id] = self._last_user_activity_at
        if push_status:
            try:
                from src.coara.mobile_sync import maybe_push_status_for_activity

                maybe_push_status_for_activity(self)
            except Exception as exc:
                logger.debug(f"activity status push skipped: {exc}")

    def record_turn_activity(self, workspace_id: str, *, push_status: bool = True) -> None:
        """Advance idle/staleness clocks when a workspace session's **turn ends**.

        Global idle clock advances when the finished turn belongs to **any**
        end's current view (cli/web/matrix) — multi-end equal, not only cli view.
        """
        prev = self._workspace_activity_at.get(workspace_id)
        now = time.time()
        self._workspace_activity_at[workspace_id] = now
        # 压缩成功派发的 janitor 按回合前 epoch 打标；本回合 turn_end 只推时钟、
        # 对话未新增用户意图——把已维护标记顺延到新 epoch，避免 ~2h 空闲再派
        # 一模一样的维护。用户再发消息走 record_user_activity，不顺延。
        if prev is not None and self._janitor_activity_at.get(workspace_id) == prev:
            self._janitor_activity_at[workspace_id] = now
            self._bump_janitor_activity_disk(workspace_id, now)
        viewed = {
            self.view_workspace_id("cli"),
            self.view_workspace_id("web"),
            self.view_workspace_id("matrix"),
        }
        if workspace_id not in viewed:
            return
        self._last_user_activity_at = now
        if push_status:
            try:
                from src.coara.mobile_sync import maybe_push_status_for_activity

                maybe_push_status_for_activity(self)
            except Exception as exc:
                logger.debug(f"turn activity status push skipped: {exc}")

    def _bump_janitor_activity_disk(self, workspace_id: str, activity_at: float) -> None:
        """顺延磁盘已维护 epoch（转发 JanitorScheduler）。"""
        self._janitor.bump_activity_disk(workspace_id, activity_at)

    async def start_idle_timeout_watcher(self) -> None:
        """Spawn a background task that maintains + renews expired sessions.

        The watcher is started once during Root initialization and cancelled
        during shutdown. Each tick it:

        1. Runs the one-time startup catch-up scan (restart recovery).
        2. Finalizes finished janitor maintenance (race check + session renew).
        3. Scans all cached workspaces for expired sessions and dispatches the
           janitor maintenance agent for each.

        A session is maintained once per activity epoch: dispatch records the
        epoch in memory (single-flight/dedup), and the durable on-disk marker
        is written only when the janitor run completes, so a process killed
        mid-maintenance is re-dispatched by the startup catch-up. The epoch is
        compared on finalize, so a user returning mid-maintenance cancels the
        renew without wasting the ``ws.md`` update.
        """
        if self._idle_timeout_task is not None and not self._idle_timeout_task.done():
            return

        async def _watcher() -> None:
            from src.core.config import config_manager

            cfg = config_manager.config
            timeout = cfg.session.idle_timeout_seconds if cfg else 7200.0
            interval = cfg.session.idle_check_interval_seconds if cfg else 60.0
            while True:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                if timeout > 0:
                    try:
                        await self._janitor_startup_scan()
                        await self._janitor_finalize_pending()
                        await self._janitor_scan_expired(timeout)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(f"janitor watcher tick failed: {exc}")
                try:
                    from src.records.daily_curator import curator_tick

                    await curator_tick(self)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"daily curator tick failed: {exc}")
                try:
                    await self._updates_sweep_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"updates sweep tick failed: {exc}")

        self._idle_timeout_task = asyncio.create_task(_watcher())
        self._bg_tasks.add(self._idle_timeout_task)
        self._idle_timeout_task.add_done_callback(lambda t: self._bg_tasks.discard(t))

    async def _janitor_startup_scan(self) -> None:
        """启动补扫（转发 JanitorScheduler；一进程只跑一遍）。"""
        await self._janitor.startup_scan()

    async def _updates_sweep_tick(self) -> None:
        """纯规则过目：勾掉过期 / 低显著超龄的消息（留处置轨迹），有勾掉则推送红点状态。"""
        store = self._updates_store()
        if store is None:
            return
        swept = await asyncio.to_thread(store.sweep)
        if not swept:
            return
        logger.info(f"Updates sweep dismissed {len(swept)} message(s)")
        try:
            await self._push_workspace_updates_state_to_matrix()
        except Exception as exc:
            logger.debug(f"updates state push after sweep skipped: {exc}")

    async def _janitor_maybe_renew(self, ws_id: str, epoch: float) -> None:
        """空闲续会话（转发 JanitorScheduler；可被 monkeypatch 拦截）。"""
        await self._janitor._maybe_renew(ws_id, epoch)

    async def _janitor_scan_expired(self, timeout: float) -> None:
        """过期扫描派发（转发 JanitorScheduler）。"""
        await self._janitor.scan_expired(timeout)

    # JanitorScheduler 状态转发：既有触点（含 tests）以 root._janitor_* 读写，
    # 落到组件同一份字典上。
    @property
    def _janitor_activity_at(self) -> dict[str, float]:
        return self._janitor.activity_at

    @property
    def _janitor_pending(self) -> dict[str, tuple[str, float]]:
        return self._janitor.pending

    @property
    def _janitor_startup_scan_done(self) -> bool:
        return self._janitor.startup_scan_done

    @_janitor_startup_scan_done.setter
    def _janitor_startup_scan_done(self, value: bool) -> None:
        self._janitor._startup_scan_done = bool(value)

    async def _janitor_finalize_pending(self) -> None:
        """在飞维护收官。

        renew 回调经实例属性解析：既有测试用 monkeypatch.setattr(root,
        "_janitor_maybe_renew", …) 拦截续会话，转发必须留这个钩子。
        """
        from src.coara.workspace_protocol import _janitor_flights

        sched = self._janitor
        for ws_id, (task_id, epoch) in list(sched.pending.items()):
            if not task_id:
                sched.pending.pop(ws_id, None)
                continue
            if task_id == "maintained":
                sched.pending.pop(ws_id, None)
                await self._janitor_maybe_renew(ws_id, epoch)
                continue
            flight = _janitor_flights.get(ws_id)
            task = flight.get("task") if flight else None
            if task is not None and not task.done():
                continue
            sched.pending.pop(ws_id, None)
            await self._janitor_maybe_renew(ws_id, epoch)

    async def stop_idle_timeout_watcher(self) -> None:
        """Cancel the idle-timeout background task and wait for it to finish."""
        task = self._idle_timeout_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _enqueue_to_scheduler(self, msg, priority: str) -> None:
        from src.coara.scheduler import MessagePriority

        self.scheduler.put_nowait(
            msg,
            priority=MessagePriority.URGENT if priority == "urgent" else MessagePriority.NORMAL,
        )

    async def initialize(self) -> None:
        from src.coara.root_lifecycle import initialize_root_services

        await initialize_root_services(self)
        # 事件溯源转正：旧 session_history.json 快照线已删除（不迁移），
        # 启动时清理残留文件（含 .tmp），避免用户误读与磁盘滞留
        try:
            self._purge_legacy_session_history_files()
        except Exception:
            logger.debug("legacy session_history cleanup skipped", exc_info=True)

    def _purge_legacy_session_history_files(self) -> None:
        """删除所有工作空间的旧 session_history.json（一次性清理，幂等）。"""
        wm = self.workspace_manager
        if wm is None:
            return
        try:
            coara_home = Path(wm.coara_home)
        except Exception:
            return
        base = coara_home / "workspaces"
        if not base.is_dir():
            return
        removed = 0
        for hist_dir in base.iterdir():
            if not hist_dir.is_dir():
                continue
            for name in ("session_history.json", "session_history.json.tmp"):
                target = hist_dir / name
                try:
                    if target.is_file():
                        target.unlink()
                        removed += 1
                except OSError:
                    continue
        if removed:
            logger.info(f"removed {removed} legacy session_history file(s) under {base}")

    async def shutdown(self) -> None:
        from src.coara.root_lifecycle import shutdown_root_services

        await shutdown_root_services(self)

    def _subscribe_turn_end_activity(self) -> None:
        """Refresh activity clocks when a workspace session's turn ends.

        Subscribes to ``turn_end`` on the shared EventBus. Only turns from a
        WorkspaceSession's main coara count — subagent and background
        maintenance agents (janitor / daily) have their own coara ids that
        match no session, so they can never refresh the clock they depend on.
        """

        async def _on_turn_end(event: Any) -> None:
            # 后台完成唤醒回合（source=background）不代表用户在场，不刷新
            # 空闲时钟——否则会自动续跑推迟依赖空闲的 janitor / 自动新会话
            payload = getattr(event, "payload", None) or {}
            if str(payload.get("source") or "") == "background":
                return
            coara_id = str(event.coara_id or "")
            if not coara_id:
                return
            for ws_id, session in list(self._sessions.items()):
                session_coara = getattr(session, "coara", None)
                if session_coara is None:
                    continue
                if str(getattr(session_coara.identity, "coara_id", "") or "") != coara_id:
                    continue
                self.record_turn_activity(ws_id)
                await self._persist_activity_epoch(ws_id, session)
                return

        self._subscriptions.append(
            self.event_bus.subscribe(
                _on_turn_end,
                topic="turn_end",
            )
        )

    async def _persist_activity_epoch(self, ws_id: str, session: Any) -> None:
        """Persist the turn-end activity epoch to session_state.json.

        Keeps the on-disk clock on the same basis (last conversation activity)
        so a restart does not resurrect a stale epoch from the last user
        message. Small atomic write, once per turn.
        """
        coara = getattr(session, "coara", None)
        wm = self.workspace_manager
        workspace_dir = getattr(coara, "workspace_dir", None) if coara is not None else None
        session_id = getattr(coara, "session_id", "") if coara is not None else ""
        if wm is None or not workspace_dir or not session_id:
            return
        from src.coara.workspace_state import save_session_state

        try:
            await asyncio.to_thread(
                save_session_state,
                Path(workspace_dir),
                str(session_id),
                coara_home=wm.coara_home,
                last_updated=self._workspace_activity_at.get(ws_id),
            )
        except Exception as exc:
            logger.debug(f"persist activity epoch failed for {ws_id}: {exc}")

    def _subscribe_background_task_notifications(self) -> None:
        """Subscribe to background task completion events.

        当 bash 或 agent 后台任务完成时，注入一条 ``<后台结果>``
        into the conversation so the LLM is aware and can decide to call
        TaskStore / completion inject for detailed results.
        """

        async def _on_background_task_complete(event: TraceEvent) -> None:
            payload = event.payload or {}
            task_id = str(payload.get("task_id") or "unknown")

            # 历史遗留：kind=workflow 的完成事件来自已剥离的 WDL 引擎，
            # 一律忽略（引擎终态由 WDL 软件自管）。
            if str(payload.get("kind") or "") == "workflow":
                return

            from src.coara.injections.background_injector import (
                build_background_completion_message,
                build_background_completion_reminder,
            )

            reminder = build_background_completion_reminder(
                task_id=payload.get("task_id", "unknown"),
                status=payload.get("status", "completed"),
                has_error=bool(payload.get("has_error", False)),
                description=str(payload.get("description") or ""),
                exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
                error=str(payload["error"]) if payload.get("error") else None,
                result=str(payload.get("result_full") or payload.get("result_preview") or ""),
                log_path=str(payload.get("log_path") or ""),
                kind=str(payload.get("kind") or ""),
            )
            message_content = build_background_completion_message(reminder)

            try:
                # Route by the *launching* session — same pattern as the
                # shell-output wake below. A task launched in workspace A must
                # not be injected into workspace B after a mid-turn switch.
                target = self._resolve_session_coara_for_event(payload)
                if target is None:
                    # 发起会话已消亡（如子智能体收官后任务才完成）：升级投到
                    # 当前主会话，否则完成通知无人消费、任务看似永远 running
                    try:
                        target = self.foreground_coara
                    except RuntimeError:
                        logger.debug(f"Background task completion dropped: no live session for task '{task_id}'")
                        return
                    logger.info(f"Background task '{task_id}' completion escalated to foreground session")

                # 延迟 /new 清理进行中：此时会话即将被抹掉，continuation 会随旧
                # 回合 abort 被清空，唤醒回合注入的历史也会被清掉——通知降级落
                # 工作空间收件箱（与 reminder 同管道），不丢不注入将被清空的会话。
                if self._deferred_new_session_active(target):
                    self._park_background_completion_in_updates(
                        target,
                        task_id,
                        message_content,
                        status=str(payload.get("status") or "completed"),
                        description=str(payload.get("description") or ""),
                    )
                elif target.has_active_turn() or target._process_lock.locked():
                    # Session is busy — inject via continuation_inputs so the
                    # current turn's next ReAct iteration picks it up. Do NOT
                    # await `target._process_lock` here: that lock is held for
                    # the entire turn, so awaiting it would block until the
                    # turn ends, and by then no new iteration would run to
                    # process the injected message (the original "stuck" bug).
                    # agent_origin：子智能体结果的派发来源（delegate 调用时
                    # 用户输入端），供结果回显按端归位（谁派发、结果给谁看）。
                    target.submit_continuation_input(
                        message_content,
                        agent_origin=(
                            str(payload.get("subagent_origin") or "") or str(payload.get("origin_source") or "")
                        ),
                        agent_origin_channel=str(payload.get("subagent_origin_channel") or ""),
                    )
                    # 正文回投统一走 EndRegistry 流式路由（matrix/web 各自在
                    # 回合/跟话注入时已登记通道），不再有收尾补发镜像。
                    logger.info(f"Background task '{task_id}' completion injected via continuation input")
                else:
                    # 自动继续语义：完成通知像接续输入一样推动 LLM 处理——唤醒
                    # 一个新回合，LLM 看到结果继续推进（如拿到全量测试结果后
                    # 接着提交）。消息由 process_message 内部 append 并持久化；
                    # 输出经 chat_chunk trace 流到 CLI（source=background 无标签
                    # 镜像，像普通消息一样显示）。
                    await self._awaken_background_turn(
                        target,
                        task_id,
                        message_content,
                        origin_source=str(payload.get("origin_source") or ""),
                    )
            except Exception as exc:
                logger.warning(f"Failed to inject background task reminder: {exc}")

        self._subscriptions.append(
            self.event_bus.subscribe(
                _on_background_task_complete,
                topic="background_task_complete",
            )
        )

        # Also wire the BashBackgroundRunner to use this EventBus
        from src.background.bash_runner import BashBackgroundRunner

        BashBackgroundRunner().set_event_bus(self.event_bus)

        # 后台 agent 完成通知同一条总线：会话级 CoaraBase 没有 event_bus，
        # 不接线则 background_task_complete 永不发布、完成结果不注入主会话
        from src.coara.background_agent import BackgroundAgentManager

        BackgroundAgentManager().set_event_bus(self.event_bus)

        from src.tools.builtin.media.video_scheduler import VideoScheduler

        VideoScheduler().set_event_bus(self.event_bus)
        VideoScheduler().start()

    async def _awaken_background_turn(
        self,
        target: CoaraBase,
        task_id: str,
        content: str,
        *,
        origin_source: str = "",
    ) -> None:
        """后台任务完成后唤醒目标会话跑一个新回合。

        完成通知像接续输入一样推动 LLM 处理：空闲会话直接启动 process_message
        （source=background，CLI 以系统行样式镜像），LLM 看到结果继续推进。
        忙时会话由调用方走 continuation_inputs（当前回合下一迭代自然看到）。

        回复回投端 = ``origin_source``（发起后台任务的回合）或会话上最近一次
        真实用户输入端（``_last_user_input_source``）。规则：CLI 常显 + 输入端可见。
        """
        # 会话可能在本事件发布后、这里真正执行前进入延迟 /new 清理：再查一次，
        # 注入将被清空的会话会丢通知——降级落工作空间收件箱。
        if self._deferred_new_session_active(target):
            self._park_background_completion_in_updates(target, task_id, content)
            return
        reply_source = self._resolve_reply_input_source(target, origin_source)
        task = asyncio.create_task(self._consume_awakened_turn(target, task_id, content, origin_source=reply_source))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    def _deferred_new_session_active(target: CoaraBase) -> bool:
        """目标会话是否正被回合内 /new 的延迟清理接管（与 process_message 门禁同一判定）。"""
        deferred = getattr(target, "_deferred_new_session_task", None)
        return deferred is not None and not deferred.done()

    def _park_background_completion_in_updates(
        self,
        target: CoaraBase,
        task_id: str,
        content: str,
        *,
        status: str = "completed",
        description: str = "",
    ) -> None:
        """后台完成通知降级落工作空间收件箱（与 reminder 到点同管道）。

        用于延迟 /new 清理期间：通知既不注入将被清空的会话，也不进会随旧
        回合 abort 被清空的 continuation 队列。前台待处理处可见，不丢。
        """
        try:
            from src.tools.builtin.ws.updates_ops import resolve_updates_store

            store = resolve_updates_store(target)
            if store is None:
                logger.warning(f"Background task '{task_id}' completion dropped: no updates store")
                return
            workspace = ""
            wm = getattr(target, "workspace_manager", None)
            workspace_dir = getattr(target, "workspace_dir", None)
            if wm is not None and workspace_dir:
                workspace = str(wm.name_for_path(Path(workspace_dir)) or "")
            if not workspace:
                logger.warning(f"Background task '{task_id}' completion dropped: no workspace to receive it")
                return
            stored = store.append(
                workspace=workspace,
                source_id=f"background:{task_id}",
                event_type="background_task_complete",
                dedupe_key=f"background_task_complete:{task_id}",
                text=content,
                payload={
                    "title": f"后台任务完成 · {description or task_id}",
                    "task_id": task_id,
                    "status": status,
                    "message": content,
                },
                type="notification",
                salience="high",
                source_kind="internal_report",
            )
            if stored is not None:
                logger.info(f"Background task '{task_id}' completion parked in workspace updates ({workspace})")
        except Exception as exc:
            logger.warning(f"Failed to park background completion for task '{task_id}': {exc}")

    def _resolve_reply_input_source(self, target: CoaraBase, origin_source: str = "") -> str:
        """后台唤醒/注入后，LLM 回复应回投到哪个输入端。"""
        from src.coara.turn_source import LAUNCH_SOURCES

        # 勿对空串 normalize（会塌成 cli），否则永远吃不到归属端。
        origin = str(origin_source or "").strip().lower()
        if origin in LAUNCH_SOURCES:
            return origin
        so = getattr(target, "session_origin", None)
        so_source = str((so or {}).get("source") or "").strip().lower()
        if so_source in LAUNCH_SOURCES:
            return so_source
        last = str(getattr(target, "_last_user_input_source", "") or "").strip().lower()
        if last in LAUNCH_SOURCES:
            return last
        return origin or last or ""

    async def _consume_awakened_turn(
        self,
        target: CoaraBase,
        task_id: str,
        content: str,
        *,
        origin_source: str = "",
    ) -> None:
        """消费唤醒回合的 process_message 流（chunks 经 trace 送各端显示）。"""
        try:
            # web 发起的后台任务：唤醒回合输出经 web_server 的 TurnStream 流回
            # 浏览器（web 端由 stream_awakened_turn 经 TurnStream 流回；matrix/
            # event 端经上方 EndRegistry 通道逐帧投递）。
            if origin_source == "web":
                web_server = self._web_server
                stream_fn = getattr(web_server, "stream_awakened_turn", None) if web_server is not None else None
                if stream_fn is not None:
                    # target 即发起空间的会话 coara：视图落盘锚到其 workspace_dir，
                    # 不随用户当前视图漂移（P1-3b）。
                    launch_ws = getattr(target, "workspace_dir", None)
                    await stream_fn(
                        target, content, origin_source=origin_source, task_id=task_id, workspace_dir=launch_ws
                    )
                    return
                logger.debug(f"Background task '{task_id}' awakened turn: no web server, fallback to trace only")
            # web origin 且 web_server 缺席：source 用 web 而非 background——
            # background 源 CLI 不镜像（各端显示独立），source="web" 至少
            # 保证 trace 回投路径对本端开放（浏览器走 TurnStream 回放）。
            run_source = "web" if origin_source == "web" else "background"
            # matrix/event 发起的唤醒回合：与 matrix 正常回合同款 EndRegistry
            # 通道注册——正文/diff 逐帧流回房间，不再有收尾镜像补发特例。
            # 通道挂在 background 段键下（段 source 即路由键）。
            _sender = None
            _sess_id = str(getattr(target, "session_id", "") or "")
            end_registry = getattr(self, "end_registry", None)
            if origin_source in ("matrix", "event") and end_registry is not None:
                mnotify = getattr(self, "matrix_notify", None)
                _send_to_user = getattr(mnotify, "send_to_user", None) if mnotify is not None else None
                if _send_to_user is not None:

                    async def _awakened_matrix_sender(frame: dict) -> None:
                        if frame.get("kind") == "tool":
                            # 与 response_stream / 跟话通道同款：包 [COARA_TOOL]，
                            # 勿把裸 label 当正文（手机端会当噪音隐藏）。
                            try:
                                from src.matrix_client.tool_bridge import build_matrix_tool_message

                                message = build_matrix_tool_message(frame)
                                if message:
                                    await _send_to_user(message)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "awakened matrix tool envelope send failed",
                                    exc_info=True,
                                )
                            return
                        if frame.get("kind") == "diff":
                            return  # diff 卡片是交互端富文本，手机端不投
                        chunk = str(frame.get("text") or "")
                        if chunk.strip():
                            await _send_to_user(chunk)

                    end_registry.register("background", _awakened_matrix_sender, _sess_id)
                    _sender = _awakened_matrix_sender
            try:
                async for _ in target.process_message(content, source=run_source):
                    pass
            finally:
                if end_registry is not None and _sender is not None:
                    end_registry.unregister("background", _sender, _sess_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Background task '{task_id}' awakened turn failed: {exc}")

    async def switch_workspace(
        self,
        workspace: str,
        *,
        republish_runtime: bool = True,
    ) -> bool:
        """兼容入口：等价于 ``set_view_workspace("cli", …)``。

        端 UX / LLM 应优先 ``set_view_workspace``。不再 vault-lock、不再 ``os.chdir``、
        不再因 attach 占用而拒绝（共看是一等能力；attach↔attach 互斥仍由占用表管）。
        """
        return await self.set_view_workspace(
            "cli",
            workspace,
            republish_runtime=republish_runtime,
        )

    def _on_registry_changed(self, action: str, entry: Any) -> None:
        """Fan out workspace_registry_changed events; prune sessions of removed workspaces.

        Wired as ``WorkspaceManager.on_registry_changed`` in root_lifecycle. Covers
        both in-process writes (ws tool, /ws rename) and cross-process CLI edits
        detected via ``reload_if_stale``.
        """
        workspace_id = str(getattr(entry, "id", "") or "")
        workspace_name = str(getattr(entry, "name", "") or "")

        if action == "removed" and workspace_id:
            if workspace_id == self._foreground_session_id:
                logger.warning(
                    f"Foreground workspace {workspace_name} removed from registry externally; "
                    "keeping cached session alive to avoid breaking the running process"
                )
            else:
                session = self._sessions.get(workspace_id)
                if session is not None:
                    coara = getattr(session, "coara", None)
                    busy = bool(coara is not None and coara.is_turn_busy())
                    if busy:
                        logger.warning(
                            f"Workspace {workspace_name} removed from registry while its session is busy; "
                            "cached session kept in memory (process restart clears it)"
                        )
                    else:
                        self._sessions.pop(workspace_id, None)
                        self._workspace_activity_at.pop(workspace_id, None)
                        logger.info(f"Pruned cached session of removed workspace {workspace_name}")

        self.event_bus.publish(
            TraceEvent(
                coara_id=self.identity.coara_id,
                coara_name=self.identity.name,
                event_type="workspace_registry_changed",
                message=f"Workspace registry changed: {action} {workspace_name}",
                payload={
                    "action": action,
                    "workspace_id": workspace_id,
                    "workspace_name": workspace_name,
                },
            )
        )

    async def _on_update_stored(self, message: WorkspaceUpdate) -> None:
        await self._push_workspace_updates_to_matrix(message)

    def _updates_store(self) -> WorkspaceUpdatesStore | None:
        """工作空间动态存储：优先复用 EventSourceManager 的实例。"""
        if self.event_source_manager is not None:
            return self.event_source_manager.updates_store()
        if self.workspace_manager is None:
            return None
        return WorkspaceUpdatesStore(
            self.workspace_manager.coara_home,
            registry=getattr(self.workspace_manager, "registry", None),
        )

    async def _on_janitor_dispatch(self, update: WorkspaceUpdate) -> None:
        """handle=janitor：动态已入库；LLM review 已退役，见 dispatch_janitor_message_review。"""
        from src.coara.workspace_protocol import dispatch_janitor_message_review

        await dispatch_janitor_message_review(self, update)

    def _updates_workspaces_payload(self) -> list[dict[str, Any]]:
        if self.event_source_manager is None:
            return []
        # 与 _restore_workspace_activity_at 同判：workspace_manager 可为 None
        # （无注册表的裸启动），coara_home 直接取会 AttributeError
        if self.workspace_manager is None:
            return []
        return build_updates_workspaces_payload(
            coara_home=self.workspace_manager.coara_home,
            workspace_dir=self.workspace_dir,
        )

    def _pending_updates_payload(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """消息条目摘要（手机抽屉上区）：跨空间高显著未读。"""
        store = self._updates_store()
        if store is None:
            return []
        items: list[dict[str, Any]] = []
        for msg in store.pending(limit=limit):
            summary = (msg.display_text or msg.text or "").strip()
            items.append(
                {
                    "message_id": msg.message_id,
                    "workspace": msg.workspace,
                    "title": msg.title,
                    "salience": msg.salience,
                    "type": msg.type,
                    "summary": summary[:200],
                    "created_at": msg.created_at,
                }
            )
        return items

    async def _push_workspace_updates_to_matrix(self, message: WorkspaceUpdate) -> None:
        body = format_updates_payload(
            payload_type="workspace_updates",
            workspaces=self._updates_workspaces_payload(),
            pending=self._pending_updates_payload(),
            message={
                "message_id": message.message_id,
                "workspace": message.workspace,
                "source_id": message.source_id,
                "event_type": message.event_type,
                "type": message.type,
                "payload_ref": message.payload_ref,
                "status": message.status,
                "salience": message.salience,
                "title": message.title,
                "text": message.text,
                "display_text": message.display_text,
                "created_at": message.created_at,
            },
        )
        await self._send_updates_payload(body)

    async def _push_workspace_updates_state_to_matrix(self) -> None:
        """仅推送工作空间红点状态（无新动态）——已读水位推进后让 App 及时清零。"""
        body = format_updates_payload(
            payload_type="workspace_updates",
            workspaces=self._updates_workspaces_payload(),
            pending=self._pending_updates_payload(),
        )
        await self._send_updates_payload(body)

    async def _send_updates_payload(self, body: str) -> None:
        sent = await self.matrix_notify.send_to_user(body)
        if not sent:
            logger.debug("Workspace updates not sent to Matrix (no notify room / bot offline)")

    async def _scheduler_consumer_loop(self):
        from src.coara.inbound_router import run_scheduler_consumer_loop

        await run_scheduler_consumer_loop(self)


async def create_root_coara(
    workspace_dir: Path | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    workspace_alias: str | None = None,
) -> RootCoara:
    """Create and initialize the root coara."""
    root = RootCoara(
        workspace_dir=workspace_dir,
        provider_name=provider_name,
        model=model,
        workspace_alias=workspace_alias,
    )
    await root.initialize()
    return root
