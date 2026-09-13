"""File-backed user-records store (Markdown + YAML frontmatter + index.yaml).

Layout under ``records/user/``::

    index.yaml
    entries/
        <slug>.md
    files/
        <entry_id>/
            <filename>

Simpler than the agent store by design: no write gate (the user curates),
no archive lifecycle (items only leave when the user removes them).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from src.core.json_store import write_text_atomic

# 通用 helper 单源在 store_helpers；此处 import 即对外再导出（facade 仍从本模块取 content_hash）
from src.records.store_helpers import (
    content_hash,
    generate_record_id,
    now_iso,
    parse_dt,
)
from src.records.store_helpers import now_local as _now
from src.records.store_helpers import slugify as _slugify
from src.records.user_types import (
    COLLECTION_SOURCE_TYPES,
    CollectionEntry,
    CollectionSourceType,
)

INDEX_VERSION = 1


def generate_collection_id(when: datetime | None = None) -> str:
    return generate_record_id("col", when)


def _empty_index() -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "updated_at": now_iso(),
        "by_tag": {},
        "by_hash": {},
        "by_url": {},
        "recent": [],
    }


def _load_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_index()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return _empty_index()
    if not isinstance(raw, dict):
        return _empty_index()
    raw.setdefault("version", INDEX_VERSION)
    raw.setdefault("by_tag", {})
    raw.setdefault("by_hash", {})
    raw.setdefault("by_url", {})
    raw.setdefault("recent", [])
    return raw


def _save_index(path: Path, index: dict[str, Any]) -> None:
    index["updated_at"] = now_iso()
    index["version"] = INDEX_VERSION
    write_text_atomic(
        path,
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False, default_flow_style=False),
    )


def _entry_summary(entry: CollectionEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "path": entry.path,
        "created_at": entry.created_at.isoformat(),
        "title": entry.title,
        "source_type": entry.source_type,
        "source_url": entry.source_url,
        "tags": list(entry.tags or []),
        "content_hash": entry.content_hash,
    }


def _upsert_index(index: dict[str, Any], entry: CollectionEntry) -> None:
    _remove_from_index(index, entry.id)

    summary = _entry_summary(entry)
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
    if entry.source_url:
        ids = index.setdefault("by_url", {}).setdefault(entry.source_url, [])
        if not isinstance(ids, list):
            ids = [ids] if ids else []
            index["by_url"][entry.source_url] = ids
        if entry.id not in ids:
            ids.insert(0, entry.id)

    recent = index.setdefault("recent", [])
    if not isinstance(recent, list):
        recent = []
        index["recent"] = recent
    recent.insert(0, summary)
    index["recent"] = recent[:300]


def _remove_from_index(index: dict[str, Any], entry_id: str) -> None:
    by_tag = index.get("by_tag") or {}
    if isinstance(by_tag, dict):
        for key, ids in list(by_tag.items()):
            if isinstance(ids, list):
                by_tag[key] = [i for i in ids if i != entry_id]

    by_hash = index.get("by_hash") or {}
    if isinstance(by_hash, dict):
        for h, val in list(by_hash.items()):
            ids = val if isinstance(val, list) else [val]
            ids = [i for i in ids if i != entry_id]
            if ids:
                by_hash[h] = ids
            else:
                del by_hash[h]

    by_url = index.get("by_url") or {}
    if isinstance(by_url, dict):
        for u, val in list(by_url.items()):
            ids = val if isinstance(val, list) else [val]
            ids = [i for i in ids if i != entry_id]
            if ids:
                by_url[u] = ids
            else:
                del by_url[u]

    recent = index.get("recent") or []
    if isinstance(recent, list):
        index["recent"] = [i for i in recent if not (isinstance(i, dict) and i.get("id") == entry_id)]


class CollectionStore:
    """Markdown file store under ``records/user/``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.index_path = self.root / "index.yaml"
        # 串行化写路径：index.yaml 的 load→mutate→save 不再交错（last-writer-wins）
        self._write_lock = asyncio.Lock()
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.is_file():
            _save_index(self.index_path, _empty_index())

    def _load_index(self) -> dict[str, Any]:
        return _load_index(self.index_path)

    def _save_index(self, index: dict[str, Any]) -> None:
        _save_index(self.index_path, index)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def _entry_abs_path(self, entry: CollectionEntry) -> Path:
        if entry.path:
            return self.root / entry.path
        slug = _slugify(entry.title, entry.id)
        rel = Path("entries") / f"{slug}.md"
        entry.path = rel.as_posix()
        return self.root / rel

    def _serialize(self, entry: CollectionEntry) -> str:
        meta: dict[str, Any] = {
            "id": entry.id,
            "created_at": entry.created_at.isoformat(),
            "source_type": entry.source_type,
            "source_url": entry.source_url,
            "title": entry.title,
            "tags": list(entry.tags or []),
            "content_hash": entry.content_hash,
            "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else None,
            "access_count": entry.access_count,
        }
        parts = ["## 摘要", "", (entry.summary or "").strip()]
        if entry.note.strip():
            parts.extend(["", "## 用户注记", "", entry.note.strip()])
        if entry.content.strip():
            parts.extend(["", "## 原文", "", entry.content.strip()])
        body = "\n".join(parts).strip() + "\n"
        post = frontmatter.Post(body, **meta)
        return frontmatter.dumps(post)

    def _parse_file(self, path: Path, *, rel: str = "") -> CollectionEntry | None:
        try:
            post = frontmatter.load(path)
        except Exception:
            return None
        meta = dict(post.metadata or {})
        cid = str(meta.get("id") or "").strip()
        if not cid:
            return None

        source_type = str(meta.get("source_type") or "snippet").strip()
        if source_type not in COLLECTION_SOURCE_TYPES:
            source_type = "snippet"
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        body = str(post.content or "")
        summary, note, content = _split_body_sections(body)

        return CollectionEntry(
            id=cid,
            created_at=parse_dt(meta.get("created_at")) or _now(),
            source_type=source_type,  # type: ignore[arg-type]
            source_url=str(meta.get("source_url") or ""),
            title=str(meta.get("title") or path.stem),
            summary=summary,
            note=note,
            content=content,
            tags=[str(t) for t in tags],
            content_hash=str(meta.get("content_hash") or ""),
            last_accessed=parse_dt(meta.get("last_accessed")),
            access_count=int(meta.get("access_count") or 0),
            path=rel or path.relative_to(self.root).as_posix(),
        )

    def _iter_md_files(self) -> list[Path]:
        if not self.entries_dir.is_dir():
            return []
        return sorted(self.entries_dir.rglob("*.md"))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def write(self, entry: CollectionEntry) -> str:
        """Write an entry to disk and update the index. Returns id."""
        async with self._write_lock:
            if not entry.content_hash:
                entry.content_hash = content_hash(entry.content or entry.summary)
            if not entry.created_at:
                entry.created_at = _now()
            if entry.last_accessed is None:
                entry.last_accessed = entry.created_at
            abs_path = self._entry_abs_path(entry)
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if abs_path.exists():
                existing = self._parse_file(abs_path, rel=entry.path)
                if existing and existing.id != entry.id:
                    abs_path = abs_path.with_name(f"{abs_path.stem}_{entry.id[-6:]}.md")
                    entry.path = abs_path.relative_to(self.root).as_posix()
            write_text_atomic(abs_path, self._serialize(entry))
            index = self._load_index()
            _upsert_index(index, entry)
            self._save_index(index)
            return entry.id

    async def read(self, entry_id: str) -> CollectionEntry | None:
        entry_id = (entry_id or "").strip()
        if not entry_id:
            return None
        index = self._load_index()
        for item in index.get("recent") or []:
            if isinstance(item, dict) and item.get("id") == entry_id:
                rel = str(item.get("path") or "")
                if rel:
                    path = self.root / rel
                    if path.is_file():
                        return self._parse_file(path, rel=rel)
        for path in self._iter_md_files():
            entry = self._parse_file(path, rel=path.relative_to(self.root).as_posix())
            if entry and entry.id == entry_id:
                return entry
        return None

    async def touch(self, entry_id: str) -> None:
        await self.touch_many([entry_id])

    async def touch_many(self, entry_ids: list[str]) -> None:
        """批量 touch：一次持锁串行写，避免搜索命中逐条盘写放大。"""
        ids = [str(i).strip() for i in entry_ids if str(i).strip()]
        if not ids:
            return
        async with self._write_lock:
            for eid in dict.fromkeys(ids):
                entry = await self.read(eid)
                if not entry:
                    continue
                entry.last_accessed = _now()
                entry.access_count = int(entry.access_count or 0) + 1
                abs_path = self.root / entry.path if entry.path else self._entry_abs_path(entry)
                if abs_path.is_file():
                    write_text_atomic(abs_path, self._serialize(entry))

    async def find_by_url(self, url: str) -> CollectionEntry | None:
        url = (url or "").strip()
        if not url:
            return None
        index = self._load_index()
        by_url = index.get("by_url") or {}
        if isinstance(by_url, dict):
            val = by_url.get(url)
            candidates = val if isinstance(val, list) else ([val] if val else [])
            for cid in candidates:
                entry = await self.read(str(cid))
                if entry is not None:
                    return entry
        return None

    async def find_by_hash(self, hash_value: str) -> CollectionEntry | None:
        if not hash_value:
            return None
        index = self._load_index()
        by_hash = index.get("by_hash") or {}
        if isinstance(by_hash, dict):
            val = by_hash.get(hash_value)
            candidates = val if isinstance(val, list) else ([val] if val else [])
            for cid in candidates:
                entry = await self.read(str(cid))
                if entry is not None:
                    return entry
        return None

    async def search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[CollectionEntry]:
        """Token-AND text search; ranked by hit quality then recency."""
        q = (query or "").strip().lower()
        tag_set = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
        hits: list[tuple[int, float, CollectionEntry]] = []
        for path in self._iter_md_files():
            rel = path.relative_to(self.root).as_posix()
            entry = self._parse_file(path, rel=rel)
            if not entry:
                continue
            if tag_set:
                entry_tags = {t.lower() for t in entry.tags}
                if not tag_set.intersection(entry_tags):
                    continue
            score = 0
            if q:
                blob = f"{entry.title}\n{entry.summary}\n{entry.note}\n{entry.content}\n{' '.join(entry.tags)}".lower()
                if q in blob:
                    score = 2
                else:
                    tokens = [t for t in q.split() if t]
                    if tokens and all(t in blob for t in tokens):
                        score = 1
                    else:
                        continue
            else:
                score = 1
            created_ord = entry.created_at.timestamp() if entry.created_at else 0.0
            hits.append((score, created_ord, entry))
        hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [e for _, _, e in hits[: max(1, limit)]]

    async def list_entries(self, *, limit: int = 20) -> list[CollectionEntry]:
        return await self.search(query="", limit=limit)

    async def delete(self, entry_id: str) -> bool:
        async with self._write_lock:
            entry = await self.read(entry_id)
            if not entry:
                return False
            path = self.root / entry.path if entry.path else None
            if path and path.is_file():
                path.unlink()
            # Binary payloads for source_type=file
            files_dir = self.root / "files" / entry_id
            if files_dir.is_dir():
                shutil.rmtree(files_dir, ignore_errors=True)
            index = self._load_index()
            _remove_from_index(index, entry_id)
            self._save_index(index)
            return True


