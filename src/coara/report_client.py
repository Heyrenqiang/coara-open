"""Outbound HTTP client for ``/report`` → developer webhook.

End-user coara POSTs a packed session report to the developer's
``POST /webhook/<source_id>`` endpoint (typically behind a tunnel). The
developer machine's EventSourceManager receives it as a normal webhook event
into their local 「用户反馈」 workspace — never written on the end-user box.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from src.core.logger import logger

# Keep under developer webhook body limit (2 MiB) with headroom.
_MAX_PAYLOAD_BYTES = 1_500_000
_DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ReportEndpoint:
    url: str
    secret: str


@dataclass(frozen=True, slots=True)
class ReportSendResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None


def resolve_report_endpoint() -> ReportEndpoint | None:
    """Resolve developer webhook: env override → built-in defaults.

    End users do not configure this. Dev-only: ``COARA_REPORT_WEBHOOK_*``.
    """
    from src.coara.report_defaults import (
        DEFAULT_REPORT_WEBHOOK_SECRET,
        DEFAULT_REPORT_WEBHOOK_URL,
    )

    url = (os.environ.get("COARA_REPORT_WEBHOOK_URL") or "").strip()
    secret = (os.environ.get("COARA_REPORT_WEBHOOK_SECRET") or "").strip()
    if not url:
        url = (DEFAULT_REPORT_WEBHOOK_URL or "").strip()
    if not secret:
        secret = (DEFAULT_REPORT_WEBHOOK_SECRET or "").strip()
    if not url:
        return None
    return ReportEndpoint(url=url, secret=secret)


def _truncate_str(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _compact_content(content: Any, *, str_limit: int) -> Any:
    if isinstance(content, str):
        return _truncate_str(content, str_limit)
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = str(block.get("type") or "")
        if btype in ("image", "image_url", "input_image"):
            out.append({"type": btype, "note": "[image omitted]"})
            continue
        if btype == "text":
            out.append({"type": "text", "text": _truncate_str(str(block.get("text") or ""), str_limit)})
            continue
        cleaned = {k: v for k, v in block.items() if k not in ("data", "source", "image_url")}
        if "text" in cleaned and isinstance(cleaned["text"], str):
            cleaned["text"] = _truncate_str(cleaned["text"], str_limit)
        out.append(cleaned)
    return out


def serialize_message_for_report(message: Any, *, content_limit: int = 4000) -> dict[str, Any]:
    role = getattr(message, "role", None)
    role_val = role.value if hasattr(role, "value") else str(role or "")
    row: dict[str, Any] = {
        "role": role_val,
        "content": _compact_content(getattr(message, "content", ""), str_limit=content_limit),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        row["tool_calls"] = [
            {
                "id": getattr(tc, "id", ""),
                "name": getattr(tc, "name", ""),
                "arguments": getattr(tc, "arguments", {}) or {},
            }
            for tc in tool_calls
        ]
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        row["tool_call_id"] = tool_call_id
    name = getattr(message, "name", None)
    if name:
        row["name"] = name
    return row


def build_report_payload(
    *,
    description: str,
    report_id: str,
    dump: dict[str, Any],
) -> dict[str, Any]:
    """Build webhook JSON body; shrink until under ``_MAX_PAYLOAD_BYTES``."""
    messages = list(dump.get("messages") or [])
    content_limit = 4000
    truncated = False
    while True:
        payload: dict[str, Any] = {
            "event_type": "user.report",
            "id": report_id,
            "report_id": report_id,
            "title": description[:120],
            "content": description,
            "description": description,
            "session_id": dump.get("session_id"),
            "source_workspace": dump.get("workspace"),
            "source_workspace_dir": dump.get("workspace_dir"),
            "provider": dump.get("provider"),
            "model": dump.get("model"),
            "message_count": dump.get("message_count"),
            "captured_at": dump.get("captured_at"),
            "status": dump.get("status") or {},
            "messages": messages,
            "truncated": truncated,
        }
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        if len(raw) <= _MAX_PAYLOAD_BYTES:
            return payload
        # Shrink: drop oldest messages, then tighten content limits.
        if len(messages) > 8:
            drop = max(1, len(messages) // 4)
            messages = messages[drop:]
            truncated = True
            continue
        if content_limit > 500:
            content_limit = max(500, content_limit // 2)
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg["content"] = _compact_content(msg.get("content"), str_limit=content_limit)
            truncated = True
            continue
        # Last resort: drop messages entirely, keep description + meta.
        payload["messages"] = []
        payload["truncated"] = True
        payload["truncation_note"] = "session messages omitted (payload too large)"
        return payload


async def send_report(endpoint: ReportEndpoint, payload: dict[str, Any]) -> ReportSendResult:
    """POST *payload* to the developer webhook URL."""
    import httpx

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if endpoint.secret:
        headers["Authorization"] = f"Bearer {endpoint.secret}"
        headers["X-Coara-Webhook-Token"] = endpoint.secret

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            resp = await client.post(endpoint.url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return ReportSendResult(ok=False, error="发送超时，请稍后重试")
    except Exception as exc:
        logger.warning(f"report webhook send failed: {exc}")
        return ReportSendResult(ok=False, error=f"发送失败：{exc}")

    if 200 <= resp.status_code < 300:
        return ReportSendResult(ok=True, status_code=resp.status_code)

    detail = (resp.text or "").strip()[:200]
    if resp.status_code == 401:
        err = "开发者接收端鉴权失败（检查 report.webhook_secret）"
    elif resp.status_code == 404:
        err = "开发者接收端未找到该 webhook（检查 URL 与事件源 id）"
    elif resp.status_code == 413:
        err = "报告内容过大，接收端拒绝"
    else:
        err = f"开发者接收端返回 HTTP {resp.status_code}"
        if detail:
            err = f"{err}：{detail}"
    return ReportSendResult(ok=False, status_code=resp.status_code, error=err)
