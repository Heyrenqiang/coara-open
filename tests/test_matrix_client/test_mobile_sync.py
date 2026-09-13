"""Mobile sync payloads ([COARA_MODELS] / [COARA_WORKSPACES] / usage / status) and command output hard breaks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.coara.commands.types import CommandResult
from src.coara.mobile_sync import (
    MODELS_END,
    MODELS_START,
    STATUS_END,
    STATUS_START,
    USAGE_END,
    USAGE_START,
    WORKSPACES_END,
    WORKSPACES_START,
    format_models_payload,
    format_status_payload,
    format_usage_payload,
    format_workspaces_payload,
)
from src.matrix_client.chat_commands import try_handle_matrix_chat_command


def _extract_block(text: str, start: str, end: str) -> dict:
    i = text.index(start) + len(start)
    j = text.index(end, i)
    return json.loads(text[i:j].strip())


def test_format_thinking_payload_round_trip() -> None:
    from src.coara.mobile_sync import THINKING_END, THINKING_START, format_thinking_payload

    payload = format_thinking_payload(
        enabled=True,
        source="默认开启",
        note="MiniMax M3：思考已开（自适应）",
        model="minimax/MiniMax-M3",
    )
    data = _extract_block(payload, THINKING_START, THINKING_END)
    assert data["type"] == "thinking"
    assert data["enabled"] is True
    assert data["model"] == "minimax/MiniMax-M3"
    # 档位字段默认缺省（旧模型不支持档位）
    assert data["level"] == ""
    assert data["supports_levels"] is False


def test_format_thinking_payload_with_levels() -> None:
    from src.coara.mobile_sync import THINKING_END, THINKING_START, format_thinking_payload

    payload = format_thinking_payload(
        enabled=True,
        source="本会话",
        note="Kimi K3：思考已开（档位：中）",
        model="kimi/k3",
        level="medium",
        supports_levels=True,
    )
    data = _extract_block(payload, THINKING_START, THINKING_END)
    assert data["level"] == "medium"
    assert data["supports_levels"] is True


def test_format_usage_and_status_payload_round_trip() -> None:
    usage = _extract_block(
        format_usage_payload(summary="输入 12 · 输出 3", input_tokens=12, output_tokens=3, llm_turns=1),
        USAGE_START,
        USAGE_END,
    )
    assert usage["type"] == "usage"
    assert usage["input_tokens"] == 12
    assert usage["summary"] == "输入 12 · 输出 3"

    status = _extract_block(
        format_status_payload(
            summary="v8 · 对话 4 条",
            workspace="v8",
            session_id="sid-1",
            last_user_activity_at=1_700_000_000_000,
            idle_timeout_seconds=7200,
        ),
        STATUS_START,
        STATUS_END,
    )
    assert status["type"] == "status"
    assert status["workspace"] == "v8"
    assert status["session_id"] == "sid-1"
    assert status["last_user_activity_at"] == 1_700_000_000_000
    assert status["idle_timeout_seconds"] == 7200


def test_build_usage_payload_empty_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from pathlib import Path

    from src.coara.mobile_sync import build_usage_payload
    from src.runtime.usage_query import SessionUsageSummary

    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "src.runtime.usage_query.resolve_usage_path_for_root",
        lambda _root: events,
    )
    monkeypatch.setattr(
        "src.runtime.usage_query.summarize_session_stats",
        lambda _path, _sid: SessionUsageSummary(session_id="s1", events_path=Path(_path)),
    )
    root = SimpleNamespace(session_id="s1", _usage_store=None)
    data = _extract_block(build_usage_payload(root), USAGE_START, USAGE_END)
    assert data["summary"] == "本会话暂无用量"
    assert data["llm_turns"] == 0


def test_build_status_payload_compact() -> None:
    from src.coara.mobile_sync import build_status_payload

    fg = SimpleNamespace(session_id="sess-abc")
    root = SimpleNamespace(
        get_status=lambda: {"status": "idle", "message_count": 4, "workspace_dir": ""},
        workspace_manager=None,
        foreground_coara=fg,
        _last_user_activity_at=1_700_000_000.5,
    )
    data = _extract_block(build_status_payload(root), STATUS_START, STATUS_END)
    assert data["summary"] == "对话 4 条"
    assert data["workspace"] == ""
    assert data["session_id"] == "sess-abc"
    assert data["last_user_activity_at"] == 1_700_000_000_500
    assert data["idle_timeout_seconds"] == 7200
    assert "status" not in data
    # session_event is opt-in: absent unless a real new-session push asks for it
    assert "session_event" not in data


def test_status_payload_session_event_only_when_requested() -> None:
    from src.coara.mobile_sync import build_status_payload

    root = SimpleNamespace(
        get_status=lambda: {"message_count": 0, "workspace_dir": ""},
        workspace_manager=None,
        foreground_coara=SimpleNamespace(session_id="s1"),
        _last_user_activity_at=0.0,
    )
    plain = _extract_block(build_status_payload(root), STATUS_START, STATUS_END)
    assert "session_event" not in plain

    marked = _extract_block(build_status_payload(root, session_event="new_session"), STATUS_START, STATUS_END)
    assert marked["session_event"] == "new_session"


def test_build_models_payload_carries_view_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """models 回推必须带视图空间名：手机端据此结算切空间画线，防旧视图回推错配。"""
    from src.coara.mobile_sync import build_models_payload

    monkeypatch.setattr("src.llm.model_catalog.list_model_choices", lambda _cfg: [])

    view = SimpleNamespace(
        provider_name="kimi",
        model_name="k3",
        workspace_dir=r"D:\code_ws\gora",
    )
    manager = SimpleNamespace(
        match_path_to_workspace_id=lambda _path: "wid-nx",
        registry=SimpleNamespace(get_by_id=lambda _wid: SimpleNamespace(name="nx")),
        active_name="nx",
        active_path=r"D:\code_ws\gora",
    )
    root = SimpleNamespace(foreground_coara=view, workspace_manager=manager)
    data = _extract_block(build_models_payload(root), MODELS_START, MODELS_END)
    assert data["current"] == "kimi·k3"
    assert data["workspace"] == "nx"


def test_build_models_payload_workspace_empty_without_manager() -> None:
    """无 workspace_manager 时归属字段为空串（旧端兼容：不参与归属判定）。"""
    from src.coara.mobile_sync import build_models_payload

    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(provider_name="p", model_name="m"),
        workspace_manager=None,
    )
    data = _extract_block(build_models_payload(root), MODELS_START, MODELS_END)
    assert data["workspace"] == ""


def test_build_status_payload_workspace_name_override() -> None:
    """Switch pushes pass the TraceEvent target so the divider label cannot drift."""
    from src.coara.mobile_sync import build_status_payload

    root = SimpleNamespace(
        get_status=lambda: {"message_count": 1, "workspace_dir": r"D:\code_ws\gora"},
        workspace_manager=None,
        foreground_coara=SimpleNamespace(session_id="sid-nx"),
        _last_user_activity_at=0.0,
    )
    data = _extract_block(
        build_status_payload(root, session_event="workspace_switch", workspace_name="nx"),
        STATUS_START,
        STATUS_END,
    )
    assert data["workspace"] == "nx"
    assert data["session_event"] == "workspace_switch"
    assert data["summary"].startswith("nx ·")


def test_push_status_payload_forwards_session_event() -> None:
    import asyncio

    import src.coara.mobile_sync as mobile_sync

    mobile_sync.reset_status_push_throttle_for_tests()
    sent: list[tuple[str, str]] = []

    async def _fake_sender(room_id: str, text: str):
        sent.append((room_id, text))

    previous = mobile_sync._SYNC_SENDER
    mobile_sync.configure_mobile_sync_sender(_fake_sender)
    try:
        root = SimpleNamespace(
            get_status=lambda: {"message_count": 0, "workspace_dir": ""},
            workspace_manager=None,
            foreground_coara=SimpleNamespace(session_id="s1"),
            _last_user_activity_at=1_700_000_000.0,
            matrix_notify=SimpleNamespace(resolve_room_id=lambda: "room-1"),
        )

        async def _run():
            mobile_sync.push_status_payload(root, force=True, session_event="new_session")
            mobile_sync.push_status_payload(root, force=True)  # e.g. workspace switch path
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert len(sent) == 2
        first = _extract_block(sent[0][1], STATUS_START, STATUS_END)
        second = _extract_block(sent[1][1], STATUS_START, STATUS_END)
        assert first["session_event"] == "new_session"
        assert "session_event" not in second
    finally:
        mobile_sync.configure_mobile_sync_sender(previous)
        mobile_sync.reset_status_push_throttle_for_tests()


@pytest.mark.asyncio
async def test_root_start_new_session_delegates_without_push() -> None:
    """start_new_session 只代理到前台；手机推送由 session_started 事件订阅层负责。"""
    from unittest.mock import AsyncMock

    from src.coara.root import RootCoara

    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(start_new_session=AsyncMock(return_value="sid-new")),
    )
    session_id = await RootCoara.start_new_session(root, interrupt_source="idle_timeout")

    assert session_id == "sid-new"
    root.foreground_coara.start_new_session.assert_awaited_once_with(interrupt_source="idle_timeout")


def test_session_started_subscriber_pushes_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """push_status_for_session_started：同空间新会话推手机画分隔线，matrix 自发 /new 跳过。"""
    import src.coara.mobile_sync as mobile_sync
    from src.core.events import TraceEvent

    calls: list[dict] = []

    def _fake_push(root, *, force=False, fallback_room_id="", session_event=None, **_kw):
        calls.append({"force": force, "session_event": session_event})

    monkeypatch.setattr(mobile_sync, "push_status_payload", _fake_push)

    def _event(**payload) -> TraceEvent:
        return TraceEvent(coara_id="c", coara_name="n", event_type="session_started", message="m", payload=payload)

    # 手机 pin 在 ws-1
    root = SimpleNamespace(pinned_view_id=lambda end: "ws-1")

    push = mobile_sync.push_status_for_session_started
    # 同空间（ws-1）web 端 /new → 推
    push(root, _event(interrupt_source="web_new_command", workspace_id="ws-1"))
    # 其它空间（ws-2）/new → 不推
    push(root, _event(interrupt_source="web_new_command", workspace_id="ws-2"))
    # 手机自己发的 /new → 跳过（避免重复画线）
    push(root, _event(interrupt_source="matrix_new_command", workspace_id="ws-1"))
    # 无空间定位（兼容旧事件）→ 推
    push(root, _event(interrupt_source="new_command"))

    assert calls == [
        {"force": True, "session_event": "new_session"},
        {"force": True, "session_event": "new_session"},
    ]


@pytest.mark.asyncio
async def test_handle_new_passes_origin_into_interrupt_source() -> None:
    """handle_new：origin_source（web/matrix）拼进 interrupt_source，供各端区分自己/他端。"""
    from unittest.mock import AsyncMock

    from src.coara.commands.registry import CommandArgs
    from src.coara.commands.session import handle_new

    for origin, expected in (("web", "web_new_command"), ("matrix", "matrix_new_command"), ("", "new_command")):
        target = SimpleNamespace(
            start_new_session=AsyncMock(return_value="sid"),
            flow_coordinator=SimpleNamespace(reset=AsyncMock()),
        )
        root = SimpleNamespace(
            foreground_coara=target,
            get_status=lambda: {"errors_log_path": ""},
            workspace_manager=None,
        )
        args = CommandArgs(name="new", parts=["new"], raw="/new", origin_source=origin, target_coara=target)
        result = await handle_new(root, args)
        target.start_new_session.assert_awaited_once_with(interrupt_source=expected)
        assert result.action == "new_session"


def test_push_status_payload_throttles_then_force() -> None:
    import asyncio

    import src.coara.mobile_sync as mobile_sync

    mobile_sync.reset_status_push_throttle_for_tests()
    sent: list[tuple[str, str]] = []

    async def _fake_sender(room_id: str, text: str):
        sent.append((room_id, text))

    previous = mobile_sync._SYNC_SENDER
    mobile_sync.configure_mobile_sync_sender(_fake_sender)
    try:
        fg = SimpleNamespace(session_id="s1")
        root = SimpleNamespace(
            get_status=lambda: {"message_count": 0, "workspace_dir": ""},
            workspace_manager=None,
            foreground_coara=fg,
            _last_user_activity_at=1_700_000_000.0,
            matrix_notify=SimpleNamespace(resolve_room_id=lambda: "room-1"),
        )

        async def _run():
            mobile_sync.push_status_payload(root, force=False)
            mobile_sync.push_status_payload(root, force=False)  # throttled
            mobile_sync.push_status_payload(root, force=True)
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert len(sent) == 2
        assert all(r == "room-1" for r, _ in sent)
        assert STATUS_START in sent[0][1]
    finally:
        mobile_sync.configure_mobile_sync_sender(previous)
        mobile_sync.reset_status_push_throttle_for_tests()


def test_run_thinking_toggle() -> None:
    from src.llm.thinking_mode import reset_for_tests, run_thinking_command, set_cli_thinking_enabled

    reset_for_tests()
    set_cli_thinking_enabled(True)
    off = run_thinking_command("/thinking toggle", model="MiniMax-M3", driver="anthropic", base_url="")
    assert "关" in off.lines[0]
    on = run_thinking_command("/thinking toggle", model="MiniMax-M3", driver="anthropic", base_url="")
    assert "开" in on.lines[0]
    reset_for_tests()


def test_build_models_payload_labels_are_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Android kotlinx.serialization expects label:String; a tuple becomes a JSON array and breaks parse."""
    from src.coara.mobile_sync import build_models_payload
    from src.llm.model_catalog import ModelChoice

    monkeypatch.setattr(
        "src.llm.model_catalog.list_model_choices",
        lambda _cm: [
            ModelChoice(provider="agnes", model_id="agnes-pro", label="agnes/agnes-pro"),
            ModelChoice(provider="kimi", model_id="k3", label="kimi/k3"),
        ],
    )
    root = SimpleNamespace(foreground_coara=SimpleNamespace(provider_name="kimi", model_name="k3"))
    data = _extract_block(build_models_payload(root), MODELS_START, MODELS_END)
    assert all(isinstance(c["label"], str) for c in data["choices"])
    assert data["choices"][0]["label"] == "agnes/agnes-pro"


