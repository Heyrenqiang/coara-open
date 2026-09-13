"""Conversation projection — L1 事件带 → UI 对话行 投影器单测。"""

from __future__ import annotations

from pathlib import Path

from src.session_log.conversation_projection import (
    contains_shadow_events,
    filter_rows_for_frontend,
    iter_conversation_rows,
    project_events,
)
from src.session_log.store import append_events
from src.session_log.types import build_event


def _event(
    kind: str,
    seq: int,
    *,
    session_id: str = "s1",
    payload: dict | None = None,
    turn_id: str = "",
    agent_kind: str = "main",
    ts: float = 1_700_000_000.0,
) -> dict:
    return build_event(
        kind=kind,
        seq=seq,
        session_id=session_id,
        payload=payload or {},
        turn_id=turn_id,
        agent_kind=agent_kind,
        ts=ts,
    )


def _tape(tmp_path: Path, events: list[dict]) -> Path:
    path = tmp_path / "session_events.jsonl"
    append_events(path, events)
    return path


def test_role_mapping_and_non_message_kinds_skipped() -> None:
    rows = project_events(
        [
            _event("user/message", 1, payload={"content": "问"}),
            _event("assistant/message", 2, payload={"content": "答"}),
            _event("tool/result", 3, payload={"content": "tool out"}),
            _event("system/note", 4, payload={"content": "note"}),
            _event("tool/exec", 5, payload={"tool_name": "shell"}),
        ]
    )
    assert [(r["role"], r["content"]) for r in rows] == [("user", "问"), ("assistant", "答")]
    assert all(r["seq"] in (1, 2) for r in rows)


def test_content_list_blocks_joined() -> None:
    rows = project_events(
        [
            _event(
                "user/message",
                1,
                payload={
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image", "url": "x"},
                        {"type": "text", "text": "第二段"},
                    ]
                },
            )
        ]
    )
    assert rows[0]["content"] == "看图\n第二段"


def test_source_from_turn_start_index_and_positional_attribution() -> None:
    """turn/start 建 turn_id→source 索引；不带 turn_id 的消息事件归属会话最近 turn。"""
    rows = project_events(
        [
            _event("turn/start", 1, payload={"source": "web"}, turn_id="t-web"),
            _event("user/message", 2, payload={"content": "from web"}),
            _event("assistant/message", 3, payload={"content": "web reply"}),
            _event("turn/start", 4, payload={"source": "matrix"}, turn_id="t-mx"),
            _event("user/message", 5, payload={"content": "from phone"}),
            _event("assistant/message", 6, payload={"content": "phone reply"}),
        ]
    )
    assert [r["source"] for r in rows] == ["web", "web", "matrix", "matrix"]
    assert [r["turn_id"] for r in rows] == ["t-web", "t-web", "t-mx", "t-mx"]

    web_rows = filter_rows_for_frontend(rows, "web")
    assert [r["content"] for r in web_rows] == ["from web", "web reply"]
    mx_rows = filter_rows_for_frontend(rows, "matrix")
    assert [r["content"] for r in mx_rows] == ["from phone", "phone reply"]


def test_unknown_source_rows_kept_by_frontend_filter() -> None:
    """source 解析不到的老行（turn/start 无 payload.source）在任何前端都保留。"""
    rows = project_events(
        [
            _event("turn/start", 1, turn_id="t1"),  # legacy：payload 无 source
            _event("user/message", 2, payload={"content": "legacy"}),
            _event("assistant/message", 3, payload={"content": "legacy reply"}),
        ]
    )
    assert [r["source"] for r in rows] == ["", ""]
    assert [r["content"] for r in filter_rows_for_frontend(rows, "web")] == ["legacy", "legacy reply"]
    assert len(filter_rows_for_frontend(rows, "matrix")) == 2


