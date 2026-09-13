"""会话事件日志归档：压缩语义 → compaction 事件（历史可审计）。

压缩路径（阈值压缩 / provider 溢出强制压缩）只写一条 ``compaction/summary``
标记（摘要文本、split_point、影子区间元数据）。被替代的消息事件本身已在
persist 边界落盘，不在此重写（防双写）。写失败仅静默（不影响压缩主流程）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.types import Message
from src.session_log.store import append_with_seq, resolve_session_log_path
from src.session_log.types import EVENT_COMPACTION_SUMMARY, build_event


def archive_compressed_messages(
    *,
    workspace_dir: str | Path,
    session_id: str,
    coara_id: str = "",
    coara_name: str = "",
    agent_kind: str = "",
    messages: list[Message],
    info: dict[str, Any],
    summary_text: str = "",
    coara_home: Path | None = None,
) -> None:
    """把压缩语义（摘要 + 影子区间元数据）归档为一条 ``compaction/summary``。

    被替代的消息事件已在 persist 边界落过盘，本函数**不重写消息事件**
    （避免与对账线双写）。失败不抛错（压缩主流程不受影响）；
    ``messages`` 为空时为空操作。
    """
    if not messages:
        return

    def _build(start: int) -> list[dict[str, Any]]:
        # 被替代的消息事件已在 persist 边界落过盘，这里只写语义归档标记
        # （摘要 + 影子区间）——重写消息事件会与对账线双写，靠 shadow 收敛
        # 既冗余又污染审计视图。
        summary_payload: dict[str, Any] = {
            # 本批不产生消息事件：区间为空，投影端跳过
            "shadow_start_seq": None,
            "shadow_end_seq": None,
            "info": {k: info.get(k) for k in ("split_point", "original_count", "compressed_count") if k in info},
        }
        if summary_text:
            summary_payload["summary"] = summary_text
        return [
            build_event(
                kind=EVENT_COMPACTION_SUMMARY,
                seq=start,
                session_id=session_id,
                payload=summary_payload,
                coara_id=coara_id,
                coara_name=coara_name,
                agent_kind=agent_kind,
            )
        ]

    try:
        path = resolve_session_log_path(workspace_dir, coara_home=coara_home)
        append_with_seq(path, _build)
    except Exception:
        # 归档是尽力而为的派生视图：任何失败都不能影响压缩与回合主流程
        return


def archive_compressed_history_for(
    coara: Any,
    *,
    original: list[Message],
    compressed: list[Message],
    info: dict[str, Any],
) -> None:
    """回合侧归档入口：把 ``original[:split_point]`` 归档为会话事件。

    供强制溢出压缩（turn_orchestrator）与常规阈值压缩（context_prep）共用；
    任何失败静默（不影响压缩与回合主流程）。
    """
    try:
        split = 0
        try:
            split = int(info.get("split_point") or 0)
        except (TypeError, ValueError):
            split = 0
        if split <= 0:
            return
        summary_text = ""
        # 头部保护后 compressed[0] 可能是环境种子（<系统消息>），摘要在其后——
        # 需遍历找含 <state_snapshot> 的摘要消息，不能只看首条。
        for msg in compressed:
            content = str(getattr(msg, "content", "") or "")
            if "<state_snapshot" in content:
                summary_text = content
                break
        identity = getattr(coara, "identity", None)
        archive_compressed_messages(
            workspace_dir=coara.workspace_dir,
            session_id=coara.session_id,
            coara_id=str(getattr(identity, "coara_id", "") or ""),
            coara_name=str(getattr(identity, "name", "") or ""),
            agent_kind="",
            messages=list(original[:split]),
            info=info,
            summary_text=summary_text,
        )
    except Exception:
        # 归档是尽力而为的派生视图：任何失败都不能影响压缩与回合主流程
        from src.core.logger import logger

        logger.debug("session event log archive skipped", exc_info=True)


__all__ = [
    "archive_compressed_history_for",
    "archive_compressed_messages",
]
