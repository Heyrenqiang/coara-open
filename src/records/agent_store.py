"""File-backed memory store (Markdown + YAML frontmatter + index.yaml)."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter

from src.core.json_store import write_text_atomic
from src.records.agent_index import (
    empty_index,
    load_index,
    remove_index_entry,
    save_index,
    upsert_index_entry,
)
from src.records.agent_types import (
    MEMORY_TYPES,
    TYPE_DIR_MAP,
    DedupeResult,
    MemoryEntry,
    MemorySource,
    MemoryType,
    Scope,
    Sensitivity,
    SourceType,
)

# 通用 helper 单源在 store_helpers；此处 import 即对外再导出（facade/tests 仍从本模块取）
from src.records.store_helpers import (
    content_hash,
    generate_record_id,
    normalize_for_hash,
    parse_dt,
)
from src.records.store_helpers import now_local as _now
from src.records.store_helpers import slugify as _slugify


def generate_memory_id(when: datetime | None = None) -> str:
    return generate_record_id("mem", when)


def _title_similarity(a: str, b: str) -> float:
    """Simple token Jaccard similarity for fuzzy title dedupe."""
    ta = set(normalize_for_hash(a).split())
    tb = set(normalize_for_hash(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class MemoryStore:
    """Markdown file store under ``records/agent/`` (agent-origin notes + digests)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.index_path = self.root / "index.yaml"
        self.archive_dir = self.root / "archive"
        # 串行化写路径：index.yaml 的 load→mutate→save 不再交错（last-writer-wins）
        self._write_lock = asyncio.Lock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "digests").mkdir(parents=True, exist_ok=True)
        for dirname in TYPE_DIR_MAP.values():
            (self.root / dirname).mkdir(parents=True, exist_ok=True)
        if not self.index_path.is_file():
            save_index(self.index_path, empty_index())

    def digest_path(self, day: str) -> Path:
        """Absolute path for ``digests/YYYY-MM-DD.md``."""
        key = (day or "").strip()
        return self.root / "digests" / f"{key}.md"

    def write_digest(self, day: str, content: str) -> Path:
        """Write daily digest Markdown; returns absolute path."""
        key = (day or "").strip()
        if not key:
            raise ValueError("digest day required (YYYY-MM-DD)")
        text = (content or "").strip()
        if not text:
            raise ValueError("digest content required")
        path = self.digest_path(key)
        write_text_atomic(path, text + "\n")
        return path

    def _load_index(self) -> dict[str, Any]:
        return load_index(self.index_path)

    def _save_index(self, index: dict[str, Any]) -> None:
        save_index(self.index_path, index)

    def _entry_abs_path(self, entry: MemoryEntry) -> Path:
        if entry.path:
            return self.root / entry.path
        type_dir = TYPE_DIR_MAP.get(entry.type, "events")
        created = entry.created_at or _now()
        if entry.type == "event":
            rel = Path(type_dir) / created.strftime("%Y") / created.strftime("%m") / created.strftime("%d")
        else:
            rel = Path(type_dir)
        slug = _slugify(entry.title, entry.id)
        rel = rel / f"{slug}.md"
        entry.path = rel.as_posix()
        return self.root / rel

    def _serialize(self, entry: MemoryEntry) -> str:
        meta: dict[str, Any] = {
            "id": entry.id,
            "type": entry.type,
            "scope": entry.scope,
            "sensitivity": entry.sensitivity,
            "created_at": entry.created_at.isoformat(),
            "source": entry.source.model_dump(),
            "source_type": entry.source_type,
            "tags": list(entry.tags or []),
            "content_hash": entry.content_hash,
            "ttl_days": entry.ttl_days,
            "last_accessed": entry.last_accessed.isoformat() if entry.last_accessed else None,
            "access_count": entry.access_count,
            "status": entry.status,
            "superseded_by": entry.superseded_by,
            "supersedes": entry.supersedes,
            "valid_until": entry.valid_until.isoformat() if entry.valid_until else None,
            "version": entry.version,
            "title": entry.title,
        }
        post = frontmatter.Post(entry.content or "", **meta)
        return frontmatter.dumps(post)

    def _parse_file(self, path: Path, *, rel: str = "") -> MemoryEntry | None:
        try:
            post = frontmatter.load(path)
        except Exception:
            return None
        meta = dict(post.metadata or {})
        mid = str(meta.get("id") or "").strip()
        if not mid:
            return None
        mtype = str(meta.get("type") or "event").strip()
        if mtype not in MEMORY_TYPES:
            mtype = "event"

        src_raw = meta.get("source") or {}
        if not isinstance(src_raw, dict):
            src_raw = {}

        def _float(val: Any) -> float | None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        source = MemorySource(
            session_id=str(src_raw.get("session_id") or ""),
            turn_index=int(src_raw.get("turn_index") or 0),
            actor=(src_raw.get("actor") if src_raw.get("actor") in ("user", "assistant", "system") else "assistant"),  # type: ignore[arg-type]
            workspace=str(src_raw.get("workspace") or ""),
            tape_start=_float(src_raw.get("tape_start")),
            tape_end=_float(src_raw.get("tape_end")),
        )
        created = parse_dt(meta.get("created_at")) or _now()
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        return MemoryEntry(
            id=mid,
            type=mtype,  # type: ignore[arg-type]
            scope=str(meta.get("scope") or "user"),  # type: ignore[arg-type]
            sensitivity=str(meta.get("sensitivity") or "internal"),  # type: ignore[arg-type]
            created_at=created,
            source=source,
            title=str(meta.get("title") or path.stem),
            content=str(post.content or ""),
            source_type=str(meta.get("source_type") or "explicit"),  # type: ignore[arg-type]
            tags=[str(t) for t in tags],
            content_hash=str(meta.get("content_hash") or ""),
            ttl_days=meta.get("ttl_days"),
            last_accessed=parse_dt(meta.get("last_accessed")),
            access_count=int(meta.get("access_count") or 0),
            status=str(meta.get("status") or "active"),  # type: ignore[arg-type]
            superseded_by=meta.get("superseded_by"),
            supersedes=meta.get("supersedes"),
            valid_until=parse_dt(meta.get("valid_until")),
            version=int(meta.get("version") or 1),
            path=rel or path.relative_to(self.root).as_posix(),
        )

    def _iter_md_files(self, *, include_archived: bool = False) -> list[Path]:
        files: list[Path] = []
        for dirname in TYPE_DIR_MAP.values():
            d = self.root / dirname
            if d.is_dir():
                files.extend(d.rglob("*.md"))
        if include_archived and self.archive_dir.is_dir():
            files.extend(self.archive_dir.rglob("*.md"))
        return files

    async def find_by_hash(self, hash_value: str) -> MemoryEntry | None:
        if not hash_value:
            return None
        index = self._load_index()
        by_hash = index.get("by_hash") or {}
        if isinstance(by_hash, dict):
            val = by_hash.get(hash_value)
            candidates = val if isinstance(val, list) else ([val] if val else [])
            for mid in candidates:
                entry = await self.read(str(mid))
                if entry is not None:
                    return entry
        # Fallback scan
        for path in self._iter_md_files(include_archived=False):
            entry = self._parse_file(path, rel=path.relative_to(self.root).as_posix())
            if entry and entry.content_hash == hash_value and entry.status == "active":
                return entry
        return None

    async def search_by_title(self, title: str, threshold: float = 0.8) -> MemoryEntry | None:
        best: MemoryEntry | None = None
        best_score = 0.0
        for path in self._iter_md_files(include_archived=False):
            entry = self._parse_file(path, rel=path.relative_to(self.root).as_posix())
            if not entry or entry.status != "active":
                continue
            score = _title_similarity(title, entry.title)
            if score > best_score:
                best_score = score
                best = entry
        if best and best_score >= threshold:
            return best
        return None

    async def dedupe_check(self, content: str, title: str) -> DedupeResult:
        h = content_hash(content)
        existing = await self.find_by_hash(h)
        if existing:
            return DedupeResult(action="skip", reason=f"内容已存在: {existing.id}", existing_id=existing.id)
        similar = await self.search_by_title(title, threshold=0.8)
        if similar:
            return DedupeResult(
                action="review",
                reason=f"发现相似记忆: {similar.id}",
                existing_id=similar.id,
            )
        return DedupeResult(action="allow")

    async def write(self, entry: MemoryEntry) -> str:
        """Write a memory entry to disk and update index. Returns id."""
        async with self._write_lock:
            return self._write_inner(entry)

    def _write_inner(self, entry: MemoryEntry) -> str:
        """write 的锁内实现（调用方须持 _write_lock）。"""
        if not entry.content_hash:
            entry.content_hash = content_hash(entry.content)
        if not entry.created_at:
            entry.created_at = _now()
        if entry.last_accessed is None:
            entry.last_accessed = entry.created_at
        abs_path = self._entry_abs_path(entry)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Avoid clobbering different ids that share slug
        if abs_path.exists():
            existing = self._parse_file(abs_path, rel=entry.path)
            if existing and existing.id != entry.id:
                stem = abs_path.stem
                abs_path = abs_path.with_name(f"{stem}_{entry.id[-6:]}.md")
                entry.path = abs_path.relative_to(self.root).as_posix()
        write_text_atomic(abs_path, self._serialize(entry))
        index = self._load_index()
        upsert_index_entry(index, entry)
        self._save_index(index)
        return entry.id

    async def read(self, memory_id: str) -> MemoryEntry | None:
        memory_id = (memory_id or "").strip()
        if not memory_id:
            return None
        index = self._load_index()
        # Prefer path from index
        for items in (index.get("by_type") or {}).values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("id") == memory_id:
                    rel = str(item.get("path") or "")
                    if rel:
                        path = self.root / rel
                        if path.is_file():
                            return self._parse_file(path, rel=rel)
        for path in self._iter_md_files(include_archived=True):
            entry = self._parse_file(path, rel=path.relative_to(self.root).as_posix())
            if entry and entry.id == memory_id:
                return entry
        return None

    async def update_content(self, memory_id: str, content: str) -> bool:
        """Rewrite an entry's body in place: same id/path, refreshed hash+index."""
        memory_id = (memory_id or "").strip()
        content = (content or "").strip()
        if not memory_id or not content:
            return False
        async with self._write_lock:
            entry = await self.read(memory_id)
            if entry is None:
                return False
            entry.content = content
            entry.content_hash = content_hash(content)
            abs_path = self.root / entry.path if entry.path else self._entry_abs_path(entry)
            write_text_atomic(abs_path, self._serialize(entry))
            index = self._load_index()
            upsert_index_entry(index, entry)
            self._save_index(index)
            return True

    async def touch(self, memory_id: str) -> None:
        await self.touch_many([memory_id])

    async def touch_many(self, memory_ids: list[str]) -> None:
        """批量 touch：一次 index load/save 合并写，避免搜索命中逐条盘写放大。"""
        ids = [str(i).strip() for i in memory_ids if str(i).strip()]
        if not ids:
            return
        async with self._write_lock:
            index = self._load_index()
            changed = False
            for mid in dict.fromkeys(ids):
                entry = await self.read(mid)
                if not entry:
                    continue
                entry.last_accessed = _now()
                entry.access_count = int(entry.access_count or 0) + 1
                abs_path = self.root / entry.path if entry.path else self._entry_abs_path(entry)
                if abs_path.is_file():
                    write_text_atomic(abs_path, self._serialize(entry))
                upsert_index_entry(index, entry)
                changed = True
            if changed:
                self._save_index(index)

    async def search(
        self,
        query: str = "",
        type_filter: list[MemoryType] | list[str] | None = None,
        tags: list[str] | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        q = (query or "").strip().lower()
        type_set = {str(t) for t in (type_filter or []) if t}
        tag_set = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}
        hits: list[tuple[int, MemoryEntry]] = []
        for path in self._iter_md_files(include_archived=include_archived):
            rel = path.relative_to(self.root).as_posix()
            entry = self._parse_file(path, rel=rel)
            if not entry:
                continue
            if entry.status == "deleted":
                continue
            if entry.status == "archived" and not include_archived:
                continue
            if entry.status == "superseded":
                continue
            if type_set and entry.type not in type_set:
                continue
            if tag_set:
                entry_tags = {t.lower() for t in entry.tags}
                if not tag_set.intersection(entry_tags):
                    continue
            score = 0
            if q:
                blob = f"{entry.title}\n{entry.content}\n{' '.join(entry.tags)}".lower()
                if q not in blob:
                    # token AND: all tokens must appear
                    tokens = [t for t in q.split() if t]
                    if tokens and all(t in blob for t in tokens):
                        score = 1
                    else:
                        continue
                else:
                    score = 2
            else:
                score = 1
            # Prefer newer
            created_ord = int(entry.created_at.timestamp()) if entry.created_at else 0
            hits.append((score * 10_000_000_000 + created_ord, entry))
        hits.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in hits[: max(1, limit)]]

    async def list_entries(
        self,
        *,
        type_filter: str | None = None,
        tag: str | None = None,
        recent_days: int | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[MemoryEntry]:
        types = [type_filter] if type_filter else None
        tags = [tag] if tag else None
        entries = await self.search(
            query="",
            type_filter=types,  # type: ignore[arg-type]
            tags=tags,
            include_archived=include_archived,
            limit=max(limit, 200),
        )
        if recent_days is not None and recent_days > 0:
            cutoff = _now().timestamp() - recent_days * 86400
            entries = [e for e in entries if e.created_at and e.created_at.timestamp() >= cutoff]
        return entries[:limit]

    async def update(self, memory_id: str, updates: dict[str, Any]) -> bool:
        async with self._write_lock:
            entry = await self.read(memory_id)
            if not entry:
                return False
            data = entry.model_dump()
            data.update(updates)
            updated = MemoryEntry.model_validate(data)
            if "content" in updates:
                updated.content_hash = content_hash(updated.content)
            self._write_inner(updated)
            return True

    async def delete(self, memory_id: str) -> bool:
        """Hard-delete the file and drop from index (user-requested forget)."""
        async with self._write_lock:
            entry = await self.read(memory_id)
            if not entry:
                return False
            path = self.root / entry.path if entry.path else None
            if path and path.is_file():
                path.unlink()
            index = self._load_index()
            remove_index_entry(index, memory_id)
            self._save_index(index)
            return True

    async def archive(self, memory_id: str) -> bool:
        async with self._write_lock:
            entry = await self.read(memory_id)
            if not entry or entry.status == "archived":
                return False
            src = self.root / entry.path
            if not src.is_file():
                return False
            dest_dir = self.archive_dir / _now().strftime("%Y") / _now().strftime("%m")
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if dest.exists():
                dest = dest_dir / f"{src.stem}_{entry.id[-6:]}{src.suffix}"
            shutil.move(str(src), str(dest))
            entry.status = "archived"
            entry.path = dest.relative_to(self.root).as_posix()
            write_text_atomic(dest, self._serialize(entry))
            index = self._load_index()
            upsert_index_entry(index, entry)
            self._save_index(index)
            return True

    async def unarchive(self, memory_id: str) -> bool:
        async with self._write_lock:
            entry = await self.read(memory_id)
            if not entry or entry.status != "archived":
                return False
            src = self.root / entry.path
            if not src.is_file():
                return False
            entry.status = "active"
            entry.last_accessed = _now()
            # Place back under type dir
            entry.path = ""
            dest = self._entry_abs_path(entry)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            entry.path = dest.relative_to(self.root).as_posix()
            write_text_atomic(dest, self._serialize(entry))
            index = self._load_index()
            upsert_index_entry(index, entry)
            self._save_index(index)
            return True


def compose_body(*, content: str, title: str = "", context: str = "", reason: str = "") -> tuple[str, str]:
    """Assemble ``(title_clean, body)`` exactly as ``build_entry`` stores them.

    落盘 hash 对 body 计算；去重必须用同一构造（单一真相），否则 hash 口径漂移。
    """
    title_clean = (title or "").strip() or content.strip().splitlines()[0][:80]
    body_parts = [f"## {title_clean}", "", content.strip()]
    if context.strip():
        body_parts.extend(["", "## 情境", "", context.strip()])
    if reason.strip():
        body_parts.extend(["", "## 理由", "", reason.strip()])
    body = "\n".join(body_parts).strip() + "\n"
    return title_clean, body


def build_entry(
    *,
    content: str,
    title: str = "",
    type_: MemoryType | str = "event",
    scope: Scope | str = "user",
    sensitivity: Sensitivity | str = "internal",
    source: MemorySource | None = None,
    source_type: SourceType | str = "explicit",
    tags: list[str] | None = None,
    context: str = "",
    reason: str = "",
) -> MemoryEntry:
    """Helper to assemble a MemoryEntry with Markdown body sections."""
    now = _now()
    mid = generate_memory_id(now)
    mtype = type_ if type_ in MEMORY_TYPES else "event"
    title_clean, body = compose_body(content=content, title=title, context=context, reason=reason)
    return MemoryEntry(
        id=mid,
        type=mtype,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        sensitivity=sensitivity,  # type: ignore[arg-type]
        created_at=now,
        source=source or MemorySource(),
        title=title_clean,
        content=body,
        source_type=source_type,  # type: ignore[arg-type]
        tags=list(tags or []),
        content_hash=content_hash(body),
        last_accessed=now,
        access_count=0,
        status="active",
    )
