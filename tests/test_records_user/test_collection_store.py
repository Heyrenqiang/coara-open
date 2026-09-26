"""Collection store tests: write/read/search/dedupe/delete round-trips."""

from __future__ import annotations

import pytest

from src.records.user_store import CollectionStore, build_entry


@pytest.fixture
def store(tmp_path):
    return CollectionStore(tmp_path / "knowledge")


@pytest.mark.asyncio
async def test_write_and_read_round_trip(store):
    entry = build_entry(
        title="Agent Memory 综述",
        summary="- 记忆系统全景\n- 各方案对比",
        source_type="link",
        source_url="https://example.com/agent-memory",
        note="先存后看",
        content="原文正文……",
        tags=["agent", "memory"],
    )
    entry_id = await store.write(entry)

    loaded = await store.read(entry_id)
    assert loaded is not None
    assert loaded.id == entry_id
    assert loaded.title == "Agent Memory 综述"
    assert loaded.source_type == "link"
    assert loaded.source_url == "https://example.com/agent-memory"
    assert loaded.summary.startswith("- 记忆系统全景")
    assert loaded.note == "先存后看"
    assert loaded.content == "原文正文……"
    assert loaded.tags == ["agent", "memory"]


@pytest.mark.asyncio
async def test_search_token_and(store):
    await store.write(build_entry(title="向量数据库选型", summary="对比 Chroma 和 Qdrant", tags=["rag"]))
    await store.write(build_entry(title="发布流程", summary="先在 Web 点发布再验证", tags=["deploy"]))

    hits = await store.search(query="Chroma Qdrant")
    assert [h.title for h in hits] == ["向量数据库选型"]

    hits = await store.search(query="发布")
    assert [h.title for h in hits] == ["发布流程"]

    # Tags filter
    await store.write(build_entry(title="RAG 检索模式", summary="HyDE 与 RRF", tags=["rag"]))
    tagged = await store.search(query="", tags=["rag"])
    assert {t.title for t in tagged} == {"向量数据库选型", "RAG 检索模式"}


@pytest.mark.asyncio
async def test_dedupe_by_url_and_hash(store):
    entry = build_entry(
        title="A",
        summary="s",
        source_url="https://example.com/a",
        content="same body",
    )
    await store.write(entry)
    assert await store.find_by_url("https://example.com/a") is not None
    assert await store.find_by_url("https://example.com/b") is None
    assert await store.find_by_hash(entry.content_hash) is not None


@pytest.mark.asyncio
async def test_touch_and_delete(store):
    entry = build_entry(title="临时", summary="s")
    entry_id = await store.write(entry)
    await store.touch(entry_id)
    loaded = await store.read(entry_id)
    assert loaded is not None
    assert loaded.access_count == 1

    assert await store.delete(entry_id)
    assert await store.read(entry_id) is None
    assert await store.delete(entry_id) is False


@pytest.mark.asyncio
async def test_touch_many(store):
    a = build_entry(title="批量甲", summary="s")
    b = build_entry(title="批量乙", summary="s")
    await store.write(a)
    await store.write(b)
    await store.touch_many([a.id, b.id])
    for eid in (a.id, b.id):
        loaded = await store.read(eid)
        assert loaded is not None and loaded.access_count == 1


@pytest.mark.asyncio
async def test_by_url_collects_multiple_entries(store):
    """同 URL 第二条不再覆盖第一条：删除其一后另一条仍可按 URL 查到。"""
    a = build_entry(title="收藏甲", summary="s1", source_url="https://example.com/x")
    b = build_entry(title="收藏乙", summary="s2", source_url="https://example.com/x")
    await store.write(a)
    await store.write(b)

    found = await store.find_by_url("https://example.com/x")
    assert found is not None and found.id == b.id  # 最新优先
    assert await store.delete(b.id)
    again = await store.find_by_url("https://example.com/x")
    assert again is not None and again.id == a.id


@pytest.mark.asyncio
async def test_slug_collision_keeps_both(store):
    a = build_entry(title="同名", summary="第一条")
    b = build_entry(title="同名", summary="第二条")
    await store.write(a)
    await store.write(b)
    assert (await store.read(a.id)).summary == "第一条"  # type: ignore[union-attr]
    assert (await store.read(b.id)).summary == "第二条"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_list_entries_recent_first(store):
    await store.write(build_entry(title="早", summary="s"))
    await store.write(build_entry(title="晚", summary="s"))
    entries = await store.list_entries(limit=10)
    assert len(entries) == 2
    assert entries[0].created_at >= entries[1].created_at
