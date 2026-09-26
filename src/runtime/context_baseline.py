"""Workspace-level context usage baseline (provider-reported only)"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths
from src.core.json_store import write_json_atomic
from src.core.logger import logger

_BASELINE_FILENAME = "context_baseline.json"


@dataclass(frozen=True, slots=True)
class ContextBaseline:
    prompt_tokens: int
    payload_hash: str | None
    model: str
    workspace_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": int(self.prompt_tokens),
            "payload_hash": self.payload_hash,
            "model": self.model,
            "workspace_id": self.workspace_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, workspace_id: str, model: str) -> ContextBaseline | None:
        if not isinstance(data, dict):
            return None
        try:
            tokens = int(data.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= 0:
            return None
        payload_hash = data.get("payload_hash")
        if payload_hash is not None:
            payload_hash = str(payload_hash).strip() or None
        return cls(
            prompt_tokens=tokens,
            payload_hash=payload_hash,
            model=str(data.get("model") or model or "").strip() or model,
            workspace_id=str(data.get("workspace_id") or workspace_id or "").strip() or workspace_id,
        )


def _baseline_path(workspace_dir: str | Path, *, configured_home: str | Path | None = None) -> Path:
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home)
    return paths.workspace_home / _BASELINE_FILENAME


def _model_key(model: str) -> str:
    return str(model or "").strip() or "_default"


def load_context_baseline(
    workspace_dir: str | Path,
    *,
    model: str,
    configured_home: str | Path | None = None,
) -> ContextBaseline | None:
    """Load baseline for ``model`` under this workspace; missing/corrupt → None."""
    path = _baseline_path(workspace_dir, configured_home=configured_home)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    by_model = raw.get("by_model")
    if not isinstance(by_model, dict):
        return None
    entry = by_model.get(_model_key(model))
    if not isinstance(entry, dict):
        return None
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home)
    return ContextBaseline.from_dict(entry, workspace_id=paths.workspace_id, model=model)


def save_context_baseline(
    workspace_dir: str | Path,
    *,
    model: str,
    prompt_tokens: int,
    payload_hash: str | None,
    configured_home: str | Path | None = None,
) -> ContextBaseline | None:
    """Persist baseline, keeping the shortest prompt for the same payload_hash"""
    tokens = int(prompt_tokens or 0)
    if tokens <= 0:
        return None
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home)
    path = paths.workspace_home / _BASELINE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_raw: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_raw = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_raw = {}

    by_model = existing_raw.get("by_model")
    if not isinstance(by_model, dict):
        by_model = {}

    key = _model_key(model)
    prev = ContextBaseline.from_dict(by_model.get(key) or {}, workspace_id=paths.workspace_id, model=model)
    hash_s = str(payload_hash or "").strip() or None
    if prev is not None and prev.payload_hash and hash_s and prev.payload_hash == hash_s:
        tokens = min(tokens, prev.prompt_tokens)

    baseline = ContextBaseline(
        prompt_tokens=tokens,
        payload_hash=hash_s,
        model=str(model or "").strip() or "_default",
        workspace_id=paths.workspace_id,
    )
    by_model[key] = baseline.to_dict()
    write_json_atomic(path, {"version": 1, "by_model": by_model})
    return baseline


def maybe_record_baseline_from_snapshot(
    coara: Any,
    *,
    configured_home: str | Path | None = None,
) -> None:
    """After a real LLM turn, refresh workspace baseline (best-effort)."""
    try:
        snap = getattr(coara, "_llm_usage_snapshot", None)
        if snap is None or getattr(snap, "estimated", False):
            return
        if not getattr(snap, "has_reported_input", False):
            return
        # 只在冷启动附近记基线：历史很长时 prompt 已含对话，不能当前缀。
        history_len = getattr(snap, "history_len", None)
        try:
            if history_len is not None and int(history_len) > 6:
                return
        except (TypeError, ValueError):
            pass
        from src.llm.usage import total_prompt_tokens

        prompt = total_prompt_tokens(getattr(snap, "usage", None) or {})
        if prompt <= 0:
            return
        # 无 payload_hash 不落盘，避免中途指纹失败把大 prompt 写成基线
        if not getattr(snap, "payload_hash", None):
            return
        workspace_dir = getattr(coara, "workspace_dir", None)
        if not workspace_dir:
            return
        model = str(getattr(coara, "model_name", "") or "")
        save_context_baseline(
            workspace_dir,
            model=model,
            prompt_tokens=prompt,
            payload_hash=getattr(snap, "payload_hash", None),
            configured_home=configured_home,
        )
    except Exception as exc:
        logger.debug(f"context baseline save skipped: {exc}")


def inject_workspace_baseline(
    coara: Any,
    *,
    current_payload_hash: str | None = None,
    configured_home: str | Path | None = None,
) -> bool:
    """Inject stored baseline into ``coara._llm_usage_snapshot`` after /new.

    Returns True when an estimate was injected. Hash mismatch → leave at 0.
    """
    snap = getattr(coara, "_llm_usage_snapshot", None)
    if snap is None:
        return False
    workspace_dir = getattr(coara, "workspace_dir", None)
    if not workspace_dir:
        return False
    model = str(getattr(coara, "model_name", "") or "")
    baseline = load_context_baseline(workspace_dir, model=model, configured_home=configured_home)
    if baseline is None:
        return False
    want = str(current_payload_hash or "").strip() or None
    if want and baseline.payload_hash and want != baseline.payload_hash:
        return False
    snap.inject_estimate(baseline.prompt_tokens, payload_hash=baseline.payload_hash or want)
    return True