def _split_body_sections(body: str) -> tuple[str, str, str]:
    """Split a serialized body back into (summary, note, content)."""
    bucket = "summary"
    buckets: dict[str, list[str]] = {"summary": [], "note": [], "content": []}
    for line in body.splitlines():
        if line.strip() == "## 摘要":
            bucket = "summary"
            continue
        if line.strip() == "## 用户注记":
            bucket = "note"
            continue
        if line.strip() == "## 原文":
            bucket = "content"
            continue
        buckets[bucket].append(line)
    summary = "\n".join(buckets["summary"]).strip()
    note = "\n".join(buckets["note"]).strip()
    content = "\n".join(buckets["content"]).strip()
    return summary, note, content


def build_entry(
    *,
    title: str,
    summary: str,
    source_type: CollectionSourceType | str = "snippet",
    source_url: str = "",
    note: str = "",
    content: str = "",
    tags: list[str] | None = None,
) -> CollectionEntry:
    """Assemble a CollectionEntry (digestion happens upstream in the agent)."""
    now = _now()
    stype = str(source_type or "snippet").strip()
    if stype not in COLLECTION_SOURCE_TYPES:
        stype = "snippet"
    title_clean = (title or "").strip() or (summary.strip().splitlines() or [""])[0][:80]
    return CollectionEntry(
        id=generate_collection_id(now),
        created_at=now,
        source_type=stype,  # type: ignore[arg-type]
        source_url=(source_url or "").strip(),
        title=title_clean,
        summary=(summary or "").strip(),
        note=(note or "").strip(),
        content=(content or "").strip(),
        tags=[str(t).strip() for t in (tags or []) if str(t).strip()],
        content_hash=content_hash(content or summary),
        last_accessed=now,
        access_count=0,
    )
