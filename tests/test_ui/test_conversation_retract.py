"""UI transcript retract（中途切空间）经 L1 history/shadow 的投影语义。

旧平表靠 retract_messages_for_turn 改写文件删行；收编后 L1 append-only，
sync_history 检出前缀分叉写 history/shadow，投影不再产出被截掉的尾部。
"""

from __future__ import annotations

from pathlib import Path

from src.core.types import Message, MessageRole
from src.session_log.conversation_projection import iter_conversation_rows
from src.session_log.recorder import SessionLogRecorder
from src.session_log.store import resolve_session_log_path


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


def test_retracted_tail_hidden_by_shadow(tmp_path: Path) -> None:
    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_id="c1",
        coara_name="root",
        agent_kind="main",
    )
    history = [_user("keep me"), _user("切换到B"), _assistant("ws 工具链")]
    recorder.sync_history(list(history))

    # strip_ws_switch_tail 清掉切换尾部后对账：前缀分叉 → history/shadow
    recorder.sync_history([_user("keep me")])

    rows = list(iter_conversation_rows(resolve_session_log_path(tmp_path), include_agent_kinds={"main"}))
    assert [r["content"] for r in rows] == ["keep me"]


def test_shadow_retract_then_new_turn_projects_cleanly(tmp_path: Path) -> None:
    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_id="c1",
        coara_name="root",
        agent_kind="main",
    )
    recorder.record_turn_start("turn-keep", source="cli")
    recorder.sync_history([_user("earlier")])
    recorder.record_turn_start("turn-switch", source="cli")
    recorder.sync_history([_user("earlier"), _user("切换到B")])

    # 回撤切换 turn 后继续新回合：影子之后的行正常投影
    recorder.sync_history([_user("earlier")])
    recorder.record_turn_start("turn-next", source="web")
    recorder.sync_history([_user("earlier"), _user("新回合"), _assistant("新答复")])

    rows = list(iter_conversation_rows(resolve_session_log_path(tmp_path), include_agent_kinds={"main"}))
    assert [(r["content"], r["source"]) for r in rows] == [
        ("earlier", "cli"),
        ("新回合", "web"),
        ("新答复", "web"),
    ]
