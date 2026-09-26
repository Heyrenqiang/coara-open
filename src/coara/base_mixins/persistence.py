"""持久化/录像带 mixin（O 组）"""

from __future__ import annotations

from pathlib import Path

from src.core.logger import logger


class PersistenceMixin:
    """持久化/录像带：会话事件记录器重建 / in-flight 标记 / 会话落盘 / 遗留审计清理"""

    def _rebuild_session_log(self) -> None:
        """按当前 session_id 重建会话事件记录器（唯一事实源，无开关）；失败静默为 None"""
        self._session_log = None
        try:
            from src.session_log.recorder import build_recorder
            from src.session_log.store import workflow_session_log_path

            log_path = None
            tape = self._session_tape
            if self.is_flow_subject() or tape == "flow":
                log_path = workflow_session_log_path("flow", coara_home=self._session_state_coara_home())
            elif tape == "engine":
                log_path = workflow_session_log_path("engine", coara_home=self._session_state_coara_home())

            self._session_log = build_recorder(
                workspace_dir=self.workspace_dir,
                session_id=self.session_id,
                coara_id=self.identity.coara_id,
                coara_name=self.identity.name,
                agent_kind=self._session_agent_kind or "main",
                coara_home=self._session_state_coara_home(),
                log_path=log_path,
            )
        except Exception:
            logger.exception("Session log recorder init failed for {}", self.workspace_dir)
            self._session_log = None

    async def _purge_legacy_audit_logs(self) -> None:
        """Remove deprecated session tool-audit directories for this workspace."""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        from src.core.config import config_manager
        from src.core.error_log import purge_session_audit_logs

        coara_home = config_manager.config.coara_home if config_manager._config else None
        purge_session_audit_logs(workspace_dir=self.workspace_dir, coara_home=coara_home)

    def _session_state_coara_home(self) -> Path | None:
        """Resolve coara_home for workspace session files (state/history/marker)."""
        from src.core.config import config_manager

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager._config.coara_home
        wm = getattr(self, "workspace_manager", None)
        if wm is not None and getattr(wm, "coara_home", None) is not None:
            coara_home = wm.coara_home
        return coara_home

    def _mark_turn_in_flight(self, turn_id: str) -> None:
        """Write the turn in-flight marker (atomic small file)."""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import mark_flow_turn_in_flight, mark_turn_in_flight

            if self.is_flow_subject():
                mark_flow_turn_in_flight(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                    turn_id=turn_id,
                )
            else:
                mark_turn_in_flight(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                    turn_id=turn_id,
                )
        except Exception:
            logger.debug("Failed to mark turn in-flight for {}", self.workspace_dir, exc_info=True)

    def _clear_turn_in_flight(self) -> None:
        """Clear the turn in-flight marker after the turn's history is persisted."""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import clear_flow_turn_in_flight, clear_turn_in_flight

            if self.is_flow_subject():
                clear_flow_turn_in_flight(
                    self.workspace_dir,
                    coara_home=self._session_state_coara_home(),
                )
            else:
                clear_turn_in_flight(
                    self.workspace_dir,
                    coara_home=self._session_state_coara_home(),
                )
        except Exception:
            logger.debug("Failed to clear turn in-flight marker for {}", self.workspace_dir, exc_info=True)

    def persist_session_to_disk(self) -> None:
        """落盘会话状态：事件日志同步（唯一事实源）+ 会话索引"""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import save_flow_session_state, save_session_state

            # janitor 是附着在每个用户空间上的维护机制（拿目标空间上下文跑维护回合），
            persona_name = str(getattr(self.identity.persona, "name", "") or "").strip().lower()
            if persona_name == "janitor":
                pass
            elif self.is_flow_subject():
                save_flow_session_state(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                )
            else:
                save_session_state(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                )
        except Exception:
            logger.exception("Failed to persist session state for {}", self.workspace_dir)
        recorder = self._session_log
        if recorder is None:
            return
        try:
            recorder.sync_history(list(self.message_history))
            recorder.record_session_meta({"usage_snapshot": self._llm_usage_snapshot.to_dict()})
        except Exception:
            logger.exception("Failed to sync session event log for {}", self.workspace_dir)
