"""Tests for memory write gate."""

from __future__ import annotations

from src.records.agent_gate import contains_secret, memory_gate
from src.records.agent_types import MemorySource


def test_memory_gate_behavior() -> None:
    assert contains_secret("api_key: sk-abcdefghijklmnopqrstuvwxyz123456")
    assert contains_secret("password = hunter2")
    assert not contains_secret("选择 asyncio 作为并发模型")

    source = MemorySource(session_id="s1")
    bad = memory_gate("token: Bearer abcdefghijklmnop", source=source)
    assert bad.allowed is False and "敏感" in bad.reason
    assert memory_gate("   ", source=source).allowed is False

    missing = memory_gate("记住这个决策", source=None)
    assert missing.allowed is False and "来源" in missing.reason

    ok = memory_gate(
        "项目 v8 选择 asyncio",
        source=MemorySource(session_id="sess", turn_index=3, actor="user"),
    )
    assert ok.allowed is True
    assert ok.sensitivity in ("internal", "personal", "public")
