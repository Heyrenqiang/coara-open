"""Tests for session event log: storage, projection, compression archive."""

from __future__ import annotations

from pathlib import Path

from src.core.types import Message, MessageRole, ToolCall
from src.session_log.archive import archive_compressed_messages
from src.session_log.project import derive_messages
from src.session_log.recorder import build_recorder
from src.session_log.store import (
    append_event,
    append_events,
    count_events,
    last_seq,
    read_events,
)
from src.session_log.types import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_COMPACTION_SUMMARY,
    EVENT_SYSTEM_NOTE,
    EVENT_TOOL_RESULT,
    EVENT_USER_MESSAGE,
    build_event,
    message_to_event,
)


def _event(*, kind: str, seq: int, session_id: str = "s1", payload: dict | None = None, ts: float = 1.0):
    return build_event(kind=kind, seq=seq, session_id=session_id, payload=payload or {}, ts=ts)


def test_append_read_and_seq(tmp_path: Path) -> None:
    path = tmp_path / "session_events.jsonl"
    assert last_seq(path) == 0
    append_events(
        path,
        [
            _event(kind=EVENT_USER_MESSAGE, seq=1, payload={"content": "hi"}),
            _event(kind=EVENT_ASSISTANT_MESSAGE, seq=2, payload={"content": "yo"}),
        ],
    )
    append_event(path, _event(kind="turn/end", seq=3))
    assert last_seq(path) == 3
    assert count_events(path) == 3
    events = read_events(path)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert events[0]["kind"] == EVENT_USER_MESSAGE


def test_message_to_event_roles() -> None:
    user = message_to_event(message=Message(role=MessageRole.USER, content="hi"), seq=1, session_id="s1")
    assert user is not None and user["kind"] == EVENT_USER_MESSAGE

    assistant = message_to_event(
        message=Message(
            role=MessageRole.ASSISTANT,
            content="ok",
            reasoning_content="think",
            tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "a"})],
        ),
        seq=2,
        session_id="s1",
    )
    assert assistant is not None and assistant["kind"] == EVENT_ASSISTANT_MESSAGE
    assert assistant["payload"]["reasoning_content"] == "think"
    assert assistant["payload"]["tool_calls"][0]["name"] == "read"

    result = message_to_event(
        message=Message(
            role=MessageRole.TOOL_RESULT,
            content="content",
            tool_call_id="c1",
            name="read",
        ),
        seq=3,
        session_id="s1",
    )
    assert result is not None and result["kind"] == EVENT_TOOL_RESULT
    assert result["payload"]["tool_call_id"] == "c1"

    note = message_to_event(message=Message(role=MessageRole.SYSTEM, content="sys"), seq=4, session_id="s1")
    assert note is not None and note["kind"] == EVENT_SYSTEM_NOTE


def test_derive_messages_basic() -> None:
    events = [
        _event(kind=EVENT_USER_MESSAGE, seq=1, payload={"content": "hi"}),
        _event(
            kind=EVENT_ASSISTANT_MESSAGE,
            seq=2,
            payload={"content": "yo", "tool_calls": [{"id": "c1", "name": "grep", "arguments": {"q": "x"}}]},
        ),
        _event(kind=EVENT_TOOL_RESULT, seq=3, payload={"tool_call_id": "c1", "name": "grep", "content": "hit"}),
        _event(kind="turn/start", seq=4),
    ]
    messages = derive_messages(events)
    assert len(messages) == 3
    assert messages[0].role == MessageRole.USER and messages[0].content == "hi"
    assert messages[1].role == MessageRole.ASSISTANT
    assert messages[1].tool_calls[0].name == "grep"
    assert messages[2].role == MessageRole.TOOL_RESULT
    # turn/start 不产出消息
    assert messages[2].content == "hit"


def test_derive_messages_skips_shadowed() -> None:
    events = [
        _event(kind=EVENT_USER_MESSAGE, seq=1, payload={"content": "a"}),
        _event(kind=EVENT_ASSISTANT_MESSAGE, seq=2, payload={"content": "b"}),
        _event(
            kind=EVENT_COMPACTION_SUMMARY,
            seq=3,
            payload={
                "summary": "<state_snapshot>…</state_snapshot>",
                "shadow_start_seq": 1,
                "shadow_end_seq": 2,
                "surface_op": {"op": "replace", "start": 1, "end": 2},
            },
        ),
        _event(kind=EVENT_USER_MESSAGE, seq=4, payload={"content": "c"}),
    ]
    messages = derive_messages(events)
    # seq 1/2 被影子化跳过，只留 seq 4 的用户消息
    assert len(messages) == 1
    assert messages[0].content == "c"


