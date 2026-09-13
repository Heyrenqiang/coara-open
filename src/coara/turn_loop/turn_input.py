"""User-turn input preparation (environment seed, history append)."""

from __future__ import annotations

from typing import Any

from src.coara.turn_loop.user_turn_injectors import DEFAULT_USER_TURN_INJECTORS, UserTurnContext


async def begin_user_turn(
    coara,
    content: str,
    *,
    image_blocks: list[dict[str, Any]] | None = None,
) -> str:
    """Run user-turn injectors, append user message, emit trace events."""
    ctx = UserTurnContext(content=content, image_blocks=image_blocks)

    for injector in DEFAULT_USER_TURN_INJECTORS:
        await injector(coara, ctx)

    active_turn = getattr(coara, "_active_turn", None)
    turn_id = str(getattr(active_turn, "turn_id", "") or "")
    source = str(getattr(coara, "_active_turn_source", "") or "")
    coara._emit_trace(
        "conversation_message",
        ctx.content,
        payload={
            "role": "user",
            "content": ctx.content,
            "turn_id": turn_id,
            "source": source,
        },
    )
    coara._emit_trace(
        "message_received",
        "Received input message",
        payload={
            "is_owner_context": coara.identity.is_owner_context,
            "content_preview": ctx.content[:120],
            "turn_id": turn_id,
        },
    )
    return ctx.content
