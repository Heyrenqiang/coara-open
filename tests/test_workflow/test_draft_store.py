"""Workflow draft store, asset layout, and path helpers."""

from __future__ import annotations

import json

from src.workflow.draft_store import WorkflowDraftStore
from src.workflow.paths import workflow_assets_layout, workflow_drafts_dir
from tests.workflow_wdl_fixtures import minimal_two_step_wdl


def test_draft_store_save_list_and_asset_paths(tmp_path) -> None:
    layout = workflow_assets_layout()
    assert layout["root"].endswith("workflows")
    assert layout["drafts"].endswith("drafts")

    store = WorkflowDraftStore()
    draft = store.save(minimal_two_step_wdl(name="demo"), source_subagent="root")
    assert draft.draft_id.startswith("draft-")

    path = workflow_drafts_dir() / f"{draft.draft_id}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["draft_id"] == draft.draft_id

    listed = store.list_drafts()
    assert len(listed) == 1
    updated = store.save(minimal_two_step_wdl(name="demo2"), draft_id=draft.draft_id)
    assert "demo2" in store.get(draft.draft_id).wdl
    assert updated.draft_id == draft.draft_id


def test_serialize_draft_includes_wdl_name(tmp_path) -> None:
    from src.workflow.draft_service import serialize_draft

    store = WorkflowDraftStore()
    draft = store.save(minimal_two_step_wdl(name="订单处理"), source_subagent="root")
    payload = serialize_draft(draft)
    assert payload["name"] == "订单处理"
    assert payload["draft_id"] == draft.draft_id
