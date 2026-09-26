"""Records facade over unified ``RecordsStore`` (origin=agent|user).

Tools ``record`` / ``local_search`` and Web/手机端上手动收藏用此层。CLI 无 ``/records`` / ``/collect``。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.records.agent_format import format_memory_results, format_memory_show
from src.records.agent_gate import contains_secret, memory_gate
from src.records.agent_store import build_entry as build_memory_entry
from src.records.agent_store import compose_body as compose_memory_body
from src.records.agent_types import MemorySource
from src.records.store import RecordsStore
from src.records.user_format import format_collection_results, format_collection_show
from src.records.user_store import build_entry as build_collection_entry
from src.records.user_store import content_hash as collection_content_hash

OriginFilter = Literal["agent", "user", "all"]

_MAX_COLLECTION_CONTENT = 4000


@dataclass(slots=True)
class FacadeResult:
    ok: bool
    message: str
    metadata: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return not self.ok


class RecordsFacade:
    """Write/search API over ``RecordsStore``."""

    def __init__(self, store: RecordsStore | None = None) -> None:
        self.store = store

    def _need_agent(self) -> FacadeResult | None:
        if self.store is None or self.store.agent is None:
            return FacadeResult(
                False,
                "记录未启用（配置 records.enabled: false；用配置助手打开后重启内核）",
            )
        return None

    def _need_user(self) -> FacadeResult | None:
        if self.store is None or self.store.user is None:
            return FacadeResult(False, "用户记录未初始化")
        return None

    async def add_agent(
        self,
        *,
        content: str,
        title: str = "",
        type_: str = "event",
        context: str = "",
        reason: str = "",
        scope: str = "user",
        tags: list[str] | None = None,
        session_id: str = "unknown",
        workspace: str = "",
        tape_start: float | None = None,
        tape_end: float | None = None,
    ) -> FacadeResult:
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        agent = self.store.agent
        text = (content or "").strip()
        if not text:
            return FacadeResult(False, "add 需要 content")
        source = MemorySource(
            session_id=session_id or "unknown",
            turn_index=0,
            actor="assistant",
            workspace=workspace,
            tape_start=tape_start,
            tape_end=tape_end,
        )
        gate = memory_gate(text, source=source, source_type="explicit")
        if not gate.allowed:
            return FacadeResult(False, f"记录被拒绝：{gate.reason}")

        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        # 去重 hash 与落盘同一构造（body），否则两者恒不相等、内容级去重失效
        dedupe_title, dedupe_body = compose_memory_body(content=text, title=title, context=context, reason=reason)
        dedupe = await agent.dedupe_check(dedupe_body, dedupe_title)
        if dedupe.action == "skip" and dedupe.existing_id:
            await agent.touch(dedupe.existing_id)
            return FacadeResult(
                True,
                f"内容已存在，已更新访问记录（ID: {dedupe.existing_id}）",
                {"id": dedupe.existing_id, "deduped": True, "origin": "agent"},
            )

        entry = build_memory_entry(
            content=text,
            title=title,
            type_=type_,
            scope=scope,
            sensitivity=gate.sensitivity,
            source=source,
            source_type=gate.source_type,
            tags=tags,
            context=context,
            reason=reason,
        )
        memory_id = await agent.write(entry)
        note = ""
        if dedupe.action == "review" and dedupe.existing_id:
            note = f"（发现相似记录 {dedupe.existing_id}，已新增）"
        return FacadeResult(
            True,
            f"已记录（agent · ID: {memory_id}）{note}",
            {"id": memory_id, "origin": "agent"},
        )

    async def read_source(self, memory_id: str) -> FacadeResult:
        """按记录上的溯源坐标回放录像带原文（record 工具 action=source）。"""
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        entry = await self.store.agent.read(memory_id)
        if entry is None:
            return FacadeResult(False, f"记录不存在：{memory_id}")
        src = entry.source
        if not src.workspace or (src.tape_start is None and src.tape_end is None):
            return FacadeResult(False, "该记录无录像带溯源信息（旧记录没有加盖坐标）")
        from src.session_log.replay import replay_dialogue

        text = await asyncio.to_thread(
            replay_dialogue,
            src.workspace,
            ts_start=src.tape_start,
            ts_end=src.tape_end,
        )
        return FacadeResult(True, text)

    async def add_user(
        self,
        *,
        title: str,
        summary: str = "",
        url: str = "",
        note: str = "",
        content: str = "",
        tags: list[str] | None = None,
        source_type: str | None = None,
        auto_summary: bool = False,
    ) -> FacadeResult:
        err = self._need_user()
        if err:
            return err
        assert self.store is not None and self.store.user is not None
        user = self.store.user
        title = (title or "").strip()
        summary = (summary or "").strip()
        url = (url or "").strip()
        note = (note or "").strip()
        content = (content or "").strip()
        if len(content) > _MAX_COLLECTION_CONTENT:
            content = content[:_MAX_COLLECTION_CONTENT]
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]

        if not title:
            return FacadeResult(False, "收藏需要 title")
        if not summary:
            if auto_summary:
                for line in (content or title).splitlines():
                    s = line.strip()
                    if s:
                        summary = s[:200]
                        break
                if not summary:
                    summary = title[:200] or "收藏"
            else:
                return FacadeResult(False, "收藏需要 summary（入库即消化：请先消化成要点）")

        blob = "\n".join(x for x in (title, summary, note, content, url) if x)
        if contains_secret(blob):
            return FacadeResult(False, "包含敏感信息，拒绝收藏")

        if url:
            existing = await user.find_by_url(url)
            if existing:
                await user.touch(existing.id)
                return FacadeResult(
                    True,
                    f"这个链接已收藏过：{existing.title}（ID: {existing.id}）",
                    {"id": existing.id, "deduped": True, "origin": "user"},
                )
        if content:
            existing = await user.find_by_hash(collection_content_hash(content))
            if existing:
                await user.touch(existing.id)
                return FacadeResult(
                    True,
                    f"相同内容已收藏过：{existing.title}（ID: {existing.id}）",
                    {"id": existing.id, "deduped": True, "origin": "user"},
                )

        if source_type and source_type in ("link", "file", "snippet", "note"):
            stype = source_type
        elif url and not url.startswith("matrix:"):
            stype = "link"
        elif content:
            stype = "snippet"
        else:
            stype = "note"

        entry = build_collection_entry(
            title=title,
            summary=summary,
            source_type=stype,
            source_url=url,
            note=note,
            content=content,
            tags=tags,
        )
        entry_id = await user.write(entry)
        return FacadeResult(
            True,
            f"已收藏：{title}（ID: {entry_id}）",
            {"id": entry_id, "origin": "user"},
        )

    async def add_user_file(
        self,
        *,
        title: str,
        summary: str,
        filename: str,
        file_bytes: bytes,
        mime: str = "",
        source_url: str = "",
        note: str = "",
        tags: list[str] | None = None,
    ) -> FacadeResult:
        """Write binary under ``records/user/files/<id>/`` plus an md index entry."""
        err = self._need_user()
        if err:
            return err
        assert self.store is not None and self.store.user is not None
        user = self.store.user
        title = (title or "").strip() or (filename or "file").strip()
        summary = (summary or "").strip() or f"收藏文件：{filename}"
        source_url = (source_url or "").strip()
        note = (note or "").strip()
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]

        if source_url:
            existing = await user.find_by_url(source_url)
            if existing:
                await user.touch(existing.id)
                return FacadeResult(
                    True,
                    f"这个文件已收藏过：{existing.title}（ID: {existing.id}）",
                    {"id": existing.id, "deduped": True, "origin": "user"},
                )

        if contains_secret("\n".join(x for x in (title, summary, note, filename) if x)):
            return FacadeResult(False, "包含敏感信息，拒绝收藏")

        entry = build_collection_entry(
            title=title,
            summary=summary,
            source_type="file",
            source_url=source_url,
            note=note,
            content="",
            tags=tags,
        )
        safe = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", (filename or "file.bin").strip()) or "file.bin"
        safe = safe[:120]
        rel_dir = Path("files") / entry.id
        abs_dir = user.root / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        abs_file = abs_dir / safe
        abs_file.write_bytes(file_bytes)
        rel_file = (rel_dir / safe).as_posix()
        meta_lines = [
            f"文件：{filename or safe}",
            f"路径：`{rel_file}`",
            f"大小：{len(file_bytes)} 字节",
        ]
        if mime:
            meta_lines.append(f"MIME：{mime}")
        entry.content = "\n".join(meta_lines)
        entry.content_hash = collection_content_hash(entry.content)
        entry_id = await user.write(entry)
        return FacadeResult(
            True,
            f"已收藏文件：{title}（ID: {entry_id}）",
            {"id": entry_id, "origin": "user", "path": rel_file},
        )

    async def remove(self, entry_id: str, *, origin: OriginFilter = "all") -> FacadeResult:
        eid = (entry_id or "").strip()
        if not eid:
            return FacadeResult(False, "需要 id")
        agent = self.store.agent if self.store else None
        user = self.store.user if self.store else None
        if origin in ("agent", "all") and agent is not None and await agent.delete(eid):
            return FacadeResult(True, f"已删除记录：{eid}", {"id": eid, "origin": "agent"})
        if origin in ("user", "all") and user is not None and await user.delete(eid):
            return FacadeResult(True, f"已删除收藏：{eid}", {"id": eid, "origin": "user"})
        return FacadeResult(False, f"未找到记录：{eid}")

    async def update(self, entry_id: str, content: str) -> FacadeResult:
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        eid = (entry_id or "").strip()
        content = (content or "").strip()
        if not eid:
            return FacadeResult(False, "需要 id")
        if not content:
            return FacadeResult(False, "需要 content")
        if contains_secret(content):
            return FacadeResult(False, "包含敏感信息，拒绝写入")
        if not await self.store.agent.update_content(eid, content):
            return FacadeResult(False, f"未找到记录：{eid}")
        return FacadeResult(True, f"已更新记录：{eid}", {"id": eid, "origin": "agent"})

    async def archive(self, entry_id: str) -> FacadeResult:
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        eid = (entry_id or "").strip()
        if not eid:
            return FacadeResult(False, "需要 id")
        if not await self.store.agent.archive(eid):
            return FacadeResult(False, f"无法归档：{eid}")
        return FacadeResult(True, f"已归档：{eid}", {"id": eid, "origin": "agent"})

    async def unarchive(self, entry_id: str) -> FacadeResult:
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        eid = (entry_id or "").strip()
        if not eid:
            return FacadeResult(False, "需要 id")
        if not await self.store.agent.unarchive(eid):
            return FacadeResult(False, f"无法恢复：{eid}")
        return FacadeResult(True, f"已恢复为活跃：{eid}", {"id": eid, "origin": "agent"})

    async def write_digest(self, day: str, content: str) -> FacadeResult:
        err = self._need_agent()
        if err:
            return err
        assert self.store is not None and self.store.agent is not None
        try:
            path = self.store.agent.write_digest(day, content)
        except ValueError as exc:
            return FacadeResult(False, str(exc))
        except Exception as exc:
            return FacadeResult(False, f"写入日报失败：{exc}")
        # 镜像进记录流：RecordsView 可见、local_search 可检索；digests 文件仍是存档真身。
        # 走 add_agent 自带门禁与内容级去重（同日重跑内容不变时不会产生重复条目）。
        mirror = await self.add_agent(
            content=content,
            title=f"日报 · {day}",
            type_="event",
            tags=["daily", day],
            session_id="daily",
        )
        note = "" if not mirror.is_error else f"（记录镜像失败：{mirror.message}）"
        return FacadeResult(True, f"已写入日报：{path}{note}", {"path": str(path), "date": day})

    async def search(
        self,
        *,
        query: str,
        origin: OriginFilter = "all",
        include_archived: bool = False,
        limit: int = 5,
        type_filter: list[str] | None = None,
    ) -> FacadeResult:
        q = (query or "").strip()
        if not q:
            return FacadeResult(False, "search 需要 query")
        limit = max(1, int(limit or 5))
        sections: list[str] = []
        ids: list[str] = []
        count = 0

        if origin in ("agent", "all") and self.store and self.store.agent is not None:
            results = await self.store.agent.search(
                query=q,
                type_filter=type_filter,
                include_archived=include_archived,
                limit=limit,
            )
            if results:
                await self.store.agent.touch_many([e.id for e in results])
                sections.append("【记录 · agent】\n" + format_memory_results(results, query=q))
                ids.extend(e.id for e in results)
                count += len(results)

        if origin in ("user", "all") and self.store and self.store.user is not None:
            user_results = await self.store.user.search(query=q, limit=limit)
            if user_results:
                await self.store.user.touch_many([e.id for e in user_results])
                sections.append("【收藏 · user】\n" + format_collection_results(user_results, query=q))
                ids.extend(e.id for e in user_results)
                count += len(user_results)

        if not sections:
            return FacadeResult(True, "没有找到相关记录。", {"count": 0, "ids": []})
        return FacadeResult(True, "\n\n".join(sections), {"count": count, "ids": ids})

    async def list_entries(
        self,
        *,
        origin: OriginFilter = "all",
        include_archived: bool = False,
        limit: int = 20,
        type_filter: str | None = None,
    ) -> FacadeResult:
        limit = max(1, int(limit or 20))
        lines: list[str] = []
        count = 0

        if origin in ("agent", "all") and self.store and self.store.agent is not None:
            entries = await self.store.agent.list_entries(
                type_filter=type_filter,
                include_archived=include_archived,
                limit=limit,
            )
            if entries:
                lines.append(f"记录（agent）· {len(entries)} 条：")
                for e in entries:
                    created = e.created_at.strftime("%Y-%m-%d") if e.created_at else ""
                    lines.append(f"- `{e.id}` [{e.type}/{e.status}] {e.title} · {created}")
                count += len(entries)

        if origin in ("user", "all") and self.store and self.store.user is not None:
            user_entries = await self.store.user.list_entries(limit=limit)
            if user_entries:
                if lines:
                    lines.append("")
                lines.append(f"收藏（user）· {len(user_entries)} 条：")
                for user_e in user_entries:
                    created = user_e.created_at.strftime("%Y-%m-%d") if user_e.created_at else ""
                    lines.append(f"- `{user_e.id}` [{user_e.source_type}] {user_e.title} · {created}")
                count += len(user_entries)

        if not lines:
            return FacadeResult(True, "暂无记录。", {"count": 0})
        return FacadeResult(True, "\n".join(lines), {"count": count})

    async def show(self, entry_id: str, *, origin: OriginFilter = "all") -> FacadeResult:
        eid = (entry_id or "").strip()
        if not eid:
            return FacadeResult(False, "show 需要 id")

        if origin in ("agent", "all") and self.store and self.store.agent is not None:
            entry = await self.store.agent.read(eid)
            if entry is not None:
                await self.store.agent.touch(eid)
                return FacadeResult(
                    True,
                    "【记录 · agent】\n" + format_memory_show(entry),
                    {"id": eid, "origin": "agent"},
                )

        if origin in ("user", "all") and self.store and self.store.user is not None:
            user_entry = await self.store.user.read(eid)
            if user_entry is not None:
                await self.store.user.touch(eid)
                return FacadeResult(
                    True,
                    "【收藏 · user】\n" + format_collection_show(user_entry),
                    {"id": eid, "origin": "user"},
                )

        return FacadeResult(False, f"未找到记录：{eid}")


def facade_from_root(root: Any) -> RecordsFacade:
    store = getattr(root, "records_store", None)
    if isinstance(store, RecordsStore):
        return RecordsFacade(store)
    return RecordsFacade(None)
