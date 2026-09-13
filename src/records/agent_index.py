"""index.yaml maintenance for the memory store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.core.json_store import write_text_atomic
from src.records.agent_types import MemoryEntry
from src.records.store_helpers import now_iso as _now_iso

INDEX_VERSION = 1


def empty_index() -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "updated_at": _now_iso(),
        "by_type": {},
        "by_tag": {},
        "by_hash": {},
        "recent": [],
    }


def load_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return empty_index()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return empty_index()
    if not isinstance(raw, dict):
        return empty_index()
    raw.setdefault("version", INDEX_VERSION)
    raw.setdefault("by_type", {})
    raw.setdefault("by_tag", {})
    raw.setdefault("by_hash", {})
    raw.setdefault("recent", [])
    # 旧版 index 可能带 paused；写入开关已迁到 config records.enabled，忽略该字段
    raw.pop("paused", None)
    return raw


def save_index(path: Path, index: dict[str, Any]) -> None:
    index["updated_at"] = _now_iso()
    index["version"] = INDEX_VERSION
    write_text_atomic(
        path,
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False, default_flow_style=False),
    )


def _entry_summary(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "path": entry.path,
        "created_at": entry.created_at.isoformat(),
        "tags": list(entry.tags or []),
        "title": entry.title,
        "type": entry.type,
        "status": entry.status,
        "content_hash": entry.content_hash,
    }


def upsert_index_entry(index: dict[str, Any], entry: MemoryEntry) -> None:
    """Insert or replace an entry in all index views."""
    remove_index_entry(index, entry.id)

    summary = _entry_summary(entry)
    by_type = index.setdefault("by_type", {})
    type_list = by_type.setdefault(entry.type, [])
    if not isinstance(type_list, list):
        type_list = []
        by_type[entry.type] = type_list
    type_list.insert(0, summary)

    by_tag = index.setdefault("by_tag", {})
    for tag in entry.tags or []:
        key = str(tag).strip()
        if not key:
            continue
        ids = by_tag.setdefault(key, [])
        if not isinstance(ids, list):
            ids = []
            by_tag[key] = ids
        if entry.id not in ids:
            ids.insert(0, entry.id)

    if entry.content_hash:
        ids = index.setdefault("by_hash", {}).setdefault(entry.content_hash, [])
        if not isinstance(ids, list):
            ids = [ids] if ids else []
            index["by_hash"][entry.content_hash] = ids
        if entry.id not in ids:
            ids.insert(0, entry.id)

    recent = index.setdefault("recent", [])
    if not isinstance(recent, list):
        recent = []
        index["recent"] = recent
    recent.insert(0, summary)
    index["recent"] = recent[:200]


def remove_index_entry(index: dict[str, Any], memory_id: str) -> None:
    """Remove an id from by_type / by_tag / recent / by_hash."""
    by_type = index.get("by_type") or {}
    if isinstance(by_type, dict):
        for key, items in list(by_type.items()):
            if not isinstance(items, list):
                continue
            by_type[key] = [i for i in items if not (isinstance(i, dict) and i.get("id") == memory_id)]

    by_tag = index.get("by_tag") or {}
    if isinstance(by_tag, dict):
        for key, ids in list(by_tag.items()):
            if isinstance(ids, list):
                by_tag[key] = [i for i in ids if i != memory_id]

    by_hash = index.get("by_hash") or {}
    if isinstance(by_hash, dict):
        for h, val in list(by_hash.items()):
            ids = val if isinstance(val, list) else [val]
            ids = [i for i in ids if i != memory_id]
            if ids:
                by_hash[h] = ids
            else:
                del by_hash[h]

    recent = index.get("recent") or []
    if isinstance(recent, list):
        index["recent"] = [i for i in recent if not (isinstance(i, dict) and i.get("id") == memory_id)]
