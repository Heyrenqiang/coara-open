"""Matrix turn lifecycle signal"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

MATRIX_TURN_ENVELOPE_PREFIX = "[COARA_TURN]"
MATRIX_TURN_QUIET_ENVELOPE = '[COARA_TURN]{"event":"quiet"}'

# 结束信封是一等公民控制消息：丢了手机端挂灯到本地兜底（30 分钟），必须强制重试。
_END_ENVELOPE_MAX_ATTEMPTS = 3
_END_ENVELOPE_BACKOFF_S = (0.5, 1.0, 2.0)


def turn_end_envelope(*, background: bool, chunk_losses: int = 0) -> str:
    """turn 结束信封；background=True 表示主 turn 收尾时仍有后台级联工作在跑"""
    payload: dict[str, Any] = {"event": "end", "background": background}
    if chunk_losses > 0:
        payload["chunk_lost"] = chunk_losses
    return MATRIX_TURN_ENVELOPE_PREFIX + json.dumps(payload, separators=(",", ":"))


class TurnSendStats:
    """回合级发送统计：正文 chunk 失败计数（供结束信封携带丢失标记）"""

    __slots__ = ("failed_chunks", "send")

    def __init__(self) -> None:
        self.failed_chunks = 0
        self.send: Any | None = None

    def wrap(self, send_chunk: Any) -> Any:
        """包一层统计：控制消息（[COARA_TURN] 信封）不算正文 chunk"""

        async def counted(room_id: str, body: str) -> Any:
            try:
                result = await send_chunk(room_id, body)
            except Exception:
                # 发送抛异常：正文确定未送达，计入丢失；异常原样上抛不打断既有语义
                if not body.startswith(MATRIX_TURN_ENVELOPE_PREFIX):
                    self.failed_chunks += 1
                raise
            if result is False and not body.startswith(MATRIX_TURN_ENVELOPE_PREFIX):
                self.failed_chunks += 1
                return False
            return None

        return counted


async def _send_end_envelope_with_retry(send_chunk: Any, room_id: str, envelope: str) -> bool:
    from src.core.logger import logger

    for attempt in range(_END_ENVELOPE_MAX_ATTEMPTS):
        try:
            if await send_chunk(room_id, envelope) is not False:
                if attempt:
                    logger.info("matrix turn end envelope recovered on attempt %d (room_id=%s)", attempt + 1, room_id)
                return True
        except Exception as exc:
            logger.warning(
                "matrix turn end envelope send raised (attempt %d/%d, room_id=%s): %s",
                attempt + 1,
                _END_ENVELOPE_MAX_ATTEMPTS,
                room_id,
                exc,
            )
        if attempt < _END_ENVELOPE_MAX_ATTEMPTS - 1:
            await asyncio.sleep(_END_ENVELOPE_BACKOFF_S[attempt])
    logger.error(
        "matrix turn end envelope LOST after %d attempts (room_id=%s): 手机端靠本地看门狗兜底灭灯",
        _END_ENVELOPE_MAX_ATTEMPTS,
        room_id,
    )
    return False


def turn_send_stats(send_chunk: Any | None) -> TurnSendStats | None:
    """为回合创建正文发送统计并包一层计数；回合体内用 ``stats.send`` 替换原通道。

    结束信封发送走原 send_chunk（不经包装），避免自统计自报告。
    """
    if send_chunk is None:
        return None
    stats = TurnSendStats()
    stats.send = stats.wrap(send_chunk)
    return stats


@asynccontextmanager
async def matrix_turn_scope(
    room_id: str,
    *,
    send_chunk: Any | None = None,
    background_active: Callable[[], bool] | None = None,
    send_stats: TurnSendStats | None = None,
) -> AsyncIterator[None]:
    """Wrap a Matrix turn; send the [COARA_TURN] end envelope on exit. 正常结束、异常、slash 命令提前返回都会送到"""
    from src.core.logger import logger

    logger.info("matrix turn start (room_id={})", room_id)
    try:
        yield
    except BaseException as exc:
        logger.info(
            "matrix turn end: abnormal ({}: {}) (room_id={})",
            type(exc).__name__,
            exc,
            room_id,
        )
        raise
    else:
        logger.info("matrix turn end: completed (room_id={})", room_id)
    finally:
        if send_chunk is not None:
            background = False
            if background_active is not None:
                with suppress(Exception):
                    background = bool(background_active())
            chunk_losses = send_stats.failed_chunks if send_stats is not None else 0
            envelope = turn_end_envelope(background=background, chunk_losses=chunk_losses)
            await _send_end_envelope_with_retry(send_chunk, room_id, envelope)