def test_assistant_diff_projects_as_assistant_row_with_diff() -> None:
    """assistant/diff 事件投影为 role=assistant、带 diff 字段的 UI 行（content 空）。"""
    diff = {
        "path": "a.py",
        "added": 2,
        "removed": 1,
        "hunks": [[{"kind": "delete", "oldNum": 5, "newNum": 0, "code": "old"}]],
    }
    rows = project_events(
        [
            _event("turn/start", 1, payload={"source": "web"}, turn_id="t1"),
            _event("user/message", 2, payload={"content": "改下文件"}),
            _event("assistant/diff", 3, payload={"diff": diff, "source": "web"}, turn_id="t1"),
        ]
    )
    assert len(rows) == 2
    diff_row = rows[1]
    assert diff_row["role"] == "assistant"
    assert diff_row["content"] == ""
    assert diff_row["diff"] == diff
    assert diff_row["source"] == "web"
    assert diff_row["turn_id"] == "t1"
    # diff 行经前端过滤保留（source=web）
    assert any("diff" in r for r in filter_rows_for_frontend(rows, "web"))


def test_assistant_diff_missing_payload_skipped() -> None:
    """payload 无合法 diff 时不产出行（防脏数据）。"""
    rows = project_events([_event("assistant/diff", 1, payload={"source": "web"})])
    assert rows == []


def test_assistant_inherits_previous_user_source() -> None:
    """assistant 行自己解析不到 source 时继承同会话前一 user 行的 source。"""
    rows = project_events(
        [
            _event("turn/start", 1, payload={"source": "web"}, turn_id="t1"),
            _event("user/message", 2, payload={"content": "web 问"}),
            _event("turn/start", 3, payload={"source": ""}, turn_id="t2"),
            _event("assistant/message", 4, payload={"content": "答"}),
        ]
    )
    assert rows[-1]["source"] == "web"
    assert [r["content"] for r in filter_rows_for_frontend(rows, "web")] == ["web 问", "答"]


def test_system_injection_rows_skipped_and_wrappers_stripped() -> None:
    rows = project_events(
        [
            _event("user/message", 1, payload={"content": "<系统消息>\n重启清算\n</系统消息>"}),
            _event("user/message", 2, payload={"content": "<系统提醒>\n必须遵守\n</系统提醒>"}),
            _event("user/message", 3, payload={"content": "<子智能体消息>\n[t1]\n报告\n</子智能体消息>"}),
            _event("user/message", 4, payload={"content": "<途中消息>\n追指令\n</途中消息>"}),
            _event("user/message", 5, payload={"content": "<任务指令>\n子代理任务\n</任务指令>"}),
            _event("user/message", 6, payload={"content": "手机来的"}),
            _event("user/message", 7, payload={"content": "<接续输入>\n排队补充\n</接续输入>"}),
            _event("assistant/message", 9, payload={"content": "<系统消息>\n不是注入也要 Skip\n</系统消息>"}),
        ]
    )
    # 系统/注入类整段跳过；<接续输入> 剥外层包裹留正文；裸文本原样。
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "手机来的"),
        ("user", "排队补充"),
    ]


