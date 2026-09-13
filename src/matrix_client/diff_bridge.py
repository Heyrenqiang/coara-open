"""Matrix diff 消息构建与常驻推送（渲染逻辑已收拢进端通道 sender）。

diff 帧统一路由后（EndRegistry.route → matrix sender），手机端 diff 由
``response_stream`` / 跟话通道的 sender 直接构建 [COARA_DIFF] 发房间；
本模块保留纯函数：build_matrix_diff_message（序列化）与 push_matrix_text
（子智能体最终结果等独立文本推送）。
"""

from __future__ import annotations

import json
from typing import Any

from src.coara.diff_render import (
    MAX_SCROLLBACK_DIFF_LINES,
    DiffLine,
    _resymmetrize_visible_diff,
    _truncate_diff_lines_screen,
    collect_diff_hunks,
    diff_line_to_matrix_payload,
    flatten_diff_lines,
)
from src.coara.tool_output.types import DiffDisplayBlock
from src.coara.turn_context import get_turn_send_text
from src.core.logger import logger

DIFF_START = "[COARA_DIFF]"
DIFF_END = "[/COARA_DIFF]"

# Bot 启动时 wire 进来的常驻发送回调：EventBus / 跨回合 drain 时 ContextVar
# 往往为空，子智能体 diff/收官直推靠它（ContextVar 拿不到时回落到 bot 常驻发送）。
_MATRIX_SEND_TEXT: Any | None = None
# 常驻 root：兜底 room_id（remote context 全失效时读 matrix_notify 最近房间）。
_MATRIX_ROOT: Any | None = None

# 手机端固定内容列宽：Android 无终端宽度可查，按竖屏典型宽度保守取值
# （超长行按此估算占屏行数，参与头尾保留的行预算）
_MATRIX_DIFF_CONTENT_COLS = 60
# 单行最大字符数：超长行在 Matrix 侧截断，防 Android 渲染撑爆消息/换行失控
_MATRIX_DIFF_MAX_LINE_CHARS = 200


def get_matrix_send_text() -> Any | None:
    """Prefer in-turn ``turn`` send_text; else the bot-wired standing callback."""
    return get_turn_send_text() or _MATRIX_SEND_TEXT


async def push_matrix_text(*, room_id: str | None, body: str) -> bool:
    """Push plain text to a Matrix room without relying on active remote_turn.

    Returns True if a send was attempted successfully. Used for subagent final
    results that must land in the dispatch room even when drained mid another turn.
    """
    room = str(room_id or "").strip()
    # remote context 全失效时兜底最近房间（matrix_notify 持久化，跨重启存活）
    if not room and _MATRIX_ROOT is not None:
        try:
            room = str(getattr(getattr(_MATRIX_ROOT, "matrix_notify", None), "last_remote_room_id", "") or "").strip()
        except Exception:  # noqa: BLE001
            room = ""
    text = str(body or "").strip()
    if not room or not text:
        return False
    send_text = get_matrix_send_text()
    if send_text is None:
        return False
    try:
        await send_text(room, text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Matrix text push failed: %s", exc)
        return False


def _clip_diff_line(dl: DiffLine, max_chars: int) -> DiffLine:
    """单行字符上限截断（保留行号/符号语义，尾部加省略号）。"""
    if len(dl.code) <= max_chars:
        return dl
    return DiffLine(dl.kind, dl.old_num, dl.new_num, dl.code[: max_chars - 1] + "…")


def build_matrix_diff_message(
    blocks: list[DiffDisplayBlock],
    *,
    tool_call_id: str = "",
    parent_tool_call_id: str = "",
) -> str | None:
    """Serialize diff blocks into a Matrix body the Android app can parse.

    ``tool_call_id`` / ``parent_tool_call_id`` 随载荷下发（有则带）：前者让端上把
    diff 精确挂到对应工具行之后，后者标识这是子智能体的改动——端上折进发起它的
    delegate 行，不铺成主列表的独立卡片。
    """
    if not blocks:
        return None
    path = blocks[0].path
    hunks, added, removed = collect_diff_hunks(blocks)
    if not hunks:
        return None

    flat, _ = flatten_diff_lines(hunks, max_lines=None)
    max_ln = max(
        (max(dl.old_num, dl.new_num) for dl in flat if dl.code or dl.old_num or dl.new_num),
        default=0,
    )
    num_width = max(len(str(max_ln)), 2)
    # 屏幕行感知截断：保留头部与尾部（尾部常含错误/关键结果），
    # 省略计数按逻辑行；超长行按手机宽度参与行预算
    kept, remaining = _truncate_diff_lines_screen(
        flat,
        num_width,
        MAX_SCROLLBACK_DIFF_LINES,
        content_cols=_MATRIX_DIFF_CONTENT_COLS,
    )
    if remaining > 0:
        kept = _resymmetrize_visible_diff(kept)
    line_payload = [diff_line_to_matrix_payload(_clip_diff_line(dl, _MATRIX_DIFF_MAX_LINE_CHARS)) for dl in kept]

    payload = {
        "path": path,
        "added": added,
        "removed": removed,
        "remaining": remaining,
        "lines": line_payload,
    }
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    if parent_tool_call_id:
        payload["parent_tool_call_id"] = parent_tool_call_id
    return f"{DIFF_START}\n{json.dumps(payload, ensure_ascii=False)}\n{DIFF_END}"


def wire_matrix_diff_send_text(send_text: Any | None, root: Any | None = None) -> None:
    """登记常驻发送回调与 root（push_matrix_text / 兜底房间解析用）。"""
    global _MATRIX_SEND_TEXT, _MATRIX_ROOT
    if send_text is not None:
        _MATRIX_SEND_TEXT = send_text
    if root is not None:
        _MATRIX_ROOT = root
