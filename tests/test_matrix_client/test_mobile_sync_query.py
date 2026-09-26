"""Silent mobile sync queries ([COARA_MODELS]/[COARA_USAGE]/…) type=query)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.coara.mobile_sync import (
    MODELS_END,
    MODELS_START,
    STATUS_END,
    STATUS_START,
    USAGE_END,
    USAGE_START,
    WORKSPACES_END,
    WORKSPACES_START,
    configure_mobile_sync_sender,
    format_status_payload,
    format_usage_payload,
    try_handle_mobile_sync_query,
)


def _query(start: str, end: str) -> str:
    return f'{start}\n{{"type":"query"}}\n{end}'


@pytest.fixture
def sync_sender():
    sent: list[tuple[str, str]] = []

    async def fake_sender(room_id: str, body: str) -> None:
        sent.append((room_id, body))

    import src.coara.mobile_sync as mobile_sync

    previous = mobile_sync._SYNC_SENDER
    configure_mobile_sync_sender(fake_sender)
    try:
        yield sent
    finally:
        configure_mobile_sync_sender(previous)


@pytest.mark.asyncio
async def test_models_query_replies_with_payload_only(sync_sender) -> None:
    root = SimpleNamespace(foreground_coara=SimpleNamespace(provider_name="kimi", model_name="k3"))
    handled = try_handle_mobile_sync_query(root, "room-1", _query(MODELS_START, MODELS_END))
    assert handled is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(sync_sender) == 1
    room_id, body = sync_sender[0]
    assert room_id == "room-1"
    assert body.startswith(MODELS_START)
    assert '"current": "kimi·k3"' in body


@pytest.mark.asyncio
async def test_usage_and_status_queries(sync_sender, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_usage_payload",
        lambda root: format_usage_payload(summary="输入 1 · 输出 2", input_tokens=1, output_tokens=2),
    )
    monkeypatch.setattr(
        "src.coara.mobile_sync.build_status_payload",
        lambda root: format_status_payload(summary="v8 · 对话 0 条", workspace="v8"),
    )
    root = SimpleNamespace()
    assert try_handle_mobile_sync_query(root, "room-1", _query(USAGE_START, USAGE_END)) is True
    assert try_handle_mobile_sync_query(root, "room-1", _query(STATUS_START, STATUS_END)) is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(sync_sender) == 2
    assert sync_sender[0][1].startswith(USAGE_START)
    assert sync_sender[1][1].startswith(STATUS_START)


@pytest.mark.asyncio
async def test_non_query_is_not_consumed(sync_sender) -> None:
    root = SimpleNamespace()
    payload_like = f'{MODELS_START}\n{{"type":"models","current":"kimi/k3","choices":[]}}\n{MODELS_END}'
    assert try_handle_mobile_sync_query(root, "room-1", payload_like) is False
    assert try_handle_mobile_sync_query(root, "room-1", "/model") is False
    assert sync_sender == []


def test_is_mobile_sync_query_requires_anchored_envelope() -> None:
    """Chat text merely mentioning a tag must not be treated as a sync query."""
    from src.coara.mobile_sync import is_mobile_sync_query

    # Tag mid-text (not at the start) → ordinary message
    assert is_mobile_sync_query(f"请 query 一下 {STATUS_START} 是什么意思 {STATUS_END}") is False
    assert is_mobile_sync_query(f'note: {STATUS_START}\n{{"type":"query"}}\n{STATUS_END}') is False
    # Starts with the tag but the closing tag is missing → not an envelope
    assert is_mobile_sync_query(f'{STATUS_START}\n{{"type":"query"}}') is False
    # Proper envelope (tolerates surrounding whitespace) → sync query
    assert is_mobile_sync_query(f'  \n{STATUS_START}\n{{"type":"query"}}\n{STATUS_END}\n') is True
    assert is_mobile_sync_query(_query(MODELS_START, MODELS_END)) is True


@pytest.mark.asyncio
async def test_query_without_sender_is_consumed_without_llm_leak() -> None:
    """No sender configured: still consume (True) so the envelope never reaches an LLM turn."""
    import src.coara.mobile_sync as mobile_sync

    previous = mobile_sync._SYNC_SENDER
    configure_mobile_sync_sender(None)
    try:
        root = SimpleNamespace()
        handled = try_handle_mobile_sync_query(root, "room-1", _query(WORKSPACES_START, WORKSPACES_END))
        assert handled is True
    finally:
        configure_mobile_sync_sender(previous)
