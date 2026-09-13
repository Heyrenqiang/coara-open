"""草案 ↔ 构建对话会话绑定注册表（每草案一个 section 一个上下文）。

草案是工作流的活模型，构建对话（FlowRoot）是它的会话：一份草案对应一个
session_id。切草案 = 切会话；绑定关系持久化在系统工作流资产目录
（``<workflows>/draft_sessions.json``），与草案同生命周期（删草案即解绑，
录像带历史保留在系统带中可审计）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.workflow.paths import workflow_assets_root

_FILENAME = "draft_sessions.json"


def _path(coara_home: Path | str | None) -> Path:
    return workflow_assets_root(coara_home) / _FILENAME


def _load(coara_home: Path | str | None) -> dict[str, Any]:
    path = _path(coara_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(coara_home: Path | str | None, data: dict[str, Any]) -> None:
    path = _path(coara_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, data)


def session_for(draft_id: str, coara_home: Path | str | None = None) -> str | None:
    """草案绑定的构建会话 session_id；未绑定返回 None。"""
    entry = (_load(coara_home).get("sessions") or {}).get(draft_id)
    if not isinstance(entry, dict):
        return None
    sid = str(entry.get("session_id") or "").strip()
    return sid or None


def bind_session(draft_id: str, session_id: str, coara_home: Path | str | None = None) -> None:
    data = _load(coara_home)
    sessions = data.setdefault("sessions", {})
    sessions[draft_id] = {
        "session_id": session_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _save(coara_home, data)


def unbind(draft_id: str, coara_home: Path | str | None = None) -> None:
    """删草案时解绑（带内历史保留，仅切断找回通道）。"""
    data = _load(coara_home)
    sessions = data.get("sessions") or {}
    changed = sessions.pop(draft_id, None) is not None
    if data.get("last_active") == draft_id:
        data.pop("last_active", None)
        changed = True
    if changed:
        _save(coara_home, data)


def last_active(coara_home: Path | str | None = None) -> str | None:
    """上次切到的草案：进程冷启动时把构建对话恢复到同一个 section。"""
    value = str(_load(coara_home).get("last_active") or "").strip()
    return value or None


def mark_active(draft_id: str, coara_home: Path | str | None = None) -> None:
    data = _load(coara_home)
    if data.get("last_active") == draft_id:
        return
    data["last_active"] = draft_id
    _save(coara_home, data)


__all__ = ["bind_session", "last_active", "mark_active", "session_for", "unbind"]
