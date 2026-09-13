"""Tests for /report slash command (developer webhook delivery)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.coara.commands import execute_command
from src.coara.commands.report import (
    clear_all_pending_report,
    has_pending_report,
    try_consume_pending_report_async,
)
from src.coara.report_client import ReportEndpoint, ReportSendResult, build_report_payload
from src.core.types import Message, MessageRole, ToolCall


@pytest.fixture(autouse=True)
def _clear_pending() -> None:
    clear_all_pending_report()
    yield
    clear_all_pending_report()


def _make_root(tmp_path: Path, *, messages: list[Message] | None = None) -> SimpleNamespace:
    history = list(messages or [])
    coara = SimpleNamespace(
        session_id="sess-report-1",
        workspace_dir=tmp_path / "ws",
        provider_name="fake",
        model_name="fake-model",
        message_history=history,
        get_status=lambda: {
            "provider": "fake",
            "model": "fake-model",
            "workspace_dir": str(tmp_path / "ws"),
            "message_count": len(history),
            "name": "考拉",
            "status": "idle",
        },
    )
    (tmp_path / "ws").mkdir(exist_ok=True)
    return SimpleNamespace(
        foreground_coara=coara,
        workspace_manager=SimpleNamespace(coara_home=tmp_path / "home"),
        foreground_active_name=lambda: "主空间",
        get_status=coara.get_status,
    )


@pytest.mark.asyncio
async def test_report_alone_enters_pending(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    result = await execute_command(root, "/report")
    assert result is not None
    assert result.data.get("pending") is True
    assert "请描述" in result.output
    assert has_pending_report(root)


@pytest.mark.asyncio
async def test_report_unconfigured_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COARA_REPORT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("COARA_REPORT_WEBHOOK_SECRET", raising=False)
    root = _make_root(tmp_path)
    with patch("src.coara.commands.report.resolve_report_endpoint", return_value=None):
        result = await execute_command(root, "/report 卡住了")
    assert result is not None
    assert result.data.get("error") is True
    assert "尚未就绪" in result.output


@pytest.mark.asyncio
async def test_report_inline_posts_webhook(tmp_path: Path) -> None:
    messages = [
        Message(role=MessageRole.USER, content="你好"),
        Message(
            role=MessageRole.ASSISTANT,
            content="在的",
            tool_calls=[ToolCall(id="t1", name="read", arguments={"path": "/a"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="ok", tool_call_id="t1"),
    ]
    root = _make_root(tmp_path, messages=messages)
    endpoint = ReportEndpoint(url="https://dev.example/webhook/coara-user-report", secret="s")
    send = AsyncMock(return_value=ReportSendResult(ok=True, status_code=200))
    with (
        patch("src.coara.commands.report.resolve_report_endpoint", return_value=endpoint),
        patch("src.coara.commands.report.send_report", send),
    ):
        result = await execute_command(root, "/report 工具调用失败了")
    assert result is not None
    assert result.data.get("error") is not True
    assert "已发送给开发者" in result.output
    send.assert_awaited_once()
    payload = send.await_args.args[1]
    assert payload["event_type"] == "user.report"
    assert payload["content"] == "工具调用失败了"
    assert payload["message_count"] == 3
    assert len(payload["messages"]) == 3


@pytest.mark.asyncio
async def test_pending_then_description_submits(tmp_path: Path) -> None:
    root = _make_root(tmp_path, messages=[Message(role=MessageRole.USER, content="hi")])
    await execute_command(root, "/report")
    endpoint = ReportEndpoint(url="https://dev.example/webhook/coara-user-report", secret="")
    send = AsyncMock(return_value=ReportSendResult(ok=True, status_code=200))
    with (
        patch("src.coara.commands.report.resolve_report_endpoint", return_value=endpoint),
        patch("src.coara.commands.report.send_report", send),
    ):
        result = await try_consume_pending_report_async(root, "手机端切空间后卡住")
    assert result is not None
    assert "已发送给开发者" in result.output
    assert not has_pending_report(root)
    assert send.await_args.args[1]["content"] == "手机端切空间后卡住"


@pytest.mark.asyncio
async def test_other_slash_abandons_pending(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    await execute_command(root, "/report")
    assert has_pending_report(root)
    await execute_command(root, "/help")
    assert not has_pending_report(root)


def test_build_report_payload_truncates_large_history() -> None:
    huge = "x" * 80_000
    messages = [{"role": "user", "content": huge} for _ in range(40)]
    dump = {
        "session_id": "s",
        "workspace": "w",
        "message_count": len(messages),
        "messages": messages,
        "captured_at": "t",
        "provider": "p",
        "model": "m",
        "status": {},
    }
    payload = build_report_payload(description="big", report_id="abc", dump=dump)
    raw_len = len(__import__("json").dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert raw_len <= 1_500_000
    assert payload.get("truncated") is True
    assert len(payload.get("messages") or []) < 40
