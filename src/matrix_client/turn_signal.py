"""Matrix turn lifecycle signal.

The phone typing indicator is driven by ``[COARA_TURN]`` envelope messages on
the room timeline (reliable, ordered), not by m.typing ephemeral:

- 手机发出消息 → 亮起 typing...（三点动画）
- turn 结束信封 ``{"event":"end","background":bool}``：background=false 灭灯；
  true 表示后台级联工作（子智能体/后台任务/工作流）仍在跑，降级为 typing 单点闪烁
- 全部安静信封 ``{"event":"quiet"}``：后台工作也归零，彻底灭灯
"""

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
    """turn 结束信封；background=True 表示主 turn 收尾时仍有后台级联工作在跑。

    ``chunk_losses>0`` 时携带 ``chunk_lost`` 标记：本回合正文 chunk 有发送失败，
    手机端收到后主动 hydrate 补拉，不等丢段。
    """
    payload: dict[str, Any] = {"event": "end", "background": background}
    if chunk_losses > 0:
        payload["chunk_lost"] = chunk_losses
    return MATRIX_TURN_ENVELOPE_PREFIX + json.dumps(payload, separators=(",", ":"))


class TurnSendStats:
    """回合级发送统计：正文 chunk 失败计数（供结束信封携带丢失标记）。

    send_chunk 内部已重试（send_guard），这里统计的是重试用尽仍失败的终态，
    即手机端确定缺失的正文段数。``send`` 属性持有包装后的计数发送器，
    由调用方在回合体内替换 host.send_chunk 使用。
    """

    __slots__ = ("failed_chunks", "send")

    def __init__(self) -> None:
        self.failed_chunks = 0
        self.send: Any | None = None

    def wrap(self, send_chunk: Any) -> Any:
        """包一层统计：控制消息（[COARA_TURN] 信封）不算正文 chunk。

        仅在确知失败（重试用尽返回 False）时向上回传 False 以便计数触发；
        其余（None / 异常）维持原样——异常时无法判定成败，保守不计。
        """

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
    """Wrap a Matrix turn; send the [COARA_TURN] end envelope on exit.

    正常结束、异常、slash 命令提前返回都会送到。信封携带此刻的后台工作状态；
    传入 send_stats 且本回合有正文 chunk 发送失败时带 chunk_lost 标记。
    结束信封是控制消息，发送失败强制重试（指数退避），不再被吞。
    """
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
