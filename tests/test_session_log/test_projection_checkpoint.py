"""投影检查点（session_log/checkpoint）正确性：与全量投影逐条一致。

金标准：每个场景下，检查点路径（replay_session_projection 走检查点）的结果
必须与全量投影（直接 read_events + project_session）的消息 content / seqs /
usage_snapshot 完全一致。正确性优先于速度。
"""

from __future__ import annotations

from pathlib import Path

from src.core.types import Message, MessageRole
from src.session_log.project import project_session
from src.session_log.store import append_events, read_events
from src.session_log.types import (
    EVENT_HISTORY_SHADOW,
    EVENT_SESSION_META,
    build_event,
    message_to_event,
)


def _msg_event(msg: Message, seq: int, session_id: str, ts: float = 1.0) -> dict:
    ev = message_to_event(message=msg, seq=seq, session_id=session_id, ts=ts)
    assert ev is not None
    return ev


def _full_projection(path: Path, session_id: str):
    return project_session(read_events(path, session_id=session_id))


def _assert_same_projection(cp_proj, full_proj) -> None:
    assert [m.content for m in cp_proj.messages] == [m.content for m in full_proj.messages]
    assert [m.role for m in cp_proj.messages] == [m.role for m in full_proj.messages]
    assert list(cp_proj.seqs) == list(full_proj.seqs)
    assert cp_proj.usage_snapshot == full_proj.usage_snapshot


def _replay(workspace: Path, session_id: str):
    """走恢复主入口（含检查点逻辑）。"""
    from src.coara.workspace_state import replay_session_projection

    return replay_session_projection(workspace, session_id, coara_home=None)


def _write_events_directly(tmp_path: Path, workspace: Path, session_id: str, events: list[dict]) -> Path:
    """绕过 cwd/coara_home 解析，直接写录像带到 workspace 对应位置。"""
    from src.session_log.store import resolve_session_log_path

    path = resolve_session_log_path(workspace, coara_home=None)
    append_events(path, events)
    return path


def _seed_session(workspace: Path, session_id: str, count: int) -> list[dict]:
    """造一个 session 的纯追加事件（user/assistant 交替）。"""
    events: list[dict] = []
    for i in range(count):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        events.append(
            _msg_event(Message(role=role, content=f"msg{i}"), seq=i + 1, session_id=session_id)
        )
    return events


def test_checkpoint_miss_then_hit(tmp_path: Path, monkeypatch) -> None:
    """无检查点 → 全量 + 写检查点；第二次恢复 → 命中检查点（零增量）。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    _write_events_directly(tmp_path, workspace, sid, _seed_session(workspace, sid, 6))

    first = _replay(workspace, sid)
    # 与全量投影比对
    from src.session_log.store import resolve_session_log_path

    tape = resolve_session_log_path(workspace, coara_home=None)
    full = _full_projection(tape, sid)
    _assert_same_projection(first, full)

    # 检查点已写入
    from src.session_log.checkpoint import checkpoint_path, load_checkpoint

    cp_file = checkpoint_path(tape.parent)
    assert cp_file.is_file()
    cp = load_checkpoint(tape.parent, sid)
    assert cp is not None and len(cp.messages) == 6

    # 第二次恢复：命中检查点，结果一致
    second = _replay(workspace, sid)
    _assert_same_projection(second, full)


def test_checkpoint_increment_pure_append(tmp_path: Path, monkeypatch) -> None:
    """检查点后有纯追加（正常对话）→ 增量拼接，结果与全量一致。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    base = _seed_session(workspace, sid, 6)
    _write_events_directly(tmp_path, workspace, sid, base)
    _replay(workspace, sid)  # 写检查点（水位 seq=6）

    # 追加 2 条新消息（seq 7,8）
    extra = [
        _msg_event(Message(role=MessageRole.USER, content="msg6"), seq=7, session_id=sid),
        _msg_event(Message(role=MessageRole.ASSISTANT, content="msg7"), seq=8, session_id=sid),
    ]
    _write_events_directly(tmp_path, workspace, sid, extra)

    got = _replay(workspace, sid)
    from src.session_log.store import resolve_session_log_path

    full = _full_projection(resolve_session_log_path(workspace, coara_home=None), sid)
    _assert_same_projection(got, full)
    assert len(got.messages) == 8


