"""Slash command ``/report``: pack session detail and POST to developer webhook.

Flow
----
1. ``/report`` alone → enter pending mode; prompt user to describe the issue.
2. Next plain message → submit description + session dump via HTTP to the
   developer-configured webhook (their local 「用户反馈」 workspace).
3. ``/report 描述…`` → submit immediately.

This never writes a local 「用户反馈」 workspace on the end-user machine.
Pending description is abandoned if the user sends another slash command instead.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from src.coara.commands.registry import CommandArgs, register, resolve_target_coara
from src.coara.commands.types import CommandResult
from src.coara.report_client import (
    build_report_payload,
    resolve_report_endpoint,
    send_report,
    serialize_message_for_report,
)
from src.core.time import now_iso

if TYPE_CHECKING:
    from src.coara.root import RootCoara

_pending_sessions: set[str] = set()


def _session_key(coara) -> str | None:
    if coara is None:
        return None
    sid = str(getattr(coara, "session_id", "") or "").strip()
    return sid or None


def _resolve_pending_target(root: RootCoara, coara=None):
    """pending 流的目标主体：内部链路传 pin 空间，外部调用方（仅前台语义）缺省前台。"""
    if coara is not None:
        return coara
    try:
        return root.foreground_coara
    except Exception:
        return None


def has_pending_report(root: RootCoara, coara=None) -> bool:
    key = _session_key(_resolve_pending_target(root, coara))
    return bool(key and key in _pending_sessions)


def begin_pending_report(root: RootCoara, coara=None) -> bool:
    key = _session_key(_resolve_pending_target(root, coara))
    if not key:
        return False
    _pending_sessions.add(key)
    return True


def clear_pending_report(root: RootCoara, coara=None) -> bool:
    key = _session_key(_resolve_pending_target(root, coara))
    if not key or key not in _pending_sessions:
        return False
    _pending_sessions.discard(key)
    return True


def clear_all_pending_report() -> None:
    """Test / shutdown helper."""
    _pending_sessions.clear()


def _description_from_args(args: CommandArgs) -> str:
    raw = (args.raw or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def build_session_dump(root: RootCoara, coara=None) -> dict[str, Any]:
    """Session detail for the report payload."""
    if coara is None:
        coara = root.foreground_coara
    status: dict[str, Any] = {}
    try:
        status = dict(root.get_status() or {})
    except Exception:
        try:
            status = dict(coara.get_status() or {})
        except Exception:
            status = {}

    history = list(getattr(coara, "message_history", None) or [])
    workspace_name = ""
    try:
        workspace_name = str(root.foreground_active_name() or "")
    except Exception:
        workspace_name = ""

    return {
        "captured_at": now_iso(),
        "session_id": str(getattr(coara, "session_id", "") or ""),
        "workspace": workspace_name,
        "workspace_dir": str(getattr(coara, "workspace_dir", "") or status.get("workspace_dir") or ""),
        "provider": str(status.get("provider") or getattr(coara, "provider_name", "") or ""),
        "model": str(status.get("model") or getattr(coara, "model_name", "") or ""),
        "message_count": len(history),
        "messages": [serialize_message_for_report(m) for m in history],
        "status": {
            k: status.get(k)
            for k in (
                "name",
                "status",
                "provider",
                "model",
                "workspace_dir",
                "message_count",
                "tools",
                "skills",
            )
            if k in status
        },
    }


async def submit_user_report(root: RootCoara, description: str, coara=None) -> CommandResult:
    """Pack session + description and POST to the developer webhook."""
    description = (description or "").strip()
    if not description:
        return CommandResult.error("请先描述遇到的问题，再提交报告")

    endpoint = resolve_report_endpoint()
    if endpoint is None:
        return CommandResult.error("报告通道尚未就绪（开发者接收地址未配置）。请稍后再试或联系开发者。")

    dump = build_session_dump(root, coara)
    report_id = uuid.uuid4().hex[:12]
    dump["report_id"] = report_id
    dump["description"] = description
    payload = build_report_payload(description=description, report_id=report_id, dump=dump)

    result = await send_report(endpoint, payload)
    if not result.ok:
        return CommandResult.error(
            result.error or "发送失败",
            report_id=report_id,
            status_code=result.status_code,
        )

    truncated = bool(payload.get("truncated"))
    lines = [
        "已发送给开发者。",
        "问题描述与本轮会话详情已通过网络提交。",
    ]
    if truncated:
        lines.append("（会话过长，部分历史已压缩后发送。）")
    return CommandResult(
        output="\n".join(lines),
        data={
            "report_id": report_id,
            "status_code": result.status_code,
            "truncated": truncated,
            "message_count": dump.get("message_count"),
        },
    )


def try_consume_pending_report(root: RootCoara, user_input: str) -> CommandResult | None:
    """If a report description is pending, mark *user_input* for async submit.

    Returns a marker with ``needs_async_submit`` for plain text, or ``None``
    when nothing pending / pending abandoned for another slash.
    """
    if not has_pending_report(root):
        return None

    stripped = (user_input or "").strip()
    if not stripped:
        return None

    if stripped.startswith("/"):
        clear_pending_report(root)
        return None

    clear_pending_report(root)
    return CommandResult(
        output="",
        data={"needs_async_submit": True, "description": stripped},
    )


async def try_consume_pending_report_async(root: RootCoara, user_input: str) -> CommandResult | None:
    """Consume pending report description (async submit)."""
    marker = try_consume_pending_report(root, user_input)
    if marker is None:
        return None
    if marker.data.get("needs_async_submit"):
        return await submit_user_report(root, str(marker.data.get("description") or ""))
    return marker


@register("report")
async def handle_report(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Start report collection or submit inline description."""
    coara = resolve_target_coara(root, args)
    description = _description_from_args(args)
    if description:
        clear_pending_report(root, coara)
        return await submit_user_report(root, description, coara)

    if not begin_pending_report(root, coara):
        return CommandResult.error("无法开始报告：当前没有活动会话")

    return CommandResult(
        output=("请描述遇到的问题（下一条消息将发送给开发者，并附带本轮完整会话详情）。"),
        data={"pending": True},
    )
