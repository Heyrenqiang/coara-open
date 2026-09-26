"""回归：compact 后的推导 context 不得把压缩模板开销一起减掉。

``original_tokens`` 是压缩调用整段 input 的 API 实报（被压区 + 压缩模板 +
固定指令），直接拿它当被压区规模会把模板开销也减掉 → 新上下文系统性偏低。
改用 ``region_chars / call_chars`` 占比把实报分摊回被压区（仍是实报的派生
物，展示标 ~）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.turn_orchestrator import _inject_compact_estimate
from src.context.window import LlmUsageSnapshot
from tests.helpers import make_test_coara


def _coara_with_snapshot(tmp_path: Path) -> tuple[object, LlmUsageSnapshot]:
    coara = make_test_coara(tmp_path)
    snap = LlmUsageSnapshot()
    snap.usage = {"input_tokens": 20_000}
    coara._llm_usage_snapshot = snap  # type: ignore[attr-defined]
    return coara, snap


def test_estimate_scales_out_compression_template_overhead(tmp_path: Path) -> None:
    coara, snap = _coara_with_snapshot(tmp_path)

    _inject_compact_estimate(
        coara,
        {
            "compressed": True,
            "method": "llm",
            "original_tokens": 12_000,
            "new_tokens": 800,
            "region_chars": 9_900,
            "call_chars": 10_000,
        },
    )

    # 旧口径 p_last - original + new = 8800（偏低 80）；折出模板开销后 8920
    assert snap.estimated is True
    assert snap.usage == {"input_tokens": 8_920, "prompt_tokens": 8_920, "output_tokens": 0}


def test_estimate_falls_back_to_raw_report_without_char_weights(tmp_path: Path) -> None:
    """无 region/call 字符量（旧 info / 截断兜底）时保持原公式，不炸。"""
    coara, snap = _coara_with_snapshot(tmp_path)

    _inject_compact_estimate(
        coara,
        {"compressed": True, "method": "llm", "original_tokens": 12_000, "new_tokens": 800},
    )

    assert snap.usage["input_tokens"] == 8_800


@pytest.mark.asyncio
async def test_auto_compress_lands_estimate_not_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自动压缩落地后与 /compact 同口径注入推导值——不得让行内上下文归 0。"""
    from types import SimpleNamespace

    from src.coara.turn_loop import context_prep as cp
    from src.core.types import Message, MessageRole

    seen: list[dict] = []

    def _fake_inject(coara, info):  # noqa: ANN001
        seen.append(dict(info))

    monkeypatch.setattr("src.coara.turn_orchestrator._inject_compact_estimate", _fake_inject)

    async def fake_maybe_compress(messages, **kwargs):  # noqa: ANN001
        return list(messages), {
            "compressed": True,
            "method": "llm",
            "status": "COMPRESSED",
            "original_count": 10,
            "compressed_count": 3,
            "original_tokens": 12_000,
            "new_tokens": 800,
        }

    monkeypatch.setattr(cp.context_window_manager, "maybe_compress_messages", fake_maybe_compress)
    monkeypatch.setattr("src.coara.workspace_switch_history.sanitize_dangling_tool_tail", lambda history: None)
    monkeypatch.setattr(
        "src.coara.workspace_switch_history.close_unmatched_tool_calls", lambda history, content: (0, 0)
    )
    monkeypatch.setattr(
        "src.llm.service.llm_service.resolve",
        lambda profile: SimpleNamespace(
            provider=SimpleNamespace(get_context_window=lambda model: 128_000), model="fake"
        ),
    )

    async def passthrough(operation, signal, **kwargs):  # noqa: ANN001
        return await operation

    monkeypatch.setattr(cp, "_await_interruptible", passthrough)

    coara = SimpleNamespace(
        message_history=[Message(role=MessageRole.USER, content="干活")],
        workspace_dir=str(tmp_path),
        session_id="sess-1",
        provider=SimpleNamespace(name="p"),
        model_name="m",
        _active_turn=None,
        _turn_timing=None,
        delegate_depth=0,
        inject_environment_seed=False,
        identity=SimpleNamespace(user_facing=True),
    )
    coara._build_system_prompt = lambda: "sys"
    coara._serialize_messages_for_trace = lambda msgs: []
    coara._get_tool_definitions_for_llm = lambda: []
    coara._emit_trace = lambda *args, **kwargs: None
    coara._resolve_context_input_tokens = lambda *args, **kwargs: 1000
    coara._evaluate_context_guard = lambda *args, **kwargs: SimpleNamespace(should_warn=False, reason="")

    await cp.prepare_messages_for_llm_turn(coara, iteration=1, signal=SimpleNamespace(), compact_hook_runner=None)

    assert seen, "自动压缩落地后必须注入推导估算（否则状态栏上下文归 0）"
