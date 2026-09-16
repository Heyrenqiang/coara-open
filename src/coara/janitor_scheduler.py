"""JanitorScheduler — 工作空间概况（ws.md）维护与空闲会话续期的编排组件。

从 RootCoara 抽离（台账 #5）：启动补扫 / 在飞收官 / 过期扫描 / 空闲续会话
四件编排，状态（已维护 epoch、在飞表、启动补扫单飞）自持。Root 仅保留
同名转发（测试与既有触点依赖），watcher tick 在 Root。

调度纪律：
- 一个会话一生只维护一次（磁盘已维护标记跨重启去重）
- 磁盘标记只在 janitor 完成时写入，进程中途被杀由启动补扫重派
- 有未收官后台任务的空间豁免派发与续会话（通知要落回原会话）
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


class JanitorScheduler:
    """Per-Root janitor orchestration: startup catch-up, finalize, expire scan, renew."""

    def __init__(self, root: RootCoara) -> None:
        self._root = root
        # 按空间记录已维护的 activity epoch，防重复触发
        self._activity_at: dict[str, float] = {}
        # 维护进行中：workspace_id -> (task_id, activity_epoch)
        self._pending: dict[str, tuple[str, float]] = {}
        # 启动补扫只跑一次
        self._startup_scan_done: bool = False

    @property
    def activity_at(self) -> dict[str, float]:
        return self._activity_at

    @property
    def pending(self) -> dict[str, tuple[str, float]]:
        return self._pending

    @property
    def startup_scan_done(self) -> bool:
        return self._startup_scan_done

    async def startup_scan(self) -> None:
        """One-time catch-up scan at process start.

        Restarted processes may have missed maintenance for sessions that
        expired while the process was down. Scan registered workspaces, and
        for stale ones with real history, dispatch a background janitor to
        refresh ``ws.md``. No session renew here — disk recovery already opens
        a fresh session when the user switches in.
        """
        if self._startup_scan_done:
            return
        self._startup_scan_done = True
        root = self._root
        wm = root.workspace_manager
        if wm is None:
            return

        from src.coara.workspace_protocol import (
            dispatch_janitor_background,
            has_real_conversation_in_history,
            session_history_path,
        )
        from src.coara.workspace_state import is_session_stale, load_session_state
        from src.workspace.types import WorkspaceStatus

        try:
            entries = wm.registry.list_active()
        except Exception as exc:
            logger.warning(f"janitor startup scan: registry unavailable: {exc}")
            return
        for entry in entries:
            if entry.status != WorkspaceStatus.ACTIVE:
                continue
            try:
                ws_dir = entry.resolved_path()
            except Exception:
                continue
            last_sid, last_updated = load_session_state(ws_dir, coara_home=wm.coara_home)
            if not last_sid or not is_session_stale(last_updated):
                continue
            history = session_history_path(str(ws_dir), str(wm.coara_home))
            if not has_real_conversation_in_history(history, session_id=last_sid):
                continue
            if self._activity_at.get(entry.id) == last_updated:
                continue
            from src.coara.workspace_protocol import load_janitor_activity

            if load_janitor_activity(str(ws_dir), str(wm.coara_home)) == last_updated:
                # 上个进程已为这个会话完成维护（跨重启去重：一个会话一生只维护一次）
                self._activity_at[entry.id] = last_updated
                continue
            # Catch-up only refreshes ws.md; do not enqueue renew (disk recovery
            # already opens a fresh session when the user switches in).
            # 派发时只记内存单飞标记；磁盘已维护标记由完成钩子写入——进程中途
            # 被杀时标记不存在，下次启动 catch-up 会重派该 epoch。
            task_id = await dispatch_janitor_background(
                root,
                workspace_name=entry.name,
                workspace_dir=str(ws_dir),
                coara_home=str(wm.coara_home),
                activity_epoch=last_updated,
            )
            if task_id:
                self._activity_at[entry.id] = last_updated

    async def scan_expired(self, timeout: float) -> None:
        """Scan cached workspaces and dispatch maintenance for expired sessions."""
        from src.coara.workspace_protocol import (
            consume_janitor_retry,
            dispatch_janitor_background,
        )
        from src.coara.workspace_state import (
            is_session_stale,
            workspace_session_has_conversation,
        )

        root = self._root
        wm = root.workspace_manager
        coara_home = str(wm.coara_home) if wm else ""
        for ws_id, session in list(root._sessions.items()):
            activity_at = root._workspace_activity_at.get(ws_id)
            if activity_at is None or not is_session_stale(activity_at):
                continue
            if ws_id in self._pending:
                continue
            coara = getattr(session, "coara", None)
            if coara is None or coara.has_active_turn():
                continue
            if not workspace_session_has_conversation(coara):
                continue
            # 有未收官后台任务的空间不派发维护也不 /new——任务收官通知要落回
            # 原会话。不清已维护标记（正常冷却不重复派发），任务收官后活动
            # 时钟随其回合/通知移动，若仍 stale 由 retry 闸门补派放行。
            if self._workspace_has_active_background(ws_id):
                continue
            if self._activity_at.get(ws_id) == activity_at:
                # 失败退避：同 epoch 上次维护失败且退避窗口已过时，消费补派名额
                # 放行一次（每 epoch 进程内最多一次，重派仍走单飞/冷却闸门）
                from src.coara.workspace_protocol import janitor_failure_pending

                retry_dir = str(getattr(coara, "workspace_dir", "") or "")
                if retry_dir and consume_janitor_retry(retry_dir, activity_at):
                    self._activity_at.pop(ws_id, None)
                elif retry_dir and janitor_failure_pending(retry_dir, activity_at):
                    # 失败待退避/待补派：不武装 renew、不重派（等窗口）
                    continue
                else:
                    # 真正已维护：只武装自动 /new，不再派一模一样的 janitor
                    self._pending[ws_id] = ("maintained", activity_at)
                    continue
            workspace_dir = str(getattr(coara, "workspace_dir", "") or "")
            workspace_name = str(getattr(session, "workspace_name", "") or "")
            if not workspace_dir or not workspace_name:
                continue
            if coara_home:
                from src.coara.workspace_protocol import load_janitor_activity

                if load_janitor_activity(workspace_dir, coara_home) == activity_at:
                    self._activity_at[ws_id] = activity_at
                    # 磁盘已维护：跳过 LLM 管家，仍走空闲续会话
                    self._pending[ws_id] = ("maintained", activity_at)
                    continue
            task_id = await dispatch_janitor_background(
                root,
                workspace_name=workspace_name,
                workspace_dir=workspace_dir,
                coara_home=coara_home,
                activity_epoch=activity_at,
            )
            if task_id:
                # 内存单飞/去重标记照常；磁盘已维护标记由 janitor 完成钩子写入
                self._activity_at[ws_id] = activity_at
                self._pending[ws_id] = (task_id, activity_at)

    def bump_activity_disk(self, workspace_id: str, activity_at: float) -> None:
        """顺延磁盘已维护 epoch（与内存已维护表对齐）。"""
        root = self._root
        session = root._sessions.get(workspace_id)
        coara = getattr(session, "coara", None) if session is not None else None
        workspace_dir = str(getattr(coara, "workspace_dir", "") or "")
        wm = root.workspace_manager
        if not workspace_dir or wm is None:
            return
        try:
            from src.coara.workspace_protocol import save_janitor_activity

            save_janitor_activity(workspace_dir, str(wm.coara_home), float(activity_at))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"janitor activity bump failed for {workspace_id}: {exc}")

    async def _maybe_renew(self, ws_id: str, epoch: float) -> None:
        """Renew the session if the workspace's activity epoch is unchanged."""
        root = self._root
        if root._workspace_activity_at.get(ws_id) != epoch:
            logger.info(
                f"janitor: workspace {ws_id} resumed before maintenance finished; "
                f"keeping the session, ws.md update retained"
            )
            return
        session = root._sessions.get(ws_id)
        if session is None:
            return
        coara = getattr(session, "coara", None)
        if coara is None:
            return
        # 有未收官后台任务（bash/子智能体/工作流）的空间豁免本次自动 /new——
        # 完成通知要落回原会话，新会话会丢上下文。清掉已维护标记让扫描重新
        # 派发（扫描层的豁免闸门会在任务收官前一直拦着），不更新活动时钟。
        if self._workspace_has_active_background(ws_id):
            logger.info(f"Idle renew skipped for workspace {ws_id}: background task(s) still running")
            self._activity_at.pop(ws_id, None)
            return
        logger.info(f"Idle timeout reached for workspace {ws_id}; starting a new session")
        try:
            await coara.start_new_session(interrupt_source="idle_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"janitor renew new-session failed for {ws_id}: {exc}")
            return
        if ws_id != root._foreground_session_id:
            return
        # 手机画新会话分隔线由 session_started 事件统一推送（Matrix 订阅层）。
        # Keep the original session_auto_new trace for UI.
        from src.coara.workspace_state import session_stale_seconds

        timeout_minutes = int(session_stale_seconds() / 60)
        new_sid = coara.session_id
        root._emit_trace(
            "session_auto_new",
            f"Started a new session after {timeout_minutes} minutes of inactivity",
            payload={
                "reason": "idle_timeout",
                "session_id": new_sid,
                "previous_activity_at": epoch,
                "idle_timeout_minutes": timeout_minutes,
            },
        )

    def _workspace_has_active_background(self, workspace_id: str) -> bool:
        """该工作空间是否有未收官的后台任务（bash / 后台子智能体 / 工作流）。

        数据以 TaskStore 磁盘持久化记录为准（进程重启后仍在）；
        janitor/daily 系统派发任务不算（不该拦 idle /new）。
        """
        session = self._root._sessions.get(workspace_id)
        coara = getattr(session, "coara", None) if session is not None else None
        workspace_dir = getattr(coara, "workspace_dir", None) if coara is not None else None
        if not workspace_dir:
            return False
        try:
            from src.background.task_store_paths import task_store_for_coara

            store = task_store_for_coara(coara)
            for record in store.list_active():
                if (record.subagent_type or "").strip() in ("janitor", "daily"):
                    continue
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"background-task check failed for workspace {workspace_id}: {exc}")
        return False
