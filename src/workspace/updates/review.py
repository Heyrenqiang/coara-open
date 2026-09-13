"""批示路由：批示 + 内容引用注入所属工作空间的会话。

Web（/api/updates/review）与 Matrix（[COARA_UPDATE_CMD]）两个入口共用。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.workspace.updates.store import WorkspaceUpdatesStore
    from src.workspace.updates.types import WorkspaceUpdate


class ReviewError(Exception):
    """批示失败：消息文本可直接回给用户。"""


def _updates_store(root: Any) -> WorkspaceUpdatesStore | None:
    getter = getattr(root, "_updates_store", None)
    if getter is None:
        return None
    return getter()


def get_update(root: Any, message_id: str) -> WorkspaceUpdate:
    store = _updates_store(root)
    msg = store.get(message_id) if store is not None else None
    if msg is None:
        raise ReviewError("动态不存在或已清理")
    return msg


def build_review_content(msg: WorkspaceUpdate, text: str) -> str:
    """批示输入 = 内容引用块 + 批示正文。"""
    summary = (msg.display_text or msg.text or "").strip()
    if len(summary) > 1500:
        summary = summary[:1500] + "…"
    quote = (
        f'<工作空间消息 workspace="{msg.workspace}" message_id="{msg.message_id}">\n'
        f"{msg.title}\n{summary}\n</工作空间消息>"
    )
    return f"{quote}\n\n{text.strip()}"


async def review_workspace_update(root: Any, *, message_id: str, text: str) -> dict[str, str]:
    """把批示路由到内容所属空间的会话。忙则进接续队列，闲则后台起回合。"""
    if not text.strip():
        raise ReviewError("批示不能为空")
    msg = get_update(root, message_id)
    wm = getattr(root, "workspace_manager", None)
    entry = wm.registry.resolve_name_or_id(msg.workspace) if wm is not None else None
    if entry is None:
        raise ReviewError(f"工作空间未登记：{msg.workspace}")

    session = await root.ensure_workspace_session(entry)
    content = build_review_content(msg, text)
    coara = session.coara
    if coara.has_active_turn() or coara._process_lock.locked():
        coara.submit_continuation_input(content)
        delivered = "queued"
    else:
        asyncio.create_task(_run_review_turn(coara, content))
        delivered = "started"

    store = _updates_store(root)
    if store is not None:
        store.mark_read(message_id)
        # 处置轨迹：用户批示 = 该消息已被用户处置
        store.set_disposition(message_id, "resolved", reviewed_by="user", note=text.strip()[:200])
    push = getattr(root, "_push_workspace_updates_state_to_matrix", None)
    if push is not None:
        try:
            await push()
        except Exception as exc:
            logger.debug(f"updates state push after review skipped: {exc}")
    return {"delivered": delivered, "workspace": msg.workspace}


async def _run_review_turn(coara: Any, content: str) -> None:
    """在消息所属空间的会话里跑批示回合（输出随 trace 进历史）。"""
    try:
        async for _ in coara.process_message(content, trust_level="owner", source="review"):
            pass
        await asyncio.to_thread(coara.persist_session_to_disk)
    except Exception as exc:
        logger.warning(f"review turn failed: {exc}")
