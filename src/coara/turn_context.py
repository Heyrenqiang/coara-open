"""Per-turn end-channel context (transport-agnostic).

Each end's adapter wraps its turn in ``turn(source, ...)`` to declare which end
the turn belongs to. Coara core checks ``get_turn_channel()`` / ``has_turn_channel()``
and the turn ``source`` label — not any specific transport protocol. Three ends
are peers; ``source`` distinguishes them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from src.coara.end_channel import EndChannel

if TYPE_CHECKING:
    from src.coara.remote_channel import RemoteInteractionChannel

_channel_id: ContextVar[str | None] = ContextVar("turn_channel_id", default=None)
_send_text: ContextVar[Any | None] = ContextVar("turn_send_text", default=None)
_interaction_channel: ContextVar[RemoteInteractionChannel | None] = ContextVar(
    "turn_interaction_channel",
    default=None,
)
_end_channel: ContextVar[EndChannel | None] = ContextVar(
    "turn_end_channel",
    default=None,
)


def has_turn_channel() -> bool:
    return _channel_id.get() is not None


def get_turn_channel_id() -> str | None:
    return _channel_id.get()


def get_turn_send_text() -> Any | None:
    return _send_text.get()


def get_turn_channel() -> RemoteInteractionChannel | None:
    return _interaction_channel.get()


def get_end_channel() -> EndChannel | None:
    """当前回合的端通道描述（source/通道标识/出站/审批/视图），端接入权威视图。"""
    return _end_channel.get()


@asynccontextmanager
async def turn(
    source: str,
    *,
    channel_id: str = "",
    send_text: Any | None = None,
    interaction_channel: RemoteInteractionChannel | None = None,
    view_workspace: str = "",
    actor: str = "",
):
    """声明本回合属于哪一端（三端平等的唯一入口，``source`` 区分端）。

    三端各自调用：web/matrix 传自己的 channel；CLI 这类无远端通道的端不传
    （channel_id/各 channel 参数留空，自然清空）。防御纵深：某端回合若未能
    在原 context 正确 reset（如 web detach 到后台 task），残留不会泄漏进下
    一端回合——回合一进来就按自己的端重置这组 ContextVar。
    """
    channel_token = _channel_id.set(channel_id or None)
    send_token = _send_text.set(send_text)
    interaction_token = _interaction_channel.set(interaction_channel)
    end_token = _end_channel.set(
        EndChannel(
            source=source,
            channel_id=channel_id,
            send_text=send_text,
            interaction_channel=interaction_channel,
            view_workspace=view_workspace,
            actor=actor,
        )
    )
    try:
        yield
    finally:
        _channel_id.reset(channel_token)
        _send_text.reset(send_token)
        _interaction_channel.reset(interaction_token)
        _end_channel.reset(end_token)


def set_turn_context(
    channel_id: str,
    *,
    send_text: Any | None = None,
    interaction_channel: RemoteInteractionChannel | None = None,
    source: str = "cli-attached",
    view_workspace: str = "",
    actor: str = "",
) -> tuple:
    """Restore turn ContextVars outside an active ``turn`` block.

    Mid-turn continuation inputs deferred by the Matrix ingress (busy turn)
    skip ``turn``, so approvals would silently
    fall back to local or auto-allow. The turn loop re-applies the deferred
    turn context with this helper; callers must reset with
    :func:`reset_turn_context` before the turn ends.

    与 ``turn`` 对齐：同时重建 ``_end_channel``（携带 source），使
    deferred 接续回合里 ``get_end_channel()`` 不再为 None——审批/回投路由
    优先走 EndChannel（方案 D4/D5）时不致退回本地/auto-allow。
    """
    return (
        _channel_id.set(channel_id),
        _send_text.set(send_text),
        _interaction_channel.set(interaction_channel),
        _end_channel.set(
            EndChannel(
                source=source,
                channel_id=channel_id,
                send_text=send_text,
                interaction_channel=interaction_channel,
                view_workspace=view_workspace,
                actor=actor,
            )
        ),
    )


def reset_turn_context(tokens: tuple) -> None:
    """Undo :func:`set_turn_context` — restore the pre-set values.

    容错：Web 回合的 async generator 被 detach 到后台 task 后，reset 发生的
    Context 可能与 set 时不同（``ValueError: ... created in a different Context``）。
    此时回合本就已结束，ContextVar 随 task 销毁自然失效，跨 context reset 失败
    无害——吞掉即可，不应让回合收尾炸出 ERROR 并中断消息流。
    """
    import contextlib

    for var, token in zip(
        (_channel_id, _send_text, _interaction_channel, _end_channel),
        tokens,
        strict=False,
    ):
        with contextlib.suppress(ValueError):
            var.reset(token)
