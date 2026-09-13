"""Human-friendly update display text (storage keeps full payload)."""

from __future__ import annotations

from typing import Any

# Keys shown in mobile/UI summaries — everything else stays in stored payload only.
_DISPLAY_META_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("feedback_id", ("feedback_id", "id")),
    ("app_version", ("app_version", "appVersion", "version_name", "app_version_name")),
    ("device", ("device_model", "model", "device_name", "device")),
    ("device_brand", ("device_brand", "manufacturer", "brand")),
    ("os", ("os_version", "android_version", "os")),
)

_CONTENT_KEYS = ("content", "feedback", "message", "body", "text", "details")


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _display_content(body: dict[str, Any]) -> str | None:
    """面向用户展示的正文：剥掉 ``<仅模型可见>`` 段，剥空按无内容处理。

    存储侧（``WorkspaceUpdate.text`` / ``payload``）保留原文——``review()`` 把
    ``msg.text`` 注入会话时，模型仍要读到那条行为指令。
    """
    from src.core.message_tags import strip_llm_only

    content = _first_str(body, _CONTENT_KEYS)
    if not content:
        return None
    return strip_llm_only(content).strip() or None


def event_payload_body(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event")
    if isinstance(event, dict):
        body = event.get("payload")
        if isinstance(body, dict):
            return body
    return payload if isinstance(payload, dict) else {}


def format_update_display_text(*, payload: dict[str, Any], fallback_text: str = "") -> str:
    """Compact markdown for phone / notifications; full data remains in the update JSON."""
    body = event_payload_body(payload)
    feedback_id = _first_str(body, ("feedback_id", "id"))
    app_version = _first_str(body, _DISPLAY_META_KEYS[1][1])
    device = _first_str(body, _DISPLAY_META_KEYS[2][1])
    brand = _first_str(body, _DISPLAY_META_KEYS[3][1])
    os_version = _first_str(body, _DISPLAY_META_KEYS[4][1])
    content = _display_content(body)

    device_label = " ".join(part for part in (brand, device) if part).strip() or device
    meta_parts: list[str] = []
    if feedback_id is not None:
        meta_parts.append(f"#{feedback_id}")
    if app_version:
        meta_parts.append(f"App {app_version}")
    if device_label:
        meta_parts.append(device_label)
    if os_version:
        meta_parts.append(os_version)

    lines: list[str] = []
    if meta_parts:
        lines.append(" · ".join(meta_parts))
    if content:
        lines.append(content)
    if lines:
        return "\n\n".join(lines)

    from src.core.message_tags import strip_llm_only

    return strip_llm_only(fallback_text).strip() or "(无摘要)"


def format_update_display_title(*, payload: dict[str, Any], fallback: str = "") -> str:
    body = event_payload_body(payload)
    content = _display_content(body)
    if content:
        one_line = content.replace("\n", " ")
        return one_line[:120] + ("…" if len(one_line) > 120 else "")
    feedback_id = _first_str(body, ("feedback_id", "id"))
    if feedback_id is not None:
        return f"反馈 #{feedback_id}"
    from src.core.message_tags import strip_llm_only

    fallback_clean = strip_llm_only(fallback).strip()
    return fallback_clean[:120] if fallback_clean else "工作空间动态"
