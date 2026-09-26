"""Tests for Matrix diff bridge payloads."""

from __future__ import annotations

import json

import pytest

from src.coara.diff_render import MAX_SCROLLBACK_DIFF_LINES
from src.coara.tool_output.types import DiffDisplayBlock
from src.matrix_client.diff_bridge import DIFF_END, DIFF_START, build_matrix_diff_message


def test_build_matrix_diff_message_new_file() -> None:
    new_text = "\n".join(f"line {i}" for i in range(1, 6))
    blocks = [
        DiffDisplayBlock(
            path="D:/ws/snake.html",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    message = build_matrix_diff_message(blocks)
    assert message is not None
    assert message.startswith(DIFF_START)
    assert message.endswith(DIFF_END)
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    assert payload["path"] == "D:/ws/snake.html"
    assert payload["added"] == 5
    assert payload["removed"] == 0
    assert payload["remaining"] == 0
    assert len(payload["lines"]) == 5
    assert payload["lines"][0]["k"] == "add"
    assert payload["lines"][0]["t"] == "line 1"


def test_build_matrix_diff_message_truncates_keeps_head_and_tail() -> None:
    new_text = "\n".join(f"line {i}" for i in range(1, 41))
    blocks = [
        DiffDisplayBlock(
            path="game.html",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    message = build_matrix_diff_message(blocks)
    assert message is not None
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    assert payload["added"] == 40
    assert payload["remaining"] > 0
    # 屏幕行预算内保留头部与尾部（尾部常含错误/关键结果）
    assert payload["lines"][0]["t"] == "line 1"
    assert payload["lines"][-1]["t"] == "line 40"
    assert len(payload["lines"]) <= MAX_SCROLLBACK_DIFF_LINES
    assert payload["remaining"] == 40 - len(payload["lines"])


def test_build_matrix_diff_message_clips_oversized_line() -> None:
    long_line = "x" * 500
    new_text = "head\n" + long_line + "\ntail"
    blocks = [
        DiffDisplayBlock(
            path="a.py",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    message = build_matrix_diff_message(blocks)
    assert message is not None
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    clipped = next(line["t"] for line in payload["lines"] if line["t"].startswith("xxx"))
    assert clipped.endswith("…")
    assert len(clipped) < 500


def test_build_matrix_diff_message_skips_blank_lines() -> None:
    """新增内容的空行不显示（diff 只保留有内容的行）。"""
    new_text = "before\n\nafter"
    blocks = [
        DiffDisplayBlock(
            path="a.txt",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    message = build_matrix_diff_message(blocks)
    assert message is not None
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    assert len(payload["lines"]) == 2
    assert payload["lines"][0]["t"] == "before"
    assert payload["lines"][1]["t"] == "after"


def test_build_matrix_diff_message_carries_lineage() -> None:
    """diff 载荷带 tool_call_id / parent_tool_call_id：端上据此挂位与折叠。"""
    blocks = [
        DiffDisplayBlock(path="a.py", old_text="", new_text="x", old_start=1, new_start=1, is_new_file=True)
    ]
    message = build_matrix_diff_message(blocks, tool_call_id="c1", parent_tool_call_id="d1")
    assert message is not None
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    assert payload["tool_call_id"] == "c1"
    assert payload["parent_tool_call_id"] == "d1"


def test_build_matrix_diff_message_omits_absent_lineage() -> None:
    """主会话 diff 无父标识时字段不落盘（老端按缺失容忍）。"""
    blocks = [
        DiffDisplayBlock(path="a.py", old_text="", new_text="x", old_start=1, new_start=1, is_new_file=True)
    ]
    message = build_matrix_diff_message(blocks)
    assert message is not None
    payload = json.loads(message[len(DIFF_START) + 1 : -len(DIFF_END)].strip())
    assert "tool_call_id" not in payload
    assert "parent_tool_call_id" not in payload


@pytest.mark.asyncio
async def test_push_matrix_text_uses_standing_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """收官直推：无 turn 时仍能用 bot 常驻 send_text + 快照房间。"""
    from src.matrix_client import diff_bridge

    sent: list[tuple[str, str]] = []

    async def standing(room: str, body: str) -> None:
        sent.append((room, body))

    monkeypatch.setattr(diff_bridge, "_MATRIX_SEND_TEXT", standing)
    monkeypatch.setattr(diff_bridge, "get_turn_send_text", lambda: None)

    assert await diff_bridge.push_matrix_text(room_id="!snap:local", body="子智能体结果") is True
    assert sent == [("!snap:local", "子智能体结果")]

    # 空房间 / 空正文不发
    assert await diff_bridge.push_matrix_text(room_id="", body="x") is False
    assert await diff_bridge.push_matrix_text(room_id="!r", body="  ") is False
    assert len(sent) == 1


def test_get_matrix_send_text_prefers_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.matrix_client import diff_bridge

    async def standing(room: str, body: str) -> None:
        return None

    async def ctx(room: str, body: str) -> None:
        return None

    monkeypatch.setattr(diff_bridge, "_MATRIX_SEND_TEXT", standing)
    monkeypatch.setattr(diff_bridge, "get_turn_send_text", lambda: ctx)
    assert diff_bridge.get_matrix_send_text() is ctx

    monkeypatch.setattr(diff_bridge, "get_turn_send_text", lambda: None)
    assert diff_bridge.get_matrix_send_text() is standing