def test_archive_compressed_messages_roundtrip(tmp_path: Path) -> None:
    """压缩归档只写一条 compaction/summary（消息事件已在 persist 边界落盘）。

    真实链路：先经 recorder.sync_history 把消息落进事件流，再压缩归档。
    """
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    messages = [
        Message(role=MessageRole.USER, content="问题"),
        Message(
            role=MessageRole.ASSISTANT,
            content="我先查一下",
            tool_calls=[ToolCall(id="c1", name="grep", arguments={"pattern": "x"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="命中", tool_call_id="c1", name="grep"),
    ]
    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_home=tmp_path,
    )
    recorder.sync_history(messages)  # persist 边界：消息事件落盘

    archive_compressed_messages(
        workspace_dir=tmp_path,
        session_id="s1",
        messages=messages,
        info={
            "original_count": 6,
            "compressed_count": 3,
            "method": "llm",
            "split_point": 3,
        },
        summary_text="<state_snapshot>摘要</state_snapshot>",
        coara_home=tmp_path,
    )

    path = resolve_session_log_path(tmp_path, coara_home=tmp_path)
    assert path.exists(), "归档文件应写入"
    events = read_events(path, session_id="s1")
    # 3 条消息（persist 边界）+ 1 条 compaction/summary（归档不再重写消息）
    assert len(events) == 4
    assert events[-1]["kind"] == EVENT_COMPACTION_SUMMARY
    assert events[-1]["payload"]["summary"] == "<state_snapshot>摘要</state_snapshot>"
    assert events[-1]["payload"]["info"]["split_point"] == 3

    rebuilt = derive_messages(events)
    # 当前视图：未发生前缀分叉（本测试未触发 sync 影子），消息仍在投影中
    assert len(rebuilt) == 3
    assert rebuilt[0].content == "问题"
    assert rebuilt[1].tool_calls[0].name == "grep"
    assert rebuilt[2].content == "命中"


def test_archive_extracts_summary_from_compressed_head_protected(tmp_path: Path, monkeypatch) -> None:
    """头部保护后 compressed[0] 是环境种子：归档摘要须遍历找到 <state_snapshot>。

    压缩结果 = [环境种子, 摘要, ack, ...]——摘要在第 2 条，不能只看首条。
    """
    from types import SimpleNamespace

    from src.session_log.archive import archive_compressed_history_for
    from src.session_log.store import resolve_session_log_path

    monkeypatch.setenv("COARA_HOME", str(tmp_path))
    coara = SimpleNamespace(
        workspace_dir=tmp_path,
        session_id="s1",
        identity=SimpleNamespace(coara_id="cid", name="coara"),
    )
    original = [Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x")]
    compressed = [
        Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x"),
        Message(role=MessageRole.USER, content="<state_snapshot>\n关键摘要\n</state_snapshot>"),
        Message(role=MessageRole.ASSISTANT, content="Got it."),
    ]

    archive_compressed_history_for(
        coara,
        original=original,
        compressed=compressed,
        info={"split_point": 1, "original_count": 1, "compressed_count": 3},
    )

    path = resolve_session_log_path(tmp_path, coara_home=tmp_path)
    events = read_events(path, session_id="s1")
    compaction = [e for e in events if e["kind"] == EVENT_COMPACTION_SUMMARY]
    assert len(compaction) == 1
    assert "关键摘要" in (compaction[0]["payload"].get("summary") or "")


def test_archive_disabled_is_noop(tmp_path: Path) -> None:
    """事件日志已转正（无开关）：archive 恒启用，空消息为 no-op。"""
    from src.session_log.store import resolve_session_log_path

    archive_compressed_messages(
        workspace_dir=tmp_path,
        session_id="s1",
        messages=[],
        info={},
        coara_home=tmp_path,
    )
    assert count_events(resolve_session_log_path(tmp_path, coara_home=tmp_path)) == 0


def test_archive_empty_messages_is_noop(tmp_path: Path) -> None:
    from src.session_log.store import resolve_session_log_path

    archive_compressed_messages(
        workspace_dir=tmp_path,
        session_id="s1",
        messages=[],
        info={},
        coara_home=tmp_path,
    )
    assert count_events(resolve_session_log_path(tmp_path, coara_home=tmp_path)) == 0


def test_archive_all_filtered_no_shadow_inversion(tmp_path: Path) -> None:
    """归档不产生消息事件：影子区间恒为空（None），不出现倒挂。"""
    from src.session_log.store import resolve_session_log_path

    archive_compressed_messages(
        workspace_dir=tmp_path,
        session_id="s1",
        messages=[Message(role=MessageRole.USER, content="x")],
        info={"original_count": 1, "compressed_count": 0, "method": "llm"},
        coara_home=tmp_path,
    )

    path = resolve_session_log_path(tmp_path, coara_home=tmp_path)
    events = read_events(path, session_id="s1")
    assert len(events) == 1
    payload = events[-1]["payload"]
    assert events[-1]["kind"] == EVENT_COMPACTION_SUMMARY
    # 归档只写语义标记：影子区间为空（None），投影端跳过
    assert payload["shadow_start_seq"] is None
    assert payload["shadow_end_seq"] is None
    assert "surface_op" not in payload


def test_append_events_advances_seq_cache(tmp_path: Path) -> None:
    """append_events 写入带 seq 的事件后推进 _seq_cache，后续 append_with_seq 不分配重复 seq。"""
    import src.session_log.store as store_mod
    from src.session_log.store import append_with_seq

    path = tmp_path / "session_events.jsonl"
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "a"})])
    assert store_mod._seq_cache.get(path) == 1
    # 绕过分配点直接写 seq=5（镜像/迁移类路径）
    append_events(path, [_event(kind=EVENT_USER_MESSAGE, seq=5, payload={"content": "b"})])
    assert store_mod._seq_cache.get(path) == 5
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "c"})])
    assert [e["seq"] for e in read_events(path)] == [1, 5, 6]


