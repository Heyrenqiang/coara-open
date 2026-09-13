"""Detach a running turn from the foreground consumer after workspace switch.

When the user switches away mid-turn, the origin WorkspaceSession must keep
running its current ``process_message`` to completion. Frontends that were
consuming chunks should stop blocking (and stop showing output) while a
background task drains the rest of the async generator.

Re-attach: if the user switches back while the detached turn is still
draining, remaining chunks are forwarded to ``on_detached_item`` (when given)
so the CLI keeps showing the turn's tool lines and text instead of losing
everything produced after the switch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from src.core.logger import logger

T = TypeVar("T")

_StillForeground = Callable[[], bool]
_DrainHook = Callable[[], Awaitable[None] | None]
_DetachedItemSink = Callable[[T], None]

# 等下一个 chunk 期间轮询前台状态的间隔：回合静默期（长工具/等模型首包）
# 没有 chunk 到达，仅靠 chunk 触发检查会让调用方一直卡在消费循环里——
# 切走后的新前台输入（含斜杠命令）随之饿死。
_DETACH_POLL_INTERVAL = 0.2


async def iter_while_foreground(
    agen: AsyncIterator[T],
    still_foreground: _StillForeground,
    *,
    drain_name: str = "detached-workspace-turn",
    on_detach: _DrainHook | None = None,
    on_detached_item: _DetachedItemSink[T] | None = None,
    on_drain_complete: Callable[[], None] | None = None,
) -> AsyncIterator[T]:
    """Yield items while *still_foreground*; then drain the rest in a task.

    Detach triggers on the next item AND on a poll while waiting for one
    (``_DETACH_POLL_INTERVAL``) — a silent turn (long tool call, waiting for
    the first model token) would otherwise pin the consumer in the async-for
    indefinitely, starving inputs queued for the newly-foreground workspace.

    After detach, this async iterator ends so the caller can handle a new
    foreground session. The origin turn continues until the generator
    completes or fails (logged).

    Re-attach: if the workspace becomes foreground again while draining
    (user switched back mid-turn), remaining items are forwarded to
    *on_detached_item* instead of being silently discarded — without it the
    CLI loses the turn's tool lines and text after a switch-away-and-back.
    *on_drain_complete* fires once when the drain ends (success, failure or
    cancel) so the sink can close any display state it opened.
    """

    def _fire_drain_complete() -> None:
        if on_drain_complete is not None:
            try:
                on_drain_complete()
            except Exception:
                logger.exception("Drain-complete hook failed ({})", drain_name)

    async def _drain(first: T | None = None) -> None:
        try:
            if on_detach is not None:
                maybe = on_detach()
                if asyncio.iscoroutine(maybe):
                    await maybe
            # ``first`` was already pulled; route it like the rest.
            if first is not None and on_detached_item is not None and still_foreground():
                on_detached_item(first)
            async for item in agen:
                if on_detached_item is not None and still_foreground():
                    on_detached_item(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Detached workspace turn failed ({})", drain_name)
        finally:
            _fire_drain_complete()

    async def _drain_pending(pending: asyncio.Task[T]) -> None:
        """静默期 detach 的接管入口：agen 已挂起一个 anext，先取回再继续 drain。"""
        try:
            first = await pending
        except StopAsyncIteration:
            # 静默期内回合已自然结束，无残余可 drain；仍发完成钩子保持配对。
            _fire_drain_complete()
            return
        except asyncio.CancelledError:
            # 取消路径同样发完成钩子（docstring 承诺 success/failure/cancel 均配对），
            # 否则 CLI 侧重挂的 streaming block 不闭合。
            _fire_drain_complete()
            raise
        except Exception:
            logger.exception("Detached workspace turn failed ({})", drain_name)
            _fire_drain_complete()
            return
        await _drain(first)

    # 手动 anext + 轮询等待：chunk 到达时照常检查；超时也检查一次前台状态，
    # 保证静默回合在切走后 _DETACH_POLL_INTERVAL 内 detach，消费方及时让位。
    pending: asyncio.Task[T] | None = asyncio.ensure_future(agen.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=_DETACH_POLL_INTERVAL)
            if not done:
                if not still_foreground():
                    asyncio.create_task(_drain_pending(pending), name=drain_name)
                    pending = None  # 所有权移交 drain 任务
                    return
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            if not still_foreground():
                asyncio.create_task(_drain(item), name=drain_name)
                return
            yield item
            pending = asyncio.ensure_future(agen.__anext__())
    finally:
        # 调用方异常放弃迭代时回收挂起的 anext；detach 路径 pending 已移交（None）。
        if pending is not None and not pending.done():
            pending.cancel()


def foreground_workspace_matcher(
    root: object,
    turn_workspace_id: str | None,
    *,
    end: str = "cli",
) -> _StillForeground:
    """True while this end's view is still *turn_workspace_id*.

    Default end=cli（CLI/attach）。Web/Matrix 传入对应 end，避免跟「默认指针」耦死。
    """

    def _still() -> bool:
        if not turn_workspace_id:
            return True
        view_id_fn = getattr(root, "view_workspace_id", None)
        if callable(view_id_fn):
            try:
                current = view_id_fn(end)
                # MagicMock 等假返回值不是 str：回退到属性探测，避免恒 False。
                if isinstance(current, str) or current is None:
                    return str(current or "") == turn_workspace_id
            except Exception:
                pass
        # 兼容旧 shim / 测试替身
        if end == "cli":
            return getattr(root, "_foreground_session_id", None) == turn_workspace_id
        attr = f"_{end}_view_workspace_id"
        return getattr(root, attr, None) == turn_workspace_id

    return _still


def workspace_display_name(root: object, workspace_dir: str) -> str:
    """Resolve *workspace_dir* to the registered workspace name for display tags.

    Falls back to the directory basename when the registry cannot resolve it.
    Returns "" when *workspace_dir* is empty.
    """
    from pathlib import Path

    raw = str(workspace_dir or "").strip()
    if not raw:
        return ""
    try:
        manager = getattr(root, "workspace_manager", None)
        if manager is not None:
            name = manager.name_for_path(raw)
            if name:
                return str(name)
    except Exception:
        pass
    try:
        return Path(raw).name
    except Exception:
        return raw
