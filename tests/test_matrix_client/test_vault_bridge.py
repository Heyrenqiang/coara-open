"""Tests for Matrix vault side-channel bridge."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.matrix_client.vault_bridge import (
    build_vault_reply_payload,
    is_vault_ack_message,
    is_vault_reply_message,
    parse_vault_reply,
    try_resolve_vault_reply,
)
from src.vault import VaultService


def _prepared_service(tmp_path) -> VaultService:
    service = VaultService(tmp_path / "home")

    async def prep() -> None:
        await service.initialize()
        service.setup("test-password-123")

    asyncio.run(prep())
    return service


def test_parse_vault_reply_unlock() -> None:
    body = build_vault_reply_payload(action="unlock", password="secret-123")
    parsed = parse_vault_reply(body)
    assert parsed is not None
    assert parsed["action"] == "unlock"
    assert parsed["password"] == "secret-123"


def test_try_resolve_vault_reply_unlocks_service(tmp_path) -> None:
    root = MagicMock()
    service = _prepared_service(tmp_path)
    root.vault_service = service

    body = build_vault_reply_payload(action="unlock", password="test-password-123")
    assert try_resolve_vault_reply(root, "!room:local", body) is True
    assert service.is_unlocked()


def test_try_resolve_vault_reply_creates_vault_on_first_unlock(tmp_path) -> None:
    root = MagicMock()

    async def prep() -> VaultService:
        service = VaultService(tmp_path / "home")
        await service.initialize()
        return service

    service = asyncio.run(prep())
    root.vault_service = service
    assert not service.is_initialized()

    body = build_vault_reply_payload(action="unlock", password="test-password-123")
    assert try_resolve_vault_reply(root, "!room:local", body) is True
    assert service.is_initialized()
    assert service.is_unlocked()


def test_try_resolve_vault_reply_wrong_password(tmp_path) -> None:
    root = MagicMock()
    service = _prepared_service(tmp_path)
    root.vault_service = service

    body = build_vault_reply_payload(action="unlock", password="wrong")
    assert try_resolve_vault_reply(root, "!room:local", body) is True
    assert not service.is_unlocked()


@pytest.mark.asyncio
async def test_maybe_prompt_cancel_returns_cancelled(tmp_path, monkeypatch) -> None:
    import src.matrix_client.vault_bridge as vb

    root = MagicMock()
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    root.vault_service = service

    async def fake_send(_room, _msg):
        return None

    monkeypatch.setattr(vb, "get_turn_send_text", lambda: fake_send)
    monkeypatch.setattr("src.coara.turn_context.get_turn_channel_id", lambda: "!room:local")

    async def cancel_soon():
        await asyncio.sleep(0.05)
        vb.try_resolve_vault_reply(
            root,
            "!room:local",
            vb.build_vault_reply_payload(action="cancel"),
        )

    task = asyncio.create_task(cancel_soon())
    status = await vb.maybe_prompt_vault_unlock(root)
    await task
    assert status == "cancelled"
    await service.shutdown()


def test_vault_reply_detection_helpers() -> None:
    body = build_vault_reply_payload(action="unlock", password="x")
    assert is_vault_reply_message(body)
    assert parse_vault_reply(build_vault_reply_payload(action="lock")) is None
    assert is_vault_ack_message("[vault] 宝箱已解锁")
    assert not is_vault_ack_message("hello")


@pytest.mark.asyncio
async def test_setup_prompt_mentions_letter_digit_policy(tmp_path, monkeypatch) -> None:
    """#346 创建宝箱的设密码提示对齐密码策略的字母数字混合要求"""
    import src.matrix_client.vault_bridge as vb

    root = MagicMock()
    service = VaultService(tmp_path / "home")
    await service.initialize()
    root.vault_service = service

    sent: list[str] = []

    async def fake_send(_room, msg):
        sent.append(msg)

    monkeypatch.setattr(vb, "get_turn_send_text", lambda: fake_send)
    monkeypatch.setattr("src.coara.turn_context.get_turn_channel_id", lambda: "!room:local")

    async def cancel_soon():
        await asyncio.sleep(0.05)
        vb.try_resolve_vault_reply(
            root,
            "!room:local",
            vb.build_vault_reply_payload(action="cancel"),
        )

    task = asyncio.create_task(cancel_soon())
    status = await vb.maybe_prompt_vault_unlock(root)
    await task
    assert status == "cancelled"
    assert sent and "≥8 位，字母数字混合" in sent[0]
    await service.shutdown()


@pytest.mark.asyncio
async def test_cross_frontend_unlock_wakes_waiting_future(tmp_path, monkeypatch) -> None:
    """Bug 1: a Matrix side-channel unlock should resolve a future registered
    by the Web bridge (or vice versa) — both share prompt_registry now."""
    import src.vault.prompt_registry as registry

    root = MagicMock()
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    root.vault_service = service

    # Simulate Web bridge registering a pending future
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    registry.set_pending(future)
    assert registry.has_pending()

    # Matrix side-channel arrives with the correct password
    body = build_vault_reply_payload(action="unlock", password="test-password-123")
    assert try_resolve_vault_reply(root, "!room:local", body) is True

    # 事件循环内解锁走异步任务（#338） 等其收官
    for _ in range(200):
        if future.done():
            break
        await asyncio.sleep(0.01)

    # The Web-registered future should have been resolved
    assert future.done()
    result = future.result()
    assert result["ok"] is True
    assert service.is_unlocked()
    registry.clear()
    await service.shutdown()


@pytest.mark.asyncio
async def test_interrupt_resolves_pending_vault_future(tmp_path) -> None:
    """Bug 2: interrupt_current_turn resolves the pending vault future via
    prompt_registry.resolve, so the blocked tool call returns immediately
    instead of waiting 60s. We test the registry path directly (interrupt
    calls the same registry.resolve)."""
    import src.vault.prompt_registry as registry

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    registry.set_pending(future)
    assert registry.has_pending()

    # Simulate what interrupt_current_turn does: resolve with cancelled
    resolved = registry.resolve({"ok": False, "cancelled": True, "message": "turn interrupted"})
    assert resolved is True
    assert future.done()
    result = future.result()
    assert result["cancelled"] is True
    registry.clear()
