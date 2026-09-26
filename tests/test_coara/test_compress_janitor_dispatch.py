"""压缩成功后派 janitor（压缩前完整历史）；失败/取消不派；合并不连触发。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.context.window import ContextWindowManager
from src.core.types import Message, MessageRole


@pytest.mark.asyncio
async def test_compress_with_llm_dispatches_janitor_after_success(monkeypatch) -> None:
    """压缩成功后才派 janitor：快照仍是压缩前原文，不在 LLM await 之前派发。"""
    order: list[str] = []
    original = [
        Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x"),
        *[
            Message(
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=f"history-{idx}-" + "y" * 200,
            )
            for idx in range(12)
        ],
    ]
    coara = SimpleNamespace(_root_ref=SimpleNamespace())

    def fake_schedule(c, *, original_history):  # noqa: ANN001
        order.append("janitor")
        assert original_history == original

    async def fake_complete(request, **kwargs):  # noqa: ANN001
        order.append("compress_llm")
        from src.llm.provider import LLMResponse

        return LLMResponse(content="<state_snapshot>\n摘要\n</state_snapshot>")

    monkeypatch.setattr(
        "src.coara.workspace_protocol.schedule_janitor_parallel_with_compress",
        fake_schedule,
    )
    monkeypatch.setattr("src.llm.service.llm_service.complete", fake_complete)

    manager = ContextWindowManager(model_context_window=128_000, compression_preserve_last_n=2)
    compressed, info = await manager.compress_with_llm(original, janitor_coara=coara)

    assert info.get("compressed") is True
    assert order == ["compress_llm", "janitor"]
    assert "<state_snapshot" in str(compressed[1].content)


@pytest.mark.asyncio
async def test_compress_cancel_does_not_dispatch_janitor(monkeypatch) -> None:
    """压缩被取消时不派 janitor（避免上下文未缩短却白跑管家）。"""
    scheduled = {"n": 0}
    original = [
        Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x"),
        *[
            Message(
                role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
                content=f"history-{idx}-" + "y" * 200,
            )
            for idx in range(12)
        ],
    ]

    def fake_schedule(c, *, original_history):  # noqa: ANN001
        scheduled["n"] += 1

    async def fake_complete(request, **kwargs):  # noqa: ANN001
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "src.coara.workspace_protocol.schedule_janitor_parallel_with_compress",
        fake_schedule,
    )
    monkeypatch.setattr("src.llm.service.llm_service.complete", fake_complete)

    manager = ContextWindowManager(model_context_window=128_000, compression_preserve_last_n=2)
    with pytest.raises(asyncio.CancelledError):
        await manager.compress_with_llm(original, janitor_coara=SimpleNamespace(_root_ref=object()))
    assert scheduled["n"] == 0


@pytest.mark.asyncio
async def test_auto_compress_passes_janitor_coara(monkeypatch) -> None:
    """prepare_messages_for_llm_turn 把 coara 传给 maybe_compress，由压缩层在成功后派 janitor。"""
    from src.coara.turn_loop import context_prep as cp

    captured: dict[str, object] = {}
    original = [
        Message(role=MessageRole.USER, content="完整对话历史"),
        Message(role=MessageRole.ASSISTANT, content="reply"),
    ]

    async def fake_maybe_compress(messages, *, janitor_coara=None, **kwargs):  # noqa: ANN001
        captured["janitor_coara"] = janitor_coara
        captured["messages"] = list(messages)
        return messages, {"compressed": False, "status": "NOOP"}

    coara = SimpleNamespace(
        message_history=list(original),
        workspace_dir="/tmp/ws",
        session_id="sess-1",
        provider=SimpleNamespace(name="p"),
        model_name="m",
        _active_turn=None,
        _turn_timing=None,
    )
    coara._build_system_prompt = lambda: "sys"
    coara._serialize_messages_for_trace = lambda msgs: []
    coara._get_tool_definitions_for_llm = lambda: []
    coara._emit_trace = lambda *args, **kwargs: None
    coara._resolve_context_input_tokens = lambda *args, **kwargs: 1000
    coara._evaluate_context_guard = lambda *args, **kwargs: SimpleNamespace(should_warn=False, reason="")

    monkeypatch.setattr(cp.context_window_manager, "maybe_compress_messages", fake_maybe_compress)
    monkeypatch.setattr(
        "src.coara.workspace_switch_history.sanitize_dangling_tool_tail",
        lambda history: None,
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

    await cp.prepare_messages_for_llm_turn(
        coara,
        iteration=1,
        signal=SimpleNamespace(),
        compact_hook_runner=None,
    )

    assert captured["janitor_coara"] is coara


@pytest.mark.asyncio
async def test_compress_janitor_coalesce_keeps_latest_snapshot(monkeypatch, tmp_path) -> None:
    """连续压缩：janitor 在飞/冷却合并时保留最新压缩前完整历史，不连开多轮。"""
    from src.coara import workspace_protocol as wp
    from src.core.coara_home import workspace_id_for

    wp.reset_janitor_flights_for_tests()
    monkeypatch.setattr("src.coara.workspace_protocol._janitor_min_interval", lambda: 0.0)
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    snapshots: list[list[Message]] = []
    builds = 0
    blocker = asyncio.Event()

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        nonlocal builds
        builds += 1
        if history_snapshot is not None:
            snapshots.append(list(history_snapshot))

        async def _run() -> None:
            await blocker.wait()

        return asyncio.create_task(_run())

    from tests.test_coara.test_workspace_protocol import _root_for_maintenance

    root = _root_for_maintenance(_fake_run)
    snap_a = [Message(role=MessageRole.USER, content="history-A")]
    snap_b = [Message(role=MessageRole.USER, content="history-B")]

    tid1 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
        history_snapshot=snap_a,
    )
    assert tid1 is not None and builds == 1
    assert snapshots[-1][0].content == "history-A"

    tid2 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
        history_snapshot=snap_b,
    )
    assert tid2 == tid1 and builds == 1
    wid = workspace_id_for(str(ws_dir))
    assert wp._janitor_flights[wid]["history_snapshot"][0].content == "history-B"

    blocker.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if builds >= 2:
            break
    assert builds == 2
    assert snapshots[-1][0].content == "history-B"
    wp.reset_janitor_flights_for_tests()
