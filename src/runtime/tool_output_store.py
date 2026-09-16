"""Persist large tool results under coara Home; expose ref + summary to the model."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths
from src.core.config import config_manager
from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.time import now_iso, parse_iso_to_datetime
from src.core.tool_base import ToolResult
from src.core.types import ToolOutputStoreConfig
from src.runtime.spill_policy import SpillKeep, build_spill_preview, resolve_spill_policy

_PRUNE_EVERY_N_WRITES = 10
_write_counter = 0


def _content_byte_len(content: str) -> int:
    return len(content.encode("utf-8"))


def model_facing_byte_len(result: ToolResult) -> int:
    """Bytes that would enter message_history for this tool result."""
    if result.is_error or getattr(result, "is_cancelled", False):
        return 0
    cached = getattr(result, "_cached_model_facing_bytes", None)
    if isinstance(cached, int) and cached >= 0:
        return cached
    if not isinstance(result.content, str):
        return len(str(result.content).encode("utf-8"))
    return _content_byte_len(result.content)


def format_spilled_tool_content(
    *,
    ref: str,
    content: str,
    store_path: Path,
    preview: str,
    keep: SpillKeep,
) -> str:
    lines = content.count("\n") + (1 if content else 0)
    byte_len = _content_byte_len(content)
    return (
        f'<tool_output ref="{ref}" bytes="{byte_len}" lines="{lines}">\n'
        f'已落盘，用 read(ref="{ref}", offset/limit) 分段读。\n\n'
        f"预览:\n{preview}\n"
        f"</tool_output>"
    )


def maybe_spill_tool_result(
    *,
    workspace_dir: Path | str,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    arguments: dict[str, Any] | None,
    result: ToolResult,
    coara_home: Path | None = None,
    tool_category: str | None = None,
    force: bool = False,
) -> ToolResult:
    """Replace oversized successful text results with a ref summary.

    Per-tool thresholds come from ``spill_policy`` (Qwen-aligned). ``force=True`` is used
    by batch-budget offload and ignores the per-tool threshold (not self-managed skips).
    """
    settings = get_tool_output_store_config()
    if not settings.enabled:
        return result
    if result.is_error or getattr(result, "is_cancelled", False):
        return result
    if not isinstance(result.content, str):
        return result
    if (result.metadata or {}).get("output_spilled"):
        return result

    content = result.content
    byte_len = _content_byte_len(content)
    # Cache on the result object only (never in metadata / LLM history).
    result._cached_model_facing_bytes = byte_len  # type: ignore[attr-defined]  # 运行期缓存属性，刻意不进 ToolResult 模型/metadata
    threshold, keep = resolve_spill_policy(tool_name, settings, tool_category=tool_category)

    if not force:
        if threshold is None or byte_len <= threshold:
            return result
    elif byte_len <= 256:
        # Batch offload: do not spill trivial bodies
        return result

    store = ToolOutputStore(workspace_dir=workspace_dir, session_id=session_id, coara_home=coara_home)
    record = store.write(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        content=content,
        arguments=arguments,
    )
    preview = build_spill_preview(
        content,
        head_chars=settings.preview_head_chars,
        tail_chars=settings.preview_tail_chars,
        keep=keep,
    )
    spilled = format_spilled_tool_content(
        ref=record.ref,
        content=content,
        store_path=store.session_dir / f"{record.ref}.json",
        preview=preview,
        keep=keep,
    )
    metadata = dict(result.metadata or {})
    metadata.update(
        {
            "output_ref": record.ref,
            "output_bytes": record.bytes,
            "output_path": str(store.session_dir / f"{record.ref}.json"),
            "output_spilled": True,
            "spill_keep": keep.value,
        }
    )
    return ToolResult.success(content=spilled, metadata=metadata, display=result.display)


def prune_stale_tool_outputs(
    *,
    workspace_dir: Path | str,
    coara_home: Path | None = None,
    retention_days: int | None = None,
) -> int:
    """Delete spilled tool outputs older than ``retention_days``. Returns files removed."""
    settings = get_tool_output_store_config()
    if not settings.enabled:
        return 0
    days = retention_days if retention_days is not None else settings.retention_days
    if days <= 0:
        return 0

    root = Path(workspace_dir).resolve()
    paths = CoaraHomePaths.for_workspace(root, configured_home=coara_home, migrate=False)
    outputs_root = paths.tool_outputs_dir
    if not outputs_root.is_dir():
        return 0

    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for session_dir in outputs_root.iterdir():
        if not session_dir.is_dir():
            continue
        for payload_path in session_dir.glob("*.json"):
            try:
                data = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            created = parse_iso_to_datetime(str(data.get("created_at") or ""))
            if created is None or created >= cutoff:
                continue
            try:
                payload_path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning("Failed to prune tool output {}: {}", payload_path, exc)
        _rebuild_tool_output_index(session_dir)
        if not any(session_dir.glob("*.json")):
            # 原子清空：进程被杀在 open("w") 与 close 之间会留下半截 index
            #（payload 本体还在可重建，但原子写连这个边角也堵住）。
            from src.core.json_store import write_text_atomic

            write_text_atomic(session_dir / "index.jsonl", "")

    if removed:
        logger.info("Pruned {} stale tool output file(s) (>{} days)", removed, days)
    return removed


def _rebuild_tool_output_index(session_dir: Path) -> None:
    rows: list[dict[str, Any]] = []
    for payload_path in sorted(session_dir.glob("*.json")):
        try:
            data = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ref = str(data.get("ref") or payload_path.stem)
        rows.append(
            {
                "ref": ref,
                "tool_name": data.get("tool_name", ""),
                "tool_call_id": data.get("tool_call_id", ""),
                "bytes": data.get("bytes", 0),
                "created_at": data.get("created_at", ""),
                "path": str(payload_path),
                "session_id": session_dir.name,
            }
        )
    index_path = session_dir / "index.jsonl"
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    index_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _maybe_prune_after_write(*, workspace_dir: Path, coara_home: Path | None) -> None:
    global _write_counter
    _write_counter += 1
    if _write_counter % _PRUNE_EVERY_N_WRITES != 0:
        return
    try:
        prune_stale_tool_outputs(workspace_dir=workspace_dir, coara_home=coara_home)
    except Exception as exc:
        logger.warning("Tool output retention prune failed: {}", exc)


def apply_batch_spill_budget(
    *,
    workspace_dir: Path | str,
    session_id: str,
    items: list[tuple[str, str, ToolResult, str | None]],
    coara_home: Path | None = None,
) -> list[ToolResult]:
    """Offload largest tool outputs when a parallel batch exceeds ``batch_budget_bytes``.

    Each item is ``(tool_name, tool_call_id, result, tool_category)``. Returns updated results
    in the same order (mutates are returned as new ToolResult instances).
    """
    settings = get_tool_output_store_config()
    if not settings.enabled or settings.batch_budget_bytes <= 0:
        return [item[2] for item in items]

    results = [item[2] for item in items]
    total = sum(model_facing_byte_len(r) for r in results)
    if total <= settings.batch_budget_bytes:
        return results

    # Largest first; skip already spilled (they are already small summaries)
    order = sorted(
        range(len(items)),
        key=lambda i: model_facing_byte_len(results[i]),
        reverse=True,
    )
    for idx in order:
        if total <= settings.batch_budget_bytes:
            break
        tool_name, tool_call_id, result, tool_category = items[idx]
        if (result.metadata or {}).get("output_spilled"):
            continue
        before = model_facing_byte_len(result)
        if before <= 256:
            continue
        updated = maybe_spill_tool_result(
            workspace_dir=workspace_dir,
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=None,
            result=result,
            coara_home=coara_home,
            tool_category=tool_category,
            force=True,
        )
        after = model_facing_byte_len(updated)
        if after < before:
            results[idx] = updated
            total -= before - after

    return results


@dataclass(slots=True)
class ToolOutputRecord:
    ref: str
    tool_name: str
    tool_call_id: str
    content: str
    bytes: int
    created_at: str
    arguments: dict[str, Any]
    session_id: str = ""


def get_tool_output_store_config() -> ToolOutputStoreConfig:
    cfg = getattr(config_manager, "_config", None)
    if cfg is not None and cfg.runtime_enhancements is not None:
        return cfg.runtime_enhancements.tool_output_store
    raw = getattr(config_manager, "_raw_config", {}).get("runtime_enhancements") or {}
    store_raw = raw.get("tool_output_store") or {}
    return ToolOutputStoreConfig.model_validate(store_raw)


class ToolOutputStore:
    """Session-scoped spill files under ``{workspace_home}/tool_outputs/{session_id}/``."""

    def __init__(self, *, workspace_dir: Path | str, session_id: str, coara_home: Path | None = None) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.session_id = session_id
        self._coara_home = coara_home
        paths = CoaraHomePaths.for_workspace(self.workspace_dir, configured_home=coara_home, migrate=False)
        self._session_dir = paths.tool_outputs_dir / session_id
        self._index_path = self._session_dir / "index.jsonl"

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    def write(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        content: str,
        arguments: dict[str, Any] | None = None,
    ) -> ToolOutputRecord:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        ref = uuid.uuid4().hex[:8]
        payload: dict[str, Any] = {
            "ref": ref,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "arguments": arguments or {},
            "content": content,
            "bytes": len(content.encode("utf-8")),
            "created_at": now_iso(),
        }
        out_path = self._session_dir / f"{ref}.json"
        write_json_atomic(out_path, payload)
        with self._index_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ref": ref,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "bytes": payload["bytes"],
                        "created_at": payload["created_at"],
                        "path": str(out_path),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        _maybe_prune_after_write(workspace_dir=self.workspace_dir, coara_home=self._coara_home)
        return ToolOutputRecord(
            ref=ref,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=content,
            bytes=payload["bytes"],
            created_at=payload["created_at"],
            arguments=arguments or {},
            session_id=self.session_id,
        )

    def load(self, ref: str) -> ToolOutputRecord:
        path = self._session_dir / f"{ref.strip()}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Tool output ref not found: {ref}")
        data = json.loads(path.read_text(encoding="utf-8"))
        content = str(data.get("content") or "")
        return ToolOutputRecord(
            ref=str(data.get("ref") or ref),
            tool_name=str(data.get("tool_name") or ""),
            tool_call_id=str(data.get("tool_call_id") or ""),
            content=content,
            bytes=int(data.get("bytes") or len(content.encode("utf-8"))),
            created_at=str(data.get("created_at") or ""),
            arguments=dict(data.get("arguments") or {}),
            session_id=self.session_id,
        )

    def list_index(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self._index_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("session_id", self.session_id)
                rows.append(row)
        if limit <= 0:
            return rows
        return rows[-limit:]


def find_tool_output_record(
    *,
    workspace_dir: Path | str,
    ref: str,
    session_id: str | None = None,
    coara_home: Path | None = None,
) -> ToolOutputRecord:
    """Load a spilled tool output, optionally scoped to one session."""
    cleaned_ref = ref.strip()
    if not cleaned_ref:
        raise FileNotFoundError("Tool output ref is empty")
    if session_id:
        return ToolOutputStore(workspace_dir=workspace_dir, session_id=session_id, coara_home=coara_home).load(
            cleaned_ref
        )

    paths = CoaraHomePaths.for_workspace(Path(workspace_dir).resolve(), configured_home=coara_home, migrate=False)
    outputs_root = paths.tool_outputs_dir
    if not outputs_root.is_dir():
        raise FileNotFoundError(f"Tool output ref not found: {cleaned_ref}")
    for session_dir in sorted(outputs_root.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        candidate = session_dir / f"{cleaned_ref}.json"
        if candidate.is_file():
            return ToolOutputStore(
                workspace_dir=workspace_dir,
                session_id=session_dir.name,
                coara_home=coara_home,
            ).load(cleaned_ref)
    raise FileNotFoundError(f"Tool output ref not found: {cleaned_ref}")


def list_tool_output_index(
    *,
    workspace_dir: Path | str,
    session_id: str | None = None,
    coara_home: Path | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List spilled tool outputs for dashboard browsing."""
    root = Path(workspace_dir).resolve()
    paths = CoaraHomePaths.for_workspace(root, configured_home=coara_home, migrate=False)
    outputs_root = paths.tool_outputs_dir
    if session_id:
        return ToolOutputStore(workspace_dir=root, session_id=session_id, coara_home=coara_home).list_index(limit=limit)

    if not outputs_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for session_dir in sorted(outputs_root.iterdir(), reverse=True):
        if not session_dir.is_dir():
            continue
        store = ToolOutputStore(workspace_dir=root, session_id=session_dir.name, coara_home=coara_home)
        rows.extend(store.list_index(limit=0))
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    if limit <= 0:
        return rows
    return rows[:limit]
