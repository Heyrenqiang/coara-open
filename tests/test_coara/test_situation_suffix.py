"""情境后缀注入规则：仅主会话（delegate_depth==0）注入，子智能体不注入。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.types import Message, MessageRole


def _make_coara(*, delegate_depth: int, user_facing: bool | None = None) -> SimpleNamespace:
    if user_facing is None:
        user_facing = delegate_depth == 0
    coara = SimpleNamespace(
        message_history=[Message(role=MessageRole.USER, content="干活")],
        workspace_dir="/tmp/ws",
        session_id="sess-1",
        provider=SimpleNamespace(name="p"),
        model_name="m",
        _active_turn=None,
        _turn_timing=None,
        delegate_depth=delegate_depth,
        inject_environment_seed=True,
        identity=SimpleNamespace(user_facing=user_facing),
    )
    coara._build_system_prompt = lambda: "sys"
    coara._serialize_messages_for_trace = lambda msgs: []
    coara._get_tool_definitions_for_llm = lambda: []
    coara._emit_trace = lambda *args, **kwargs: None
    coara._resolve_context_input_tokens = lambda *args, **kwargs: 1000
    coara._evaluate_context_guard = lambda *args, **kwargs: SimpleNamespace(
        should_warn=False, reason=""
    )
    return coara


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.coara.turn_loop import context_prep as cp

    async def fake_maybe_compress(messages, **kwargs):  # noqa: ANN001
        return messages, {"compressed": False, "status": "NOOP"}

    monkeypatch.setattr(cp.context_window_manager, "maybe_compress_messages", fake_maybe_compress)
    monkeypatch.setattr(
        "src.coara.workspace_switch_history.sanitize_dangling_tool_tail", lambda history: None
    )
    monkeypatch.setattr(
        "src.coara.workspace_switch_history.close_unmatched_tool_calls",
        lambda history, content: (0, 0),
    )
    fake_provider = SimpleNamespace(get_context_window=lambda model: 128_000)
    monkeypatch.setattr(
        "src.llm.service.llm_service.resolve",
        lambda profile: SimpleNamespace(provider=fake_provider, model="fake"),
    )

    async def passthrough_await(operation, signal, **kwargs):  # noqa: ANN001
        return await operation

    monkeypatch.setattr(cp, "_await_interruptible", passthrough_await)


@pytest.mark.asyncio
async def test_situation_suffix_injected_for_root(monkeypatch, tmp_path) -> None:
    """主会话（delegate_depth=0）回合末挂 <情境> 后缀。"""
    from src.coara.turn_loop import context_prep as cp

    _patch_pipeline(monkeypatch)
    (tmp_path / "situation.md").write_text("用户在验收\n", encoding="utf-8")
    coara = _make_coara(delegate_depth=0)

    result = await cp.prepare_messages_for_llm_turn(
        coara, iteration=1, signal=SimpleNamespace(), compact_hook_runner=None
    )
    contents = [str(m.content) for m in result.turn_messages]
    assert any("<情境>" in c and "当前对话来自" in c for c in contents)


@pytest.mark.asyncio
async def test_situation_suffix_skipped_for_subagent(monkeypatch) -> None:
    """子智能体（delegate_depth>=1）不注情境——它不面向任何对话端。"""
    from src.coara.turn_loop import context_prep as cp

    _patch_pipeline(monkeypatch)
    coara = _make_coara(delegate_depth=1)

    result = await cp.prepare_messages_for_llm_turn(
        coara, iteration=1, signal=SimpleNamespace(), compact_hook_runner=None
    )
    contents = [str(m.content) for m in result.turn_messages]
    assert all("<情境>" not in c for c in contents)
    assert all("当前对话来自" not in c for c in contents)


@pytest.mark.asyncio
async def test_situation_suffix_skipped_for_janitor(monkeypatch) -> None:
    """janitor 等维护主体（delegate_depth=0 但 user_facing=False）不注情境。"""
    from src.coara.turn_loop import context_prep as cp

    _patch_pipeline(monkeypatch)
    coara = _make_coara(delegate_depth=0, user_facing=False)

    result = await cp.prepare_messages_for_llm_turn(
        coara, iteration=1, signal=SimpleNamespace(), compact_hook_runner=None
    )
    contents = [str(m.content) for m in result.turn_messages]
    assert all("<情境>" not in c for c in contents)
    assert all("当前对话来自" not in c for c in contents)
