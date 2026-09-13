"""ConversationProjectionCache — 命中/增量/重建 三路径单测。"""

from __future__ import annotations

from pathlib import Path

from src.session_log.conversation_projection import ConversationProjectionCache
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


def _turn(seq: int, turn_id: str, source: str) -> dict:
    return _event("turn/start", seq, payload={"source": source}, turn_id=turn_id)


def test_cache_incremental_append_and_hit(tmp_path: Path) -> None:
    tape = _tape(
        tmp_path,
        [
            _turn(1, "t1", "web"),
            _event("user/message", 2, payload={"content": "问一"}),
            _event("assistant/message", 3, payload={"content": "答一"}),
        ],
    )
    cache = ConversationProjectionCache(tape)
    assert [r["content"] for r in cache.rows()] == ["问一", "答一"]
    assert cache.rows()[0]["source"] == "web"

    # 追加新事件：增量路径 不重建
    append_events(
        tape,
        [
            _turn(4, "t2", "matrix"),
            _event("user/message", 5, payload={"content": "问二"}),
            _event("assistant/message", 6, payload={"content": "答二"}),
        ],
    )
    rows = cache.rows()
    assert [r["content"] for r in rows] == ["问一", "答一", "问二", "答二"]
    assert rows[2]["source"] == "matrix"
    assert rows[3]["source"] == "matrix"  # assistant 继承同 turn
    # 无新事件：命中路径 结果一致
    assert [r["content"] for r in cache.rows()] == [r["content"] for r in rows]


def test_cache_incremental_source_from_previous_batch(tmp_path: Path) -> None:
    """turn/start 在旧批：增量批次里的消息凭累积索引解析 source（风险 1 根治）。"""
    tape = _tape(tmp_path, [_turn(1, "t1", "web")])
    cache = ConversationProjectionCache(tape)
    assert cache.rows() == []
    # 消息在后续批次到达 本批无 turn/start
    append_events(
        tape,
        [
            _event("user/message", 2, payload={"content": "跨批问"}),
            _event("assistant/message", 3, payload={"content": "跨批答"}),
        ],
    )
    rows = cache.rows()
    assert [r["source"] for r in rows] == ["web", "web"]


def test_cache_shadow_triggers_full_rebuild(tmp_path: Path) -> None:
    tape = _tape(
        tmp_path,
        [
            _turn(1, "t1", "web"),
            _event("user/message", 2, payload={"content": "旧一"}),
            _event("assistant/message", 3, payload={"content": "旧二"}),
            _event("user/message", 4, payload={"content": "旧三"}),
        ],
    )
    cache = ConversationProjectionCache(tape)
    assert len(cache.rows()) == 3
    # 影子截断 keep_until_seq=2：seq>2 的已投影行全部移除
    append_events(
        tape,
        [
            _event("history/shadow", 5, payload={"keep_until_seq": 2}),
            _event("assistant/message", 6, payload={"content": "新一"}),
        ],
    )
    rows = cache.rows()
    assert [r["content"] for r in rows] == ["旧一", "新一"]


def test_cache_max_rows_ring(tmp_path: Path) -> None:
    events = [_event("user/message", i, payload={"content": f"m{i}"}) for i in range(1, 8)]
    tape = _tape(tmp_path, events)
    cache = ConversationProjectionCache(tape, max_rows=100)
    # max_rows 下限 100 防过紧 这里只验证不膨胀
    assert len(cache.rows()) == 7
    more = [_event("user/message", 100 + i, payload={"content": f"x{i}"}) for i in range(150)]
    append_events(tape, more)
    assert len(cache.rows()) == 100
    assert cache.rows()[-1]["content"] == "x149"
