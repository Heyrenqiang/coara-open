"""Filesystem-backed workflow drafts — kernel projection text only.

草案是工作流的唯一活模型：会话编排（orchestrator）与 WebUI 编辑器读写
同一份存储（系统目录，见 src/workflow/paths.py）。``flow_name`` 记录草案
绑定的会话内 flow 名，会话重启后仍可找回绑定。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.core.json_store import write_json_atomic
from src.workflow.paths import workflow_drafts_dir


@dataclass(slots=True)
class WorkflowDraft:
    draft_id: str
    wdl: str
    source_subagent: str
    created_at: str
    updated_at: str
    flow_name: str = ""


class WorkflowDraftStore:
    """Filesystem-backed workflow drafts under ``<config_home>/users/default/workflows/drafts/``."""

    def __init__(self, coara_home: Path | str | None = None):
        self._dir = workflow_drafts_dir(coara_home)
        self._coara_home = coara_home

    @staticmethod
    def generate_id() -> str:
        return f"draft-{uuid.uuid4().hex[:8]}"

    def _path(self, draft_id: str) -> Path:
        return self._dir / f"{draft_id}.json"

    def save(
        self,
        wdl: str,
        *,
        draft_id: str | None = None,
        source_subagent: str = "root",
        flow_name: str | None = None,
    ) -> WorkflowDraft:
        now = datetime.now(UTC).isoformat()
        existing = self.get(draft_id) if draft_id else None
        if existing is None:
            draft_id = draft_id or self.generate_id()
            created_at = now
            resolved_flow_name = (flow_name or "").strip()
        else:
            created_at = existing.created_at
            # 覆盖保存：未显式给 flow_name 时保留原绑定
            resolved_flow_name = (flow_name or "").strip() or existing.flow_name

        from src.workflow.draft_service import normalize_projection

        canonical_wdl = normalize_projection(wdl)

        record = {
            "draft_id": draft_id,
            "wdl": canonical_wdl,
            "source_subagent": source_subagent,
            "created_at": created_at,
            "updated_at": now,
            "flow_name": resolved_flow_name,
        }
        write_json_atomic(self._path(draft_id), record)
        return WorkflowDraft(**record)

    def get(self, draft_id: str) -> WorkflowDraft | None:
        path = self._path(draft_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        wdl = str(data.get("wdl") or "").strip()
        if not wdl:
            path.unlink(missing_ok=True)
            return None
        return WorkflowDraft(
            draft_id=str(data.get("draft_id") or draft_id),
            wdl=wdl,
            source_subagent=str(data.get("source_subagent") or "root"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            flow_name=str(data.get("flow_name") or ""),
        )

    def list_drafts(self) -> list[WorkflowDraft]:
        drafts: list[WorkflowDraft] = []
        for path in sorted(self._dir.glob("draft-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                drafts.append(WorkflowDraft(**data))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return drafts

    def find_by_name(self, name: str) -> WorkflowDraft | None:
        """按绑定 flow 名或投影 name 找最新草案。"""
        target = str(name or "").strip()
        if not target:
            return None
        from src.workflow.draft_service import parse_projection_or_none

        fallback: WorkflowDraft | None = None
        for draft in self.list_drafts():
            if draft.flow_name == target:
                return draft
            if fallback is None:
                doc = parse_projection_or_none(draft.wdl)
                if doc is not None and str(getattr(doc, "name", "")) == target:
                    fallback = draft
        return fallback

    def path_for(self, draft_id: str) -> Path:
        return self._path(draft_id)

    def load_wdl(self, draft_id: str) -> str | None:
        draft = self.get(draft_id)
        if draft is None:
            return None
        text = str(draft.wdl or "").strip()
        return text or None

    def delete(self, draft_id: str) -> bool:
        path = self._path(draft_id)
        if not path.exists():
            return False
        path.unlink()
        from src.workflow.draft_sessions import unbind

        # 草案即 section：删除即解绑其构建会话（带内历史保留可审计）
        unbind(draft_id, self._coara_home)
        return True