def test_checkpoint_invalidated_by_history_shadow(tmp_path: Path, monkeypatch) -> None:
    """检查点后有压缩（history/shadow）→ 回退全量重建，结果与全量一致。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    _write_events_directly(tmp_path, workspace, sid, _seed_session(workspace, sid, 6))
    _replay(workspace, sid)  # 检查点水位 seq=6

    # 模拟压缩：影子作废尾部（keep_until_seq=2），重写尾部新内容
    from src.session_log.store import resolve_session_log_path

    tape = resolve_session_log_path(workspace, coara_home=None)
    shadow = build_event(
        kind=EVENT_HISTORY_SHADOW,
        seq=7,
        session_id=sid,
        payload={"keep_until_seq": 2, "replaced_count": 4, "reason": "history_prefix_replaced"},
    )
    rewritten = [
        _msg_event(Message(role=MessageRole.ASSISTANT, content="压缩后的概况"), seq=8, session_id=sid),
        _msg_event(Message(role=MessageRole.USER, content="msg_after"), seq=9, session_id=sid),
    ]
    append_events(tape, [shadow, *rewritten])

    got = _replay(workspace, sid)
    full = _full_projection(tape, sid)
    _assert_same_projection(got, full)
    # 影子截断：尾部被替换
    assert got.messages[-1].content == "msg_after"


def test_checkpoint_session_mismatch(tmp_path: Path, monkeypatch) -> None:
    """检查点 session 与目标不符（/new 后）→ 全量。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    _write_events_directly(tmp_path, workspace, "s1", _seed_session(workspace, "s1", 4))
    _replay(workspace, "s1")  # 写 s1 检查点

    # 新会话 s2（新事件段）
    s2_events = _seed_session(workspace, "s2", 3)
    from src.session_log.store import resolve_session_log_path

    tape = resolve_session_log_path(workspace, coara_home=None)
    # s2 的 seq 需接续（不重 1..3，避免与 s1 撞 seq 排序）
    s2_events = [dict(e, seq=i + 100) for i, e in enumerate(s2_events)]
    append_events(tape, s2_events)

    got = _replay(workspace, "s2")
    full = _full_projection(tape, "s2")
    _assert_same_projection(got, full)
    assert len(got.messages) == 3


def test_checkpoint_corrupted_falls_back(tmp_path: Path, monkeypatch) -> None:
    """检查点 JSON 损坏 → 回退全量。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    _write_events_directly(tmp_path, workspace, sid, _seed_session(workspace, sid, 4))
    _replay(workspace, sid)

    from src.session_log.checkpoint import checkpoint_path
    from src.session_log.store import resolve_session_log_path

    tape = resolve_session_log_path(workspace, coara_home=None)
    checkpoint_path(tape.parent).write_text("{ not valid json", encoding="utf-8")

    got = _replay(workspace, sid)
    full = _full_projection(tape, sid)
    _assert_same_projection(got, full)


def test_checkpoint_repeated_recovery_consistency(tmp_path: Path, monkeypatch) -> None:
    """多次启动（反复读检查点）结果与全量投影始终一致——模拟跨进程重启。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    _write_events_directly(tmp_path, workspace, sid, _seed_session(workspace, sid, 5))
    from src.session_log.store import resolve_session_log_path

    tape = resolve_session_log_path(workspace, coara_home=None)
    full = _full_projection(tape, sid)
    # 连续恢复 3 次（每次走检查点），结果都必须等于全量
    for _ in range(3):
        got = _replay(workspace, sid)
        _assert_same_projection(got, full)


def test_checkpoint_usage_snapshot(tmp_path: Path, monkeypatch) -> None:
    """usage_snapshot 经 session/meta 事件投影，检查点保留最后一条。"""
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path
    sid = "s1"
    events = _seed_session(workspace, sid, 4)
    events.append(
        build_event(
            kind=EVENT_SESSION_META,
            seq=5,
            session_id=sid,
            payload={"usage_snapshot": {"input_tokens": 1234}},
        )
    )
    _write_events_directly(tmp_path, workspace, sid, events)

    got = _replay(workspace, sid)
    from src.session_log.store import resolve_session_log_path

    full = _full_projection(resolve_session_log_path(workspace, coara_home=None), sid)
    _assert_same_projection(got, full)
    assert got.usage_snapshot == {"input_tokens": 1234}
