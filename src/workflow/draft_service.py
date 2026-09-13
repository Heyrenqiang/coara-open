"""Shared workflow draft business logic for HTTP API layers.

2026-08-16 内核化：草案文本一律是内核投影（nodes+edges）。校验走
core.semantics.validate_graph，规范化即 serde canonical round-trip
（parse → emit）。
"""

from __future__ import annotations

from typing import Any

from src.workflow.core.semantics import validate_graph
from src.workflow.core.serde import emit_graph, parse_graph
from src.workflow.draft_store import WorkflowDraft, WorkflowDraftStore


def parse_projection_or_none(text: str) -> Any | None:
    """解析内核投影；失败返回 None（调用方降级）。"""
    try:
        return parse_graph(text)
    except Exception:  # noqa: BLE001
        return None


def validate_projection(text: str) -> tuple[list[str], list[str]]:
    """校验内核投影文本 → (errors, warnings)。errors 非空即不可保存。"""
    try:
        graph = parse_graph(text)
    except ValueError as exc:
        return [str(exc)], []
    issues = validate_graph(graph)
    return (
        [i.message for i in issues if i.level == "error"],
        [i.message for i in issues if i.level != "error"],
    )


def normalize_projection(text: str) -> str:
    """canonical 规范化：parse → emit（round-trip 恒等的固定表达）。"""
    return emit_graph(parse_graph(text))


def serialize_draft(draft: WorkflowDraft) -> dict[str, Any]:
    """Serialize a WorkflowDraft for API responses."""
    wdl = str(getattr(draft, "wdl", "") or "").strip()
    graph = parse_projection_or_none(wdl) if wdl else None
    return {
        "draft_id": draft.draft_id,
        "name": graph.name if graph else "",
        "description": (graph.description or "") if graph else "",
        "wdl": wdl,
        "source_subagent": draft.source_subagent,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def list_drafts(store: WorkflowDraftStore) -> dict[str, Any]:
    drafts = store.list_drafts()
    payload = [serialize_draft(draft) for draft in drafts]
    return {"drafts": payload, "count": len(payload)}


def get_draft(store: WorkflowDraftStore, draft_id: str) -> dict[str, Any] | None:
    draft = store.get(draft_id)
    if draft is None:
        return None
    return serialize_draft(draft)


def save_draft(
    store: WorkflowDraftStore,
    draft_id: str,
    wdl_text: str,
    *,
    source_subagent: str | None = None,
    allow_create: bool = False,
) -> dict[str, Any]:
    """Validate, normalize and save a draft (kernel projection).

    Raises ValueError with a message suitable for HTTP 400 on validation failure.
    Returns the serialized draft on success.
    """
    existing = store.get(draft_id)
    if existing is None and not allow_create:
        raise KeyError(f"Workflow draft not found: {draft_id}")

    errors, warnings = validate_projection(wdl_text)
    if errors:
        raise ValueError("工作流校验失败（save）：" + "；".join(errors))

    normalized = normalize_projection(wdl_text)
    saved = store.save(
        normalized,
        draft_id=draft_id,
        source_subagent=source_subagent or (existing.source_subagent if existing else "root"),
    )
    return serialize_draft(saved)
