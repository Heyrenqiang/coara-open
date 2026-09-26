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


def test_contains_secret_extended_patterns() -> None:
    assert contains_secret("github_pat_11AFLO24Q0gQ4XOrSFk7OG_AtPgnagtM0sqaA4Bwsm6yUm1ZiT")
    assert contains_secret("token ghp_abcdefghij1234567890ABCD")
    assert contains_secret("glpat-abcdefghijklmnopqrst")
    assert contains_secret("Gitee 令牌 fedcba0987654321fedcba0987654321 已保存")
    assert contains_secret("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
    assert contains_secret("MINIMAX_API_KEY=abcdef123456")

    assert not contains_secret("记住这个决策：发布前必须跑全量测试")
    assert not contains_secret("2026-09-26 三端分割线机制定稿，详见架构文档")
    assert not contains_secret("commit af8d0564 修复了审批桥")
