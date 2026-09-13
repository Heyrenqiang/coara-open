"""Matrix turn signal: [COARA_TURN] envelope lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from src.matrix_client.turn_signal import (
    MATRIX_TURN_ENVELOPE_PREFIX,
    MATRIX_TURN_QUIET_ENVELOPE,
    matrix_turn_scope,
    turn_end_envelope,
)


def _parse(envelope: str) -> dict[str, Any]:
    import json

    assert envelope.startswith(MATRIX_TURN_ENVELOPE_PREFIX)
    return json.loads(envelope[len(MATRIX_TURN_ENVELOPE_PREFIX) :])


@pytest.mark.asyncio
async def test_turn_end_envelope_sent_on_normal_exit() -> None:
    """turn 正常结束必须发结束信封（手机端灭灯准绳），无后台工作时 background=false"""
    sent: list[tuple[str, str]] = []

    async def _send(room_id: str, text: str) -> None:
        sent.append((room_id, text))

    async with matrix_turn_scope("!r", send_chunk=_send, background_active=lambda: False):
        pass
    assert len(sent) == 1
    payload = _parse(sent[0][1])
    assert payload == {"event": "end", "background": False}


@pytest.mark.asyncio
async def test_turn_end_envelope_sent_on_abnormal_exit() -> None:
    """turn 异常收尾同样要发结束信封，否则手机端 typing 卡到兜底超时"""
    sent: list[tuple[str, str]] = []

    async def _send(room_id: str, text: str) -> None:
        sent.append((room_id, text))

    with pytest.raises(RuntimeError, match="boom"):
        async with matrix_turn_scope("!r", send_chunk=_send):
            raise RuntimeError("boom")
    assert len(sent) == 1
    assert _parse(sent[0][1])["event"] == "end"


@pytest.mark.asyncio
async def test_turn_end_envelope_marks_background_work() -> None:
    """收尾时仍有后台级联工作 → background=true，手机端降级为 typing 单点闪烁"""
    sent: list[tuple[str, str]] = []

    async def _send(room_id: str, text: str) -> None:
        sent.append((room_id, text))

    async with matrix_turn_scope("!r", send_chunk=_send, background_active=lambda: True):
        pass
    assert _parse(sent[0][1]) == {"event": "end", "background": True}


def test_quiet_envelope_format() -> None:
    assert _parse(MATRIX_TURN_QUIET_ENVELOPE) == {"event": "quiet"}


def test_turn_end_envelope_helper() -> None:
    assert _parse(turn_end_envelope(background=True)) == {"event": "end", "background": True}


@pytest.mark.asyncio
async def test_end_envelope_retries_until_success() -> None:
    """结束信封是一等公民控制消息：前两次失败仍退避重试，成功即恢复。"""
    sent: list[str] = []
    attempts = 0

    async def flaky_send(room_id: str, text: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return False
        sent.append(text)
        return True

    async with matrix_turn_scope("!r", send_chunk=flaky_send):
        pass
    assert attempts == 3
    assert _parse(sent[0]) == {"event": "end", "background": False}


@pytest.mark.asyncio
async def test_end_envelope_all_retries_exhausted_still_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试用尽：记录错误但不抛出（不能打断 turn 收尾），手机端靠本地看门狗兜底。"""
    monkeypatch.setattr("src.matrix_client.turn_signal._END_ENVELOPE_BACKOFF_S", (0.01, 0.01, 0.01))
    calls = 0

    async def dead_send(room_id: str, text: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    async with matrix_turn_scope("!r", send_chunk=dead_send):
        pass
    assert calls == 3


@pytest.mark.asyncio
async def test_chunk_losses_flagged_in_end_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """正文 chunk 发送失败计数入回合统计，结束信封携带 chunk_lost 标记。"""
    monkeypatch.setattr("src.matrix_client.turn_signal._END_ENVELOPE_BACKOFF_S", (0.01, 0.01, 0.01))
    from src.matrix_client.turn_signal import turn_send_stats

    envelopes: list[str] = []

    async def send(room_id: str, text: str) -> bool:
        if text.startswith(MATRIX_TURN_ENVELOPE_PREFIX):
            envelopes.append(text)
            return True
        return False  # 正文全部失败

    stats = turn_send_stats(send)
    assert stats is not None
    async with matrix_turn_scope("!r", send_chunk=send, send_stats=stats):
        await stats.send("!r", "正文一")
        await stats.send("!r", "正文二")
    assert stats.failed_chunks == 2
    payload = _parse(envelopes[0])
    assert payload == {"event": "end", "background": False, "chunk_lost": 2}


def test_turn_end_envelope_no_losses_omits_marker() -> None:
    """零丢失不带 chunk_lost 键（旧版手机端解析兼容）。"""
    assert "chunk_lost" not in turn_end_envelope(background=False)