def test_append_with_seq_rescans_when_file_externally_truncated(tmp_path: Path) -> None:
    """seq 缓存过期防御：文件被外部清空后重新分配从 1 开始，不沿用旧缓存。"""
    import src.session_log.store as store_mod
    from src.session_log.store import append_with_seq, last_seq

    path = tmp_path / "session_events.jsonl"
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "a"})])
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "b"})])
    assert last_seq(path) == 2
    # 缓存已建立（path → 2）。外部截断/删除：
    path.write_text("", encoding="utf-8")
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "c"})])
    events = read_events(path)
    assert [e["seq"] for e in events] == [1]
    assert store_mod._seq_cache.get(path) == 1
    # 外部删除文件同理
    path.unlink()
    append_with_seq(path, lambda seq: [_event(kind=EVENT_USER_MESSAGE, seq=seq, payload={"content": "d"})])
    assert [e["seq"] for e in read_events(path)] == [1]


def test_build_recorder_always_returns() -> None:
    # 事件日志已转正（唯一事实源，无开关）：recorder 恒创建
    recorder = build_recorder(
        workspace_dir=".",
        session_id="s1",
        coara_id="c1",
        coara_name="root",
    )
    assert recorder is not None


def test_recorder_writes_turn_boundary_events(tmp_path: Path) -> None:
    """写点纪律：recorder 只记 turn 边界与 meta；消息事件走 sync_history。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_id="c1",
        coara_name="root",
    )
    recorder.record_turn_start("t1")
    recorder.record_turn_end("t1", reason="completed")

    path = resolve_session_log_path(tmp_path)
    events = read_events(path, session_id="s1")
    kinds = [e["kind"] for e in events]
    assert kinds == ["turn/start", "turn/end"]
    assert events[1]["payload"]["reason"] == "completed"


def test_recorder_writes_segment_open_event(tmp_path: Path) -> None:
    """注入段边界：record_segment_open 落 segment/open 事件，带 seq/source/mid_turn。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorder = SessionLogRecorder(workspace_dir=tmp_path, session_id="s1")
    recorder.record_segment_open(seq=1, source="web", turn_id="t1", mid_turn=False)
    recorder.record_segment_open(seq=2, source="matrix", turn_id="t1", mid_turn=True)

    path = resolve_session_log_path(tmp_path)
    events = read_events(path, session_id="s1")
    assert [e["kind"] for e in events] == ["segment/open", "segment/open"]
    assert events[0]["payload"] == {"seq": 1, "source": "web", "mid_turn": False}
    assert events[1]["payload"]["source"] == "matrix"
    assert events[1]["payload"]["mid_turn"] is True
    assert events[1]["turn_id"] == "t1"


