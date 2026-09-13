"""TraceStore 事件去重 key 的 content hash 化（REMAINING_ISSUES #154）。"""

from __future__ import annotations

from pathlib import Path

from src.core.events import TraceEvent
from src.ui.trace_store import TraceStore


def _event(content: str, *, event_type: str = "conversation_message", role: str = "assistant") -> TraceEvent:
    return TraceEvent(
        coara_id="c1",
        coara_name="考拉",
        event_type=event_type,
        message=content[:80],
        payload={"session_id": "s1", "role": role, "content": content},
    )


def test_dedupe_semantics_preserved_with_hashed_content(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    big = "长正文" * 10000

    assert store.append_event(_event(big)) is True
    # 同内容同维度 → 去重拒绝
    assert store.append_event(_event(big)) is False
    # 内容不同 → 放行
    assert store.append_event(_event(big + "x")) is True
    # 维度不同（role 变）→ 放行
    assert store.append_event(_event(big, role="user")) is True


def test_written_event_keys_do_not_retain_full_content(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    big = "敏感正文" * 10000
    store.append_event(_event(big))

    keys = store._written_event_keys
    assert len(keys) == 1
    key = keys.popitem()[0]
    # key 中不再出现完整正文，只剩定长 hash（64 字符 hex）
    assert all(len(str(part)) <= 64 for part in key)
    assert not any("敏感正文" in str(part) for part in key)


def test_written_event_keys_bounded(tmp_path: Path) -> None:
    """FIFO 上限封顶（REMAINING_ISSUES：审计 P2 _written_event_keys 无限增长）。"""
    store = TraceStore(tmp_path)
    store._MAX_WRITTEN_EVENT_KEYS = 3
    for i in range(5):
        assert store.append_event(_event(f"内容{i}")) is True
    assert len(store._written_event_keys) == 3
    # 被逐出的旧 key 再次出现 → 按新事件放行（内存封顶优先于全历史去重）
    assert store.append_event(_event("内容0")) is True
    assert len(store._written_event_keys) == 3
