"""Compact tool argument summaries for usage accounting (no large payloads)."""

from __future__ import annotations

from typing import Any

_MAX_STR = 240


def _trim(value: Any, *, limit: int = _MAX_STR) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if len(text) > limit:
            return text[: limit - 1] + "…"
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_trim(item, limit=min(limit, 80)) for item in value[:8]]
    if isinstance(value, dict):
        return {str(k): _trim(v, limit=80) for k, v in list(value.items())[:12]}
    text = str(value).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def compact_tool_usage_args(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Extract a small, stable argument summary for usage JSONL."""
    if not isinstance(arguments, dict):
        return {}

    if tool_name == "web_search":
        return {k: _trim(arguments.get(k)) for k in ("query",) if arguments.get(k) not in (None, "")}

    if tool_name == "web_fetch":
        out: dict[str, Any] = {}
        if arguments.get("url"):
            out["url"] = _trim(arguments["url"], limit=512)
        if arguments.get("max_chars") is not None:
            out["max_chars"] = arguments.get("max_chars")
        return out

    if tool_name == "delegate":
        return {
            k: _trim(arguments.get(k))
            for k in ("subagent_type", "description", "workspace")
            if arguments.get(k) not in (None, "")
        } | ({"background": bool(arguments["background"])} if arguments.get("background") is not None else {})

    if tool_name == "shell":
        cmd = arguments.get("command") or arguments.get("cmd")
        return {"command": _trim(cmd, limit=160)} if cmd else {}

    if tool_name in {"read", "write", "edit", "delete", "grep", "glob"}:
        key = "path" if "path" in arguments else "pattern" if "pattern" in arguments else None
        if key and arguments.get(key):
            return {key: _trim(arguments[key], limit=512)}
        return {k: _trim(v, limit=120) for k, v in list(arguments.items())[:4]}

    return {k: _trim(v, limit=120) for k, v in list(arguments.items())[:6]}


# 录像带详情：比用量摘要多留正文类字段（write/edit），仍截断防撑带
_TAPE_BODY_LIMIT = 8_000


def tape_tool_arguments(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """录像带 tool 帧用的参数：在用量摘要之上补 write/edit 正文预览"""
    base = compact_tool_usage_args(tool_name, arguments)
    if not isinstance(arguments, dict):
        return base
    if tool_name == "write":
        body = arguments.get("contents")
        if body is None:
            body = arguments.get("content")
        if body not in (None, ""):
            base = {**base, "contents": _trim(body, limit=_TAPE_BODY_LIMIT)}
        return base
    if tool_name == "edit":
        extra: dict[str, Any] = {}
        for key in ("old_string", "new_string", "old_str", "new_str"):
            if arguments.get(key) not in (None, ""):
                extra[key] = _trim(arguments[key], limit=_TAPE_BODY_LIMIT)
        return {**base, **extra} if extra else base
    if tool_name == "shell":
        cmd = arguments.get("command") or arguments.get("cmd")
        if cmd:
            return {"command": _trim(cmd, limit=2_000)}
    return base


__all__ = ["compact_tool_usage_args", "tape_tool_arguments"]
