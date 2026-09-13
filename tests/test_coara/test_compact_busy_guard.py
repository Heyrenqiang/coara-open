"""/compact 忙闲保护（P0）：活跃回合中执行 /compact 必须拒绝，不得替换进行中历史。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.coara.commands.compact import handle_compact
from src.coara.commands.registry import CommandArgs
from src.core.types import Message, MessageRole


def _busy_coara():
    """有历史且正处于活跃回合的会话桩（绕过前端 busy 检查后到达的真实形状）。"""
    coara = SimpleNamespace(
        message_history=[Message(role=MessageRole.USER, content="hi")],
        has_active_turn=lambda: True,
        _process_lock=asyncio.Lock(),
    )
    return coara


@pytest.mark.asyncio
async def test_compact_rejected_while_turn_active() -> None:
    coara = _busy_coara()
    root = SimpleNamespace(foreground_coara=coara)
    args = CommandArgs(name="compact", parts=["compact"], raw="/compact")

    result = await handle_compact(root, args)

    assert result.data.get("compressed") is False
    assert "正在进行" in result.output
    # 历史未被触碰
    assert len(coara.message_history) == 1


@pytest.mark.asyncio
async def test_compact_reports_inflight_instead_of_too_short() -> None:
    """压缩在飞时返回「正在压缩中」，不得误报「历史太短」（误导用户连点）。"""
    coara = SimpleNamespace(
        message_history=[Message(role=MessageRole.USER, content="hi")],
        has_active_turn=lambda: False,
        _process_lock=asyncio.Lock(),
        _compress_inflight=True,
    )
    root = SimpleNamespace(foreground_coara=coara)
    args = CommandArgs(name="compact", parts=["compact"], raw="/compact")

    result = await handle_compact(root, args)

    assert result.data.get("compressed") is False
    assert "正在压缩" in result.output
    assert "历史太短" not in result.output


@pytest.mark.asyncio
async def test_force_compress_passes_janitor_coara_for_parallel_dispatch(monkeypatch) -> None:
    """/compact 强制压缩须把 coara 传入压缩层，由 compress_with_llm 与 LLM 同时派 janitor。"""
    from src.coara import turn_orchestrator as to

    original = [
        Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x"),
        Message(role=MessageRole.USER, content="完整对话历史"),
    ]
    coara = SimpleNamespace(
        message_history=list(original),
        workspace_dir="/tmp/ws",
        session_id="sess-1",
        _compress_inflight=False,
        _root_ref=None,
        compact_hook_runner=None,
        _llm_usage_snapshot=SimpleNamespace(clear=lambda: None),
    )
    captured: dict[str, object] = {}

    async def fake_maybe_compress(messages, *, janitor_coara=None, **kwargs):  # noqa: ANN001
        captured["janitor_coara"] = janitor_coara
        captured["messages"] = list(messages)
        return messages, {
            "compressed": True,
            "original_count": len(messages),
            "compressed_count": len(messages),
        }

    monkeypatch.setattr(to.context_window_manager, "maybe_compress_messages", fake_maybe_compress)
    monkeypatch.setattr("src.session_log.archive.archive_compressed_history_for", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.coara.injections.snapshot_injector.SnapshotInjector.build_snapshots",
        lambda *a, **k: [],
    )
    fake_provider = SimpleNamespace(get_context_window=lambda model: 128_000)
    monkeypatch.setattr(
        "src.llm.service.llm_service.resolve",
        lambda profile: SimpleNamespace(provider=fake_provider, model="fake"),
    )
    monkeypatch.setattr(to.context_window_manager, "_coerce_message", lambda message: message)

    info = await to.force_compress_history(coara)

    assert info is not None and info["compressed"] is True
    assert captured["janitor_coara"] is coara
    assert captured["messages"] == original
