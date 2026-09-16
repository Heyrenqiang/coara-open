"""持久化/录像带 mixin（O 组）。

宿主为 CoaraBase（src/coara/base.py），属性（_session_log / _session_tape /
_session_agent_kind / session_id / audit_session_id / identity / workspace_dir /
_llm_usage_snapshot / message_history）在宿主 __init__ 初始化
（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

from pathlib import Path

from src.core.logger import logger


class PersistenceMixin:
    """持久化/录像带：会话事件记录器重建 / in-flight 标记 / 会话落盘 / 遗留审计清理"""

    def _rebuild_session_log(self) -> None:
        """按当前 session_id 重建会话事件记录器（唯一事实源，无开关）；失败静默为 None。

        录像带跟主体不跟执行目录：主会话→工作空间带；FlowRoot 与会话内 flow
        节点→工作流系统带；引擎节点→引擎系统带（跨进程隔离）。
        """
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
        """Write the turn in-flight marker (atomic small file).

        与 ``persist_session_to_disk`` 同门禁：只有拥有工作空间会话文件的
        会话才写标记。标记写入失败不得阻断回合（仅降级为恢复时无注记）
        Flow 写独立 ``flow_turn_in_flight.json``，不覆盖主会话标记。
        """
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
        """落盘会话状态：事件日志同步（唯一事实源）+ 会话索引。

        消息事件经 ``sync_history`` 前缀对账写入事件日志；usage 快照走
        ``session/meta`` 事件。session_history.json 快照线已删除。
        Safe to call from worker threads (blocking disk IO). Owner/user-facing
        sessions only — ephemeral subagents do not own workspace session files.

        Flow 第二主体：写同一条 ``session_events.jsonl``，但索引走
        ``flow_session_state.json``，**禁止**写主会话 ``session_state.json``。
        """
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import save_flow_session_state, save_session_state

            # janitor 是附着在每个用户空间上的维护机制（拿目标空间上下文跑维护回合），
            # 只同步录像带，不占有会话索引：写了 session_state 会顶掉主会话索引、把
            # last_updated 推新，启动补扫据此判定「会话有更新」而反复触发自己（死循环）。
            # daily 不在此列——它已是独立的 internal 系统空间主体（user_facing=True，
            # 自己的 workspace_dir），该写自己的 session_state（自己的会话索引与录像带）。
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