def test_format_workspaces_payload_round_trip() -> None:
    payload = format_workspaces_payload(
        active_id="ws-1",
        workspaces=[{"name": "主空间", "id": "ws-1", "summary": "", "active": True}],
    )
    data = _extract_block(payload, WORKSPACES_START, WORKSPACES_END)
    assert data["type"] == "workspaces"
    assert data["active_id"] == "ws-1"
    assert data["workspaces"][0]["active"] is True


def test_build_workspaces_payload_marks_matrix_view_not_global_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手机独立视图：active_id/行内 active 都必须是 matrix 视图，不能残留全局前台标记。"""
    from src.coara.commands.types import CommandResult
    from src.coara.mobile_sync import build_workspaces_payload

    list_result = CommandResult(
        output="工作空间",
        data={
            "active_id": "id-v8",
            "active_name": "v8",
            "workspaces": [
                {"idx": 1, "name": "v8", "id": "id-v8", "summary": "", "path": "/v8", "mode": "local", "active": True},
                {"idx": 2, "name": "nx", "id": "id-nx", "summary": "", "path": "/nx", "mode": "local", "active": False},
            ],
        },
    )
    monkeypatch.setattr(
        "src.coara.commands.workspace._build_workspace_list_result",
        lambda root: list_result,
    )
    root = SimpleNamespace(_matrix_view_workspace_id="id-nx")
    data = _extract_block(build_workspaces_payload(root), WORKSPACES_START, WORKSPACES_END)
    assert data["active_id"] == "id-nx"
    by_name = {w["name"]: w["active"] for w in data["workspaces"]}
    assert by_name == {"v8": False, "nx": True}


@pytest.mark.asyncio
async def test_model_command_sends_hard_break_output_and_models_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "可用模型（当前 kimi/k3）：\n 1. kimi/k3\n用法: /model <序号>"
    result = CommandResult(output=output, data={"current": "kimi/k3", "choices": []})
    monkeypatch.setattr(
        "src.matrix_client.chat_commands.execute_command",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_models_payload",
        lambda root: format_models_payload(current="kimi/k3", choices=[]),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_thinking_payload",
        lambda root: "[COARA_THINKING]\n{}\n[/COARA_THINKING]",
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_status_payload",
        lambda root: format_status_payload(summary="v8 · 对话 4 条"),
    )
    root = SimpleNamespace(foreground_coara=SimpleNamespace(provider_name="kimi", model_name="k3"))
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    handled = await try_handle_matrix_chat_command(root, "/model", send_text=send_text)

    assert handled is True
    # 多行输出不再包 ``` 代码块，改用 Markdown 硬换行保留行结构
    assert sent[0] == output.replace("\n", "  \n")
    assert "```" not in sent[0]
    assert sent[1].startswith(MODELS_START)


@pytest.mark.asyncio
async def test_usage_command_sends_usage_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CommandResult(output="用量摘要…", data={})
    monkeypatch.setattr(
        "src.matrix_client.chat_commands.execute_command",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_usage_payload",
        lambda root: format_usage_payload(summary="输入 1 · 输出 2", input_tokens=1, output_tokens=2),
    )
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    assert await try_handle_matrix_chat_command(SimpleNamespace(), "/usage", send_text=send_text) is True
    assert sent[0] == "用量摘要…"
    assert sent[1].startswith(USAGE_START)


@pytest.mark.asyncio
async def test_ws_command_sends_workspaces_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CommandResult(output="Name ID Path\nActive: 主空间", data={"workspaces": [], "active_id": "ws-1"})
    monkeypatch.setattr(
        "src.matrix_client.chat_commands.execute_command",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_workspaces_payload",
        lambda root: format_workspaces_payload(active_id="ws-1", workspaces=[]),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_status_payload",
        lambda root: format_status_payload(summary="v8 · 对话 4 条"),
    )
    root = SimpleNamespace()
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    handled = await try_handle_matrix_chat_command(root, "/ws", send_text=send_text)

    assert handled is True
    assert sent[0] == "Name ID Path  \nActive: 主空间"
    assert "```" not in sent[0]
    assert sent[1].startswith(WORKSPACES_START)


@pytest.mark.asyncio
async def test_single_line_output_not_fenced(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CommandResult(output="已切换 → kimi/k3", data={})
    monkeypatch.setattr(
        "src.matrix_client.chat_commands.execute_command",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_models_payload",
        lambda root: format_models_payload(current="kimi/k3", choices=[]),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_thinking_payload",
        lambda root: "[COARA_THINKING]\n{}\n[/COARA_THINKING]",
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_status_payload",
        lambda root: format_status_payload(summary="v8 · 对话 4 条"),
    )
    root = SimpleNamespace(foreground_coara=SimpleNamespace(provider_name="kimi", model_name="k3"))
    sent: list[str] = []

    async def send_text(body: str) -> None:
        sent.append(body)

    handled = await try_handle_matrix_chat_command(root, "/model 2", send_text=send_text)

    assert handled is True
    assert sent[0] == "已切换 → kimi/k3"
    assert sent[1].startswith(MODELS_START)
