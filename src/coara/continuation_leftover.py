"""Post-turn leftover continuation dispatch — by item.source, not finishing end.

接续 = 回合未结束时、在迭代头被 drain 进历史的注入（与来自哪端无关）。
回合已结束后仍留在队列里的项 **不是** 接续，而是新回合的启动输入；
每条按 ``ContinuationInput.source`` 开回合，禁止绑死在收尾端上。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from src.core.types import ContinuationInput, unpack_continuation_item


def leftover_item_source(item: str | ContinuationInput, *, fallback: str = "") -> str:
    """Normalize leftover origin; empty/system → fallback（收尾端仅作系统注入兜底）。"""
    if isinstance(item, ContinuationInput):
        src = str(item.source or "").strip().lower()
        if src:
            return src
    return str(fallback or "").strip().lower()


def is_web_source(source: str) -> bool:
    """web 族分派门：与 turn_source.web_shows_source 本体一致（web + web- 前缀）。

    刻意不转发：web_shows_source 带 unknown 门（空来源缺省放行），分派门空串恒 False。
    """
    from src.coara.turn_source import web_shows_source

    return web_shows_source(source, unknown=False)


# 以下两个分派门与 turn_source 族判定存在**有意的语义差异**，勿统一：
# - is_matrix_source 仅认精确 "matrix"：event 来源 leftover 不能走 matrix 回合通道
#   （root.py 唤醒路径的 ("matrix","event") 同路是另一处决策，不适用于分派）
# - is_cli_source 不认 cli- 前缀：cli-* 标签的 leftover 落到「跟收尾端走」兜底分支


def is_matrix_source(source: str) -> bool:
    return str(source or "").strip().lower() == "matrix"


def is_cli_source(source: str) -> bool:
    return str(source or "").strip().lower() in ("cli", "cli-attached")


async def dispatch_leftover_item(
    root: Any,
    coara: Any,
    item: str | ContinuationInput,
    *,
    finishing_source: str,
    bind_ws_id: str | None = None,
    trust_level: str = "owner",
    run_web_turn: Any | None = None,
    run_matrix_turn: Any | None = None,
    run_cli_turn: Any | None = None,
) -> None:
    """Run one leftover queue item as a new turn on its own source.

    *run_web_turn* / *run_matrix_turn* / *run_cli_turn* are async callables
    ``(text, image_blocks) -> None`` provided by the finishing-end host for
    native paths. Cross-end leftovers use root-level bridges when available.
    """
    text, image_blocks = unpack_continuation_item(item)
    src = leftover_item_source(item, fallback=finishing_source)

    if is_web_source(src):
        if run_web_turn is not None:
            await run_web_turn(text, image_blocks)
            return
        await _dispatch_web_via_root(
            root,
            coara,
            text,
            image_blocks,
            bind_ws_id=bind_ws_id,
        )
        return

    if is_matrix_source(src):
        if run_matrix_turn is not None:
            await run_matrix_turn(text, image_blocks)
            return
        await _dispatch_matrix_via_root(
            root,
            coara,
            text,
            image_blocks,
            bind_ws_id=bind_ws_id,
            trust_level=trust_level,
        )
        return

    if is_cli_source(src):
        if run_cli_turn is not None:
            await run_cli_turn(text, image_blocks)
            return
        # 无 attach 回调时仍按 cli-attached 开回合（EndRegistry 有通道则回显）
        await _fallback_process(coara, text, image_blocks, source="cli-attached")
        return

    # 系统注入等无端来源：跟收尾端走，避免丢弃
    if is_web_source(finishing_source) and run_web_turn is not None:
        await run_web_turn(text, image_blocks)
        return
    if is_matrix_source(finishing_source) and run_matrix_turn is not None:
        await run_matrix_turn(text, image_blocks)
        return
    if run_cli_turn is not None:
        await run_cli_turn(text, image_blocks)
        return
    await _fallback_process(
        coara,
        text,
        image_blocks,
        source=finishing_source or "cli-attached",
    )


async def _fallback_process(
    coara: Any,
    text: str,
    image_blocks: list[dict[str, Any]] | None,
    *,
    source: str,
) -> None:
    async for _ in coara.process_message(
        text,
        trust_level="owner",
        show_tool_summary=True,
        image_blocks=image_blocks,
        source=source,
    ):
        pass


async def _dispatch_matrix_via_root(
    root: Any,
    coara: Any,
    text: str,
    image_blocks: list[dict[str, Any]] | None,
    *,
    bind_ws_id: str | None,
    trust_level: str,
) -> None:
    """web/cli 收尾时把 matrix 来源 leftover 开成手机回合。"""
    from src.matrix_client.response_stream import stream_coara_reply_to_matrix

    mnotify = getattr(root, "matrix_notify", None)
    send_text = getattr(mnotify, "send_text", None) if mnotify is not None else None
    room_id = ""
    if mnotify is not None:
        resolve = getattr(mnotify, "resolve_room_id", None)
        if callable(resolve):
            try:
                room_id = str(resolve() or "")
            except Exception:
                # 下方 room_id 为空的分支已有 warning + fallback，这里只留 debug 痕迹
                logger.debug("resolve matrix room id for leftover dispatch failed", exc_info=True)
                room_id = ""
    if not room_id or send_text is None:
        logger.warning(
            "matrix leftover deferred without room/send_text; falling back to process_message"
        )
        await _fallback_process(coara, text, image_blocks, source="matrix")
        return

    async def _send_chunk(rid: str, body: str) -> None:
        await send_text(rid, body)

    await stream_coara_reply_to_matrix(
        root,
        text,
        room_id=room_id,
        trust_level=trust_level,
        send_chunk=_send_chunk,
        image_blocks=image_blocks,
        bind_coara=coara,
        bind_ws_id=bind_ws_id,
    )


async def _dispatch_web_via_root(
    root: Any,
    coara: Any,
    text: str,
    image_blocks: list[dict[str, Any]] | None,
    *,
    bind_ws_id: str | None,
) -> None:
    """matrix/cli 收尾时把 web 来源 leftover 开成 web 回合。"""
    web_server = getattr(root, "_web_server", None)
    stream_fn = getattr(web_server, "stream_leftover_web_turn", None) if web_server is not None else None
    if callable(stream_fn):
        await stream_fn(
            text,
            image_blocks=image_blocks,
            bind_coara=coara,
            bind_ws_id=bind_ws_id,
        )
        return
    await _fallback_process(coara, text, image_blocks, source="web")