def test_files_event_projects_as_assistant_row() -> None:
    files = [{"file_id": "b" * 32, "filename": "x.png", "mime": "image/png", "is_image": True}]
    rows = project_events(
        [
            _event("assistant/files", 1, payload={"files": files, "source": "web"}, turn_id="t1"),
        ]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "assistant"
    assert row["content"] == ""
    assert row["source"] == "web"
    assert row["turn_id"] == "t1"
    assert row["files"][0]["filename"] == "x.png"


def test_history_shadow_truncates_only_own_session() -> None:
    rows = project_events(
        [
            _event("turn/start", 1, payload={"source": "cli"}, turn_id="t1"),
            _event("user/message", 2, payload={"content": "keep me"}),
            _event("user/message", 3, payload={"content": "切换到B"}),
            _event("assistant/message", 4, session_id="s2", payload={"content": "别空间行"}),
            _event("history/shadow", 5, payload={"keep_until_seq": 2, "replaced_count": 1}),
        ]
    )
    assert [(r["session_id"], r["content"]) for r in rows] == [("s1", "keep me"), ("s2", "别空间行")]


def test_compaction_summary_interval_skipped() -> None:
    rows = project_events(
        [
            _event("user/message", 1, payload={"content": "old"}),
            _event("user/message", 2, payload={"content": "old2"}),
            _event("compaction/summary", 3, payload={"shadow_start_seq": 1, "shadow_end_seq": 2}),
            _event("user/message", 4, payload={"content": "压缩后"}),
        ]
    )
    assert [r["content"] for r in rows] == ["压缩后"]


def test_agent_kind_filtering() -> None:
    events = [
        _event("user/message", 1, payload={"content": "main 行"}, agent_kind="main"),
        _event("user/message", 2, payload={"content": "sub 行"}, agent_kind="subagent"),
        _event("user/message", 3, payload={"content": "flow 行"}, agent_kind="flow"),
        _event("user/message", 4, payload={"content": "module 行"}, agent_kind="config"),
    ]
    default_rows = project_events(events)
    assert [r["content"] for r in default_rows] == ["main 行", "flow 行", "module 行"]
    assert [r["content"] for r in project_events(events, include_agent_kinds={"", "main"})] == ["main 行"]
    assert [r["content"] for r in project_events(events, include_agent_kinds={"flow"})] == ["flow 行"]
    assert [r["content"] for r in project_events(events, exclude_agent_kinds=None)] == [
        "main 行",
        "sub 行",
        "flow 行",
        "module 行",
    ]


def test_after_seq_incremental_cursor(tmp_path: Path) -> None:
    tape = _tape(
        tmp_path,
        [
            _event("user/message", 1, payload={"content": "m1"}),
            _event("assistant/message", 2, payload={"content": "m2"}),
            _event("user/message", 3, payload={"content": "m3"}),
        ],
    )
    all_rows = list(iter_conversation_rows(tape))
    assert [r["content"] for r in all_rows] == ["m1", "m2", "m3"]
    assert [r["content"] for r in iter_conversation_rows(tape, after_seq=2)] == ["m3"]
    assert list(iter_conversation_rows(tape, after_seq=3)) == []


def test_contains_shadow_events() -> None:
    assert contains_shadow_events([_event("history/shadow", 1, payload={"keep_until_seq": 0})])
    assert contains_shadow_events([_event("compaction/summary", 1, payload={})])
    assert not contains_shadow_events([_event("user/message", 1, payload={"content": "x"})])


def test_recorder_writes_turn_source_and_files(tmp_path: Path) -> None:
    """写侧：record_turn_start 带 source；record_files 落 assistant/files 事件。"""
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import read_events, resolve_session_log_path

    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_id="c1",
        coara_name="root",
        agent_kind="main",
    )
    recorder.record_turn_start("t1", source="web-flow")
    recorder.record_files(files=[{"file_id": "a" * 32, "filename": "x.png"}], turn_id="t1", source="web")

    path = resolve_session_log_path(tmp_path)
    events = read_events(path, session_id="s1")
    assert [e["kind"] for e in events] == ["turn/start", "assistant/files"]
    assert events[0]["payload"]["source"] == "web-flow"
    assert events[1]["payload"]["source"] == "web"
    assert events[1]["payload"]["files"][0]["filename"] == "x.png"
    assert events[1]["turn_id"] == "t1"

    # project_session（会话恢复）不消费 assistant/files；对话投影产出文件行
    rows = list(iter_conversation_rows(path, include_agent_kinds={"main"}))
    assert [(r["role"], r.get("files")) for r in rows] == [("assistant", [{"file_id": "a" * 32, "filename": "x.png"}])]
    assert rows[0]["source"] == "web"
