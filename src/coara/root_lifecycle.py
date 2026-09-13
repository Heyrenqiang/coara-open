"""RootCoara service bootstrap and teardown (extracted from root.py)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.coara.base import CoaraBase
from src.core.logger import logger
from src.llm.service import llm_service

if TYPE_CHECKING:
    from src.coara.root import RootCoara


async def _bind_foreground_workspace_session(root: RootCoara) -> None:
    """Bind the startup workspace as a WorkspaceSession; pin all end views.

    Root stays host-only. Sessions are equal; each end gets the same initial
    view pointer (cli/web/matrix) so none silently 「跟随」a global foreground.
    """
    if root.workspace_manager is None:
        raise RuntimeError("workspace_manager is required to bind foreground session")
    workspace_id = root.workspace_manager.active_id
    if not workspace_id:
        raise RuntimeError("No active workspace to bind as foreground session")
    entry = root.workspace_manager.registry.get_by_id(workspace_id)
    if entry is None:
        entry = root.workspace_manager.active_entry
    if entry is None:
        raise RuntimeError(f"No registry entry for startup workspace {workspace_id}")

    await root.ensure_workspace_session(entry)
    root._foreground_session_id = entry.id
    root._cli_view_workspace_id = entry.id
    root._web_view_workspace_id = entry.id
    root._matrix_view_workspace_id = entry.id
    fg = root.foreground_coara
    root.workspace_dir = Path(fg.workspace_dir).expanduser().resolve()
    root.identity.workspace_dir = root.workspace_dir

    from src.coara.workspace_runtime import publish_active_runtime

    publish_active_runtime(
        coara_home=root.workspace_manager.coara_home,
        workspace_path=root.workspace_dir,
        workspace_name=entry.name,
        session_id=fg.session_id,
        coara_id=fg.identity.coara_id,
        coara_name=fg.identity.name,
    )
    logger.info(
        f"Startup views pinned to WorkspaceSession {entry.name} "
        f"(id={entry.id}, session={fg.session_id})"
    )


async def initialize_root_services(root: RootCoara) -> None:
    from src.core.config import config_manager, get_config

    config = await get_config()

    from src.workspace.manager import WorkspaceManager

    root.workspace_manager = WorkspaceManager(
        root.workspace_dir,
        coara_home=config.coara_home,
        workspace_alias=root._requested_workspace,
    )
    await root.workspace_manager.initialize()
    root.workspace_manager.on_registry_changed = root._on_registry_changed
    # Operational root = CLI cwd (one directory = one workspace).
    root.workspace_dir = root.workspace_manager.startup_cwd.resolve()
    root.identity.workspace_dir = root.workspace_dir

    from src.runtime.tool_output_store import prune_stale_tool_outputs

    await asyncio.to_thread(
        prune_stale_tool_outputs,
        workspace_dir=root.workspace_dir,
        coara_home=config.coara_home,
    )

    # gomatrix 接入层托管（married sidecar）：手机连接的根基，尽量早拉起。
    # 端口上已有健康实例时接入之（adopt），否则 spawn 看护。见 docs/接入层-gomatrix.md
    if getattr(config, "matrix", None) is not None and getattr(config.matrix, "host_enabled", False):
        try:
            from src.core.coara_home import resolve_coara_home
            from src.matrix_host import GoMatrixHost

            root.matrix_host = GoMatrixHost(resolve_coara_home(root.workspace_dir, config.coara_home), config.matrix)
            await root.matrix_host.start()
        except Exception as exc:
            logger.warning(f"matrix host start failed: {exc}")

    if config.vault_enabled:
        from src.core.coara_home import resolve_coara_home
        from src.tools.builtin.vault.vault import VaultTool
        from src.vault import VaultService

        coara_home = resolve_coara_home(root.workspace_dir, config.coara_home)
        root.vault_service = VaultService(coara_home)
        await root.vault_service.initialize()
        if root.vault_service.try_unlock_from_env():
            logger.info("Vault unlocked from COARA_VAULT_PASSWORD")
        root.register_tool(VaultTool(parent_coara=root))

    # 账户活跃保活：有凭证且距上次在线校验超 12h 时后台 check 一次（令牌在
    # 服务端 7 天不活跃即过期；日常使用即续期，用户无感）。失败静默——
    # 离线宽限语义在 gate 内消化。
    # 可选能力（账户保活 / 遥测）统一从扩展点取实现：开源发行版没有实现包时
    # 这两段自然什么都不做，内核代码不用改。
    try:
        from src.core.coara_home import resolve_coara_home
        from src.ext import account_activity_pinger

        _pinger = account_activity_pinger()
        if _pinger is not None:
            _account_home = resolve_coara_home(root.workspace_dir, config.coara_home)

            async def _account_activity_ping() -> None:
                with contextlib.suppress(Exception):
                    await _pinger(_account_home)

            _ping_task = asyncio.create_task(_account_activity_ping(), name="account-activity-ping")
            root._bg_tasks.add(_ping_task)
            _ping_task.add_done_callback(root._bg_tasks.discard)
    except Exception:
        pass

    try:
        from src.core.coara_home import resolve_coara_home as _resolve_home_telemetry
        from src.ext import telemetry_service

        _service = telemetry_service(root, _resolve_home_telemetry(root.workspace_dir, config.coara_home))
        if _service is not None:
            root.telemetry_service = _service
            await _service.start()
    except Exception as exc:
        logger.warning(f"telemetry init failed: {exc}")

    memory_cfg = getattr(config, "records", None)
    from src.core.coara_home import ensure_user_layout, resolve_coara_home, user_paths
    from src.records.store import RecordsStore
    from src.tools.builtin.records.local_search import LocalSearchTool

    coara_home = resolve_coara_home(root.workspace_dir, config.coara_home)
    ensure_user_layout(coara_home)
    records_dir = user_paths(coara_home).records_dir
    agent_enabled = bool(memory_cfg is not None and getattr(memory_cfg, "enabled", False))
    root.records_store = RecordsStore(records_dir, agent_enabled=agent_enabled)
    root.register_tool(
        LocalSearchTool(store=root.records_store, parent_coara=root),
        replace=True,
    )
    logger.info(f"Records store at {records_dir} (agent={'on' if agent_enabled else 'off'})")

    # 存量迁移：记录空间与 daily 合并前的独立 .internal/records 旧条目一次性清除
    # （幂等）。必须先于 daily 登记执行：daily 条目要改名「记录」，旧条目不清掉会
    # 触发 ensure_internal_workspace 的防重名保护、改名被跳过（本轮实测踩坑）。
    try:
        from src.workspace.system_spaces import cleanup_legacy_records_workspace

        cleanup_legacy_records_workspace(root)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"legacy records workspace cleanup failed: {exc}")

    # daily 是 internal 系统工作空间：启动即注册在册（而非等首次定时 dispatch 惰性
    # 注册），让用户从启动就能 @daily 切换接入。注册是幂等的（ensure_internal_workspace），
    # 会话仍在 dispatch / @daily 时惰性创建。curator 未启用或 records_store 缺失时跳过。
    if agent_enabled:
        try:
            from src.records.daily_curator import ensure_daily_workspace_entry

            ensure_daily_workspace_entry(root)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"daily workspace registration skipped: {exc}")

    # 用量是 internal 展示空间（docs/空间模型与内容注册表.md §3 试点）：内容是全局
    # 账本聚合（report 型，非磁盘文件），门面=display，主页=web 用量页。幂等注册。
    try:
        from src.workspace.usage_space import ensure_usage_workspace_entry

        ensure_usage_workspace_entry(root)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"usage workspace registration failed: {exc}")

    # 系统空间统一登记（侧边栏空间化）：配置/消息/记录登记为 internal（目录指向
    # coara_home/workspaces/.internal/<name> 占位）。
    # 注：工作流不是空间——WDL 是一种文件类型，引擎+画布页是独立的 WDL 软件工作台，
    # 不占空间席位，这里不再登记/补身份。
    try:
        from src.workspace.system_spaces import ensure_system_workspace_entries

        ensure_system_workspace_entries(root)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"system workspace registration failed: {exc}")

    await root.bootstrap_tools()

    # plan_mode / workflow / skill / ws — also registered on each WorkspaceSession.
    # ReminderService stays on Root; ReminderTool goes on sessions.
    from src.coara.tool_registry import register_root_scoped_tools

    raw_cfg = getattr(config_manager, "_raw_config", {}) or {}
    reminders_cfg = raw_cfg.get("reminders") or {}
    from src.reminders.service import ReminderService, format_reminder_fire_text
    from src.tools.builtin.manifest import REMINDER_TOOL_TYPE

    async def _on_reminder_fire(record) -> None:
        """提醒到点 → 内容落收件箱（high 显著，挂前台待处理），不自动跑回合。"""
        try:
            from src.tools.builtin.ws.updates_ops import resolve_updates_store

            store = resolve_updates_store(root)
            if store is None:
                return
            workspace = ""
            try:
                workspace = str(root.foreground_active_name() or "")
            except Exception:
                workspace = ""
            if not workspace:
                entries = list(root.workspace_manager.registry.document.workspaces.values())
                workspace = str(entries[0].name) if entries else ""
            if not workspace:
                logger.warning(f"Reminder '{record.id}' fired but no workspace to receive it")
                return
            text = format_reminder_fire_text(record)
            stored = store.append(
                workspace=workspace,
                source_id=f"reminder:{record.id}",
                event_type="reminder_due",
                dedupe_key=f"reminder_due:{record.id}:{record.next_run_at}",
                text=text,
                payload={
                    "title": f"定时提醒 · {text[:60]}",
                    "reminder_id": record.id,
                    "message": record.message,
                },
                type="reminder",
                salience="high",
                source_kind="internal_report",
            )
            if stored is not None:
                push = getattr(root, "_push_workspace_updates_to_matrix", None)
                if push is not None:
                    await push(stored)
        except Exception as exc:
            logger.warning(f"Reminder '{record.id}' inbox write failed: {exc}")

    root.reminder_service = ReminderService(
        root.workspace_manager.coara_home,
        enqueue=root._enqueue_to_scheduler,
        coara_id=root.identity.coara_id,
        timezone=str(reminders_cfg.get("timezone", "Asia/Shanghai")),
        tick_seconds=float(reminders_cfg.get("tick_seconds", 1.0)),
        on_fire=_on_reminder_fire,
    )
    root.register_tool(REMINDER_TOOL_TYPE(root.reminder_service))

    register_root_scoped_tools(root)
    try:
        await root.reminder_service.start()
    except Exception as exc:
        logger.warning(f"Failed to start reminder service: {exc}")

    await root.load_skills()
    await CoaraBase.initialize(root)

    # 启动时 API key 审计：检查 web_search / media / LLM 的 key，状态变更才写系统消息。
    # 放在 CoaraBase.initialize 之后、前台会话绑定之前——env 已加载（get_config 时），
    # 且不阻塞工具注册（media 是否注册由 runtime_tools 单独按 key 判断）。
    try:
        from src.coara.api_key_audit import audit_api_keys

        await asyncio.to_thread(
            audit_api_keys,
            config_manager,
            root.workspace_manager.coara_home,
        )
    except Exception as exc:
        logger.warning(f"API key audit failed: {exc}")

    # Foreground conversation agent is always a WorkspaceSession CoaraBase.
    await _bind_foreground_workspace_session(root)

    await root._purge_legacy_audit_logs()

    root.set_trace_sink(root.event_bus.publish)

    try:
        from src.runtime.usage_collector import attach_usage_collector

        attach_usage_collector(root)
    except Exception as exc:
        logger.warning(f"Failed to attach usage collector: {exc}")

    # Subscribe to background task completion events for proactive notification.
    root._subscribe_background_task_notifications()
    # Activity clocks also advance at turn end (last LLM API call basis).
    root._subscribe_turn_end_activity()

    # Recover orphaned subagent/background-task records from a previous
    # session — across every registered workspace, not only the current one.
    # A kill/power-loss otherwise leaves records stuck in running_* forever
    # (and resume only accepts cancelled/failed).
    def _registered_workspace_dirs() -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()

        def _add(path: Path | str | None) -> None:
            if path is None:
                return
            try:
                resolved = Path(path).expanduser().resolve()
            except OSError:
                return
            key = str(resolved).lower()
            if key in seen:
                return
            seen.add(key)
            dirs.append(resolved)

        _add(root.workspace_dir)
        _add(Path.cwd())
        try:
            for entry in root.workspace_manager.registry.list_active():
                _add(entry.resolved_path())
        except Exception as exc:
            logger.warning(f"Workspace registry scan for stale recovery failed: {exc}")
        return dirs

    try:
        from src.coara.subagent_store import SubagentStore

        coara_home = getattr(root.workspace_manager, "coara_home", None)
        recovered_agents = 0
        for ws_dir in _registered_workspace_dirs():
            try:
                recovered_agents += SubagentStore(ws_dir, coara_home=coara_home).recover_stale_running()
            except Exception as exc:
                logger.warning(f"Failed to recover subagent records for {ws_dir}: {exc}")
        if recovered_agents:
            logger.info(f"Recovered {recovered_agents} stale running subagent record(s)")
    except Exception as exc:
        logger.warning(f"Failed to recover subagent records: {exc}")

    try:
        from src.background.task_store import TaskStore
        from src.core.coara_home import resolve_coara_home

        coara_dir = resolve_coara_home(root.workspace_dir, getattr(root.workspace_manager, "coara_home", None))
        recovered = TaskStore.recover_stale_running(coara_dir, *_registered_workspace_dirs(), coara_home=coara_dir)
        if recovered:
            # recover 自建实例直写磁盘：清掉进程级实例缓存，
            # 否则缓存实例的内存 _cache 会把已恢复的 failed 永久显示成 running
            from src.background.task_store_paths import reset_task_store_cache

            reset_task_store_cache()
            logger.info(f"Recovered {recovered} stale TaskStore background task(s)")
    except Exception as exc:
        logger.warning(f"Failed to recover TaskStore records: {exc}")

    # 后台子智能体任务台账清算：进程被杀时仍在跑的任务写入各空间动态，提示用户中断
    try:
        from src.coara.subagent_task_ledger import report_interrupted_tasks

        coara_home = getattr(root.workspace_manager, "coara_home", None)
        for ws_dir in _registered_workspace_dirs():
            try:
                await asyncio.to_thread(report_interrupted_tasks, root, ws_dir, coara_home)
            except Exception as exc:
                logger.warning(f"Failed to report interrupted subagent tasks for {ws_dir}: {exc}")
    except Exception as exc:
        logger.warning(f"Failed to report interrupted subagent tasks: {exc}")

    task = asyncio.create_task(root._scheduler_consumer_loop())
    root._bg_tasks.add(task)
    task.add_done_callback(root._bg_tasks.discard)

    await root.start_idle_timeout_watcher()

    from src.event_sources.manager import EventSourceManager

    raw_cfg = getattr(config_manager, "_raw_config", {}) or {}
    events_cfg = raw_cfg.get("events") or {}
    root.event_source_manager = EventSourceManager(
        root.workspace_manager,
        coara_id=root.identity.coara_id,
        webhook_host=str(events_cfg.get("webhook_host", "127.0.0.1")),
        webhook_port=int(events_cfg.get("webhook_port", 8765)),
        on_update_stored=root._on_update_stored,
        on_janitor_dispatch=root._on_janitor_dispatch,
    )
    try:
        await root.event_source_manager.start()
        for row in root.event_source_manager.list_status():
            if row.get("webhook_url") and row.get("running"):
                logger.info(f"Webhook event source ready: POST {row['webhook_url']}")
    except Exception as exc:
        logger.warning(f"Failed to start event sources: {exc}")

    logger.info("Root coara Scheduler started.")
    logger.info(f"Root coara initialized: {root.identity.name}")


async def shutdown_root_services(root: RootCoara) -> None:
    """Shutdown all root services with maximum parallelism.

    Three phases: interrupt + persist, then all services in parallel,
    then final cleanup (unsubscribe, CoaraBase.shutdown, usage collector,
    clear active runtime). Worst-case timeout: ~5 seconds.
    """
    await root.stop_idle_timeout_watcher()

    # 正常退出也要 cancel 后台任务/子智能体：否则它们被进程退出强断、TaskStore
    # 留 running，下次启动 recover_stale_running 全标 failed + 台账报「被中断」——
    # 把正常退出误报成中断。cancel_all 幂等，区分「正常退出」与「被强杀」。
    try:
        from src.background.bash_runner import BashBackgroundRunner
        from src.coara.background_agent import BackgroundAgentManager
        from src.tools.builtin.media.video_scheduler import VideoScheduler

        BashBackgroundRunner().cancel_all()
        BackgroundAgentManager().cancel_all()
        await BackgroundAgentManager().wait_all(timeout=3.0)
        VideoScheduler().stop()
    except Exception as exc:
        logger.debug(f"cancel background tasks during shutdown skipped: {exc}")

    try:
        # Interrupt every busy WorkspaceSession — not only the foreground.
        # A departing space may still be finishing its turn after a switch.
        busy_sessions: list[Any] = []
        for _wid, session in list(root._sessions.items()):
            coara = getattr(session, "coara", None)
            if coara is None:
                continue
            if coara.is_turn_busy() or coara._process_lock.locked():
                busy_sessions.append(coara)
        if not busy_sessions and (root.has_active_turn() or root.foreground_coara._process_lock.locked()):
            busy_sessions.append(root.foreground_coara)
        for coara in busy_sessions:
            coara.interrupt_current_turn("shutdown", interrupt_source="shutdown")
        for coara in busy_sessions:
            await coara._wait_for_process_lock_release(timeout_seconds=2.0)
    except Exception as exc:
        logger.warning(f"Error interrupting active turn during shutdown: {exc}")

    try:
        for _workspace_id, session in root._sessions.items():
            await session.persist_to_disk()
    except Exception as exc:
        logger.warning(f"Error persisting workspace session state: {exc}")

    # --- Phase 2: parallel shutdown of all independent services ---

    async def _shutdown_scheduler() -> None:
        try:
            await asyncio.wait_for(root.scheduler.shutdown(), timeout=2.0)
        except TimeoutError:
            logger.warning("Scheduler shutdown timed out after 2s")
        except Exception as exc:
            logger.warning(f"Error shutting down scheduler: {exc}")

    async def _cancel_bg_tasks() -> None:
        if not root._bg_tasks:
            return
        try:
            _done, pending = await asyncio.wait(root._bg_tasks, timeout=2.0)
            for task in pending:
                task.cancel()
            if pending:
                # gather 也要有时限：被 cancel 的任务若不响应取消，裸 gather
                # 会把整个关停流程挂死
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
            root._bg_tasks.clear()
        except Exception as exc:
            logger.warning(f"Error cleaning up background tasks: {exc}")

    async def _shutdown_vault() -> None:
        if root.vault_service is None:
            return
        try:
            await root.vault_service.shutdown()
        except Exception as exc:
            logger.warning(f"Error shutting down vault service: {exc}")

    async def _shutdown_reminders() -> None:
        if root.reminder_service is None:
            return
        try:
            await root.reminder_service.stop()
        except Exception as exc:
            logger.warning(f"Error stopping reminder service: {exc}")

    async def _shutdown_event_sources() -> None:
        if root.event_source_manager is None:
            return
        try:
            await root.event_source_manager.stop()
        except Exception as exc:
            logger.warning(f"Error stopping event sources: {exc}")

    async def _close_providers() -> None:
        try:
            await llm_service.close()
        except Exception as e:
            logger.warning(f"Error closing providers: {e}")

    async def _shutdown_telemetry() -> None:
        service = getattr(root, "telemetry_service", None)
        if service is None:
            return
        try:
            await service.stop()
        except Exception as exc:
            logger.warning(f"Error stopping telemetry: {exc}")

    async def _shutdown_matrix_host() -> None:
        host = getattr(root, "matrix_host", None)
        if host is None:
            return
        try:
            await host.stop()
        except Exception as exc:
            logger.warning(f"Error stopping matrix host: {exc}")

    # Run ALL independent shutdowns in parallel. The wall-clock time is
    # max(scheduler≈0s, bg_tasks≈0s, vault≈0s, reminders≈0s,
    # event_sources≈0.1s, providers≈0.1s) instead of the sum.
    results = await asyncio.gather(
        _shutdown_scheduler(),
        _cancel_bg_tasks(),
        _shutdown_vault(),
        _shutdown_reminders(),
        _shutdown_event_sources(),
        _close_providers(),
        _shutdown_telemetry(),
        _shutdown_matrix_host(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Error during root service shutdown: {result}")

    # --- Phase 3: final cleanup ---

    for sub in root._subscriptions:
        sub.unsubscribe()
    root._subscriptions.clear()

    await CoaraBase.shutdown(root)

    try:
        await root.event_bus.shutdown()
    except Exception as exc:
        logger.warning(f"Error shutting down event bus: {exc}")

    try:
        from src.runtime.usage_collector import shutdown_usage_collector

        shutdown_usage_collector(root)
    except Exception as exc:
        logger.warning(f"Failed to shutdown usage collector: {exc}")

    try:
        import os

        from src.coara.workspace_runtime import clear_active_runtime

        if root.workspace_manager is not None:
            clear_active_runtime(root.workspace_manager.coara_home, pid=os.getpid())
    except Exception as exc:
        logger.warning(f"Error clearing active workspace runtime: {exc}")