def test_recorder_writes_assistant_diff_event(tmp_path: Path) -> None:
    """record_assistant_diff 落 assistant/diff 事件，带 diff/source。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    diff = {
        "path": "a.py",
        "added": 2,
        "removed": 1,
        "hunks": [[{"kind": "delete", "oldNum": 5, "newNum": 0, "code": "old"}]],
    }
    recorder = SessionLogRecorder(workspace_dir=tmp_path, session_id="s1")
    recorder.record_assistant_diff(diff=diff, turn_id="t1", source="web")

    path = resolve_session_log_path(tmp_path)
    events = read_events(path, session_id="s1")
    assert [e["kind"] for e in events] == ["assistant/diff"]
    assert events[0]["payload"]["diff"] == diff
    assert events[0]["payload"]["source"] == "web"
    assert events[0]["turn_id"] == "t1"


def test_recorder_diff_budget_folds_large_hunk(tmp_path: Path) -> None:
    """单 hunk 超预算时折叠为首尾各 3 行 + 中间省略占位（防撑爆录像带）。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    big_hunk = [{"kind": "context", "oldNum": i, "newNum": i, "code": f"line{i}"} for i in range(500)]
    diff = {"path": "big.py", "added": 0, "removed": 0, "hunks": [big_hunk]}
    recorder = SessionLogRecorder(workspace_dir=tmp_path, session_id="s1")
    recorder.record_assistant_diff(diff=diff, turn_id="t1", source="web")

    path = resolve_session_log_path(tmp_path)
    events = read_events(path, session_id="s1")
    assert len(events) == 1
    out_hunks = events[0]["payload"]["diff"]["hunks"]
    assert len(out_hunks) == 1
    folded = out_hunks[0]
    # 首 3 + 占位 + 尾 3 = 7 行
    assert len(folded) == 7
    assert folded[3]["code"].startswith("… 已省略")
    assert folded[0]["code"] == "line0"
    assert folded[-1]["code"] == "line499"


def test_recorder_diff_empty_hunks_not_recorded(tmp_path: Path) -> None:
    """无 hunks 的 diff 不落带。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorder = SessionLogRecorder(workspace_dir=tmp_path, session_id="s1")
    recorder.record_assistant_diff(
        diff={"path": "a.py", "added": 0, "removed": 0, "hunks": []}, turn_id="t1", source="web"
    )
    path = resolve_session_log_path(tmp_path)
    assert read_events(path, session_id="s1") == []


def test_recorder_seq_increments_across_calls(tmp_path: Path) -> None:
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorder = SessionLogRecorder(workspace_dir=tmp_path, session_id="s1")
    recorder.record_turn_start("t1")
    recorder.record_turn_end("t1", reason="completed")
    recorder2 = SessionLogRecorder(workspace_dir=tmp_path, session_id="s2")
    recorder2.record_turn_start("t2")
    events = read_events(resolve_session_log_path(tmp_path))
    # seq 全局单调：s1 的 turn/end 是 2，s2 的 turn/start 是 3
    assert [e["seq"] for e in events] == [1, 2, 3]


def test_recorder_interleaved_threads_no_duplicate_seq(tmp_path: Path) -> None:
    import threading

    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorders = [SessionLogRecorder(workspace_dir=tmp_path, session_id=f"s{i}") for i in range(4)]
    barrier = threading.Barrier(len(recorders))

    def _worker(idx: int) -> None:
        barrier.wait()  # 尽量让各 recorder 同时开写，暴露 seq 分配竞争
        for n in range(25):
            recorders[idx].record_session_meta({"n": n})

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(len(recorders))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = read_events(resolve_session_log_path(tmp_path))
    seqs = [e["seq"] for e in events]
    assert len(events) == 100
    # 全局分配点保证无重复且连续
    assert seqs == list(range(1, 101))
