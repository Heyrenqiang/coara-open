"""Collection subtree still works under RecordsStore."""

from __future__ import annotations

import pytest

from src.records.facade import RecordsFacade
from src.records.store import RecordsStore
from src.records.user_store import CollectionStore, build_entry


@pytest.mark.asyncio
async def test_collection_store_still_works(tmp_path):
    store = CollectionStore(tmp_path / "knowledge")
    entry = build_entry(
        title="T",
        summary="s",
        source_type="note",
        tags=["x"],
    )
    eid = await store.write(entry)
    loaded = await store.read(eid)
    assert loaded is not None
    assert loaded.title == "T"


@pytest.mark.asyncio
async def test_facade_add_user_via_records_store(tmp_path):
    facade = RecordsFacade(RecordsStore(tmp_path / "records", agent_enabled=False))
    result = await facade.add_user(title="链接", summary="s", url="https://example.com/1")
    assert not result.is_error
    listed = await facade.list_entries(origin="user")
    assert "链接" in listed.message
