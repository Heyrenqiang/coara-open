"""Tests for MemoryStore file persistence."""

from __future__ import annotations

import asyncio

import pytest

from src.records.agent_store import MemoryStore, build_entry, content_hash
from src.records.agent_types import MemorySource


@pytest.mark.asyncio
async def test_write_read_and_hash_dedupe(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    source = MemorySource(session_id="sess1", turn_index=1, actor="user")
    entry = build_entry(
        content="项目选择 asyncio",
        title="技术栈决策",
        type_="decision",
        source=source,
        tags=["python", "asyncio"],
        reason="与现有风格一致",
    )
    mid = await store.write(entry)
    assert mid.startswith("mem_")

    loaded = await store.read(mid)
    assert loaded is not None
    assert loaded.title == "技术栈决策"
    assert "asyncio" in loaded.content
    assert loaded.content_hash == content_hash(entry.content)

    # exact content hash dedupe
    dup = await store.dedupe_check(entry.content, entry.title)
    assert dup.action == "skip"
    assert dup.existing_id == mid

    await store.touch(mid)
    again = await store.read(mid)
    assert again is not None
    assert again.access_count >= 1


@pytest.mark.asyncio
async def test_search_list_archive_forget(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    source = MemorySource(session_id="s", actor="assistant")
    e1 = build_entry(
        content="修复 workflow 编辑器拖拽",
        title="workflow bug",
        type_="event",
        source=source,
        tags=["bug"],
    )
    e2 = build_entry(
        content="发布流程：先测后发",
        title="deploy",
        type_="procedure",
        source=source,
        tags=["deploy"],
    )
    await store.write(e1)
    await store.write(e2)

    hits = await store.search(query="workflow", limit=5)
    assert len(hits) == 1
    assert hits[0].id == e1.id

    listed = await store.list_entries(limit=10)
    assert len(listed) == 2

    assert await store.archive(e1.id) is True
    archived = await store.read(e1.id)
    assert archived is not None
    assert archived.status == "archived"
    assert not await store.search(query="workflow", include_archived=False)
    assert await store.search(query="workflow", include_archived=True)

    assert await store.unarchive(e1.id) is True
    active = await store.read(e1.id)
    assert active is not None
    assert active.status == "active"

    assert await store.delete(e2.id) is True
    assert await store.read(e2.id) is None


@pytest.mark.asyncio
async def test_touch_many_batches_index_writes(tmp_path, monkeypatch):
    """批量 touch 合并索引写：N 条命中只 load/save 一次 index.yaml。"""
    store = MemoryStore(tmp_path / "memory")
    e1 = build_entry(content="批量化甲", title="甲")
    e2 = build_entry(content="批量化乙", title="乙")
    await store.write(e1)
    await store.write(e2)

    saves = 0
    orig_save = store._save_index

    def counting_save(index):
        nonlocal saves
        saves += 1
        orig_save(index)

    monkeypatch.setattr(store, "_save_index", counting_save)
    await store.touch_many([e1.id, e2.id])
    assert saves == 1
    for mid in (e1.id, e2.id):
        loaded = await store.read(mid)
        assert loaded is not None and loaded.access_count == 1


@pytest.mark.asyncio
async def test_concurrent_touch_serialized_by_write_lock(tmp_path, monkeypatch):
    """写路径持实例锁：touch 的 read→write 交错不丢 access_count。"""
    store = MemoryStore(tmp_path / "memory")
    entry = build_entry(content="并发目标", title="并发")
    await store.write(entry)

    orig_read = store.read

    async def slow_read(memory_id):
        await asyncio.sleep(0)  # 制造交错窗口
        return await orig_read(memory_id)

    monkeypatch.setattr(store, "read", slow_read)
    await asyncio.gather(*(store.touch(entry.id) for _ in range(5)))
    loaded = await store.read(entry.id)
    assert loaded is not None and loaded.access_count == 5


@pytest.mark.asyncio
async def test_by_hash_collects_multiple_entries(tmp_path):
    """同 hash 第二条不再覆盖第一条：删除其一后另一条仍可按 hash 查到。"""
    store = MemoryStore(tmp_path / "memory")
    e1 = build_entry(content="同一内容", title="相同标题")
    e2 = build_entry(content="同一内容", title="相同标题")
    assert e1.content_hash == e2.content_hash
    await store.write(e1)
    await store.write(e2)

    found = await store.find_by_hash(e1.content_hash)
    assert found is not None and found.id == e2.id  # 最新优先
    assert await store.delete(e2.id)
    again = await store.find_by_hash(e1.content_hash)
    assert again is not None and again.id == e1.id
