"""Subscribe to trace events and enqueue compact usage records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.coara_home import current_coara_home
from src.core.config import config_manager
from src.core.events import TraceEvent
from src.core.logger import logger
from src.runtime.usage_attribution import parse_agent_kind, resolve_agent_kind
from src.runtime.usage_query import resolve_usage_path_for_root
from src.runtime.usage_store import UsageStore, load_usage_store_config, resolve_usage_events_path


def _usage_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )
    out: dict[str, int] = {}
    for key in fields:
        val = _usage_int(usage.get(key))
        if val is not None and val >= 0:
            out[key] = val
    return out


def _events_path_for_payload(payload: dict[str, Any], store: UsageStore) -> Path:
    raw_dir = str(payload.get("workspace_dir") or "").strip()
    if raw_dir:
        try:
            return resolve_usage_events_path(Path(raw_dir), coara_home=current_coara_home())
        except Exception:
            logger.warning(
                f"resolve usage events path for workspace_dir {raw_dir!r} failed; using store default", exc_info=True
            )
    return store.events_path


def _attribution_fields(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build attribution fields; return None when agent_kind cannot be resolved."""
    agent_kind = parse_agent_kind(payload.get("agent_kind"))
    if agent_kind is None:
        persona = str(payload.get("persona") or "")
        # Trace payload already stamped by attribution_from_coara; if somehow
        # missing, only persona (write-time identity) may recover — not coara_name.
        if not persona.strip():
            return None
        agent_kind = resolve_agent_kind(persona=persona, user_facing=False)
        if parse_agent_kind(agent_kind) is None:
            return None
    fields: dict[str, Any] = {"agent_kind": agent_kind}
    for key in ("persona", "workspace_id", "workspace_name", "workspace_dir"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            fields[key] = val
    return fields


def _record_llm_turn(store: UsageStore, event: TraceEvent) -> None:
    payload = event.payload or {}
    llm_output = payload.get("llm_output") or {}
    usage = _normalize_usage(llm_output.get("usage"))
    if not usage and not payload.get("has_tool_calls"):
        return
    attrs = _attribution_fields(payload)
    if attrs is None:
        return
    record: dict[str, Any] = {
        "kind": "llm_turn",
        "session_id": payload.get("session_id") or "",
        "turn_id": payload.get("turn_id") or "",
        "coara_id": event.coara_id,
        "coara_name": event.coara_name,
        "model": payload.get("model") or "",
        "provider": payload.get("provider") or "",
        "iteration": _usage_int(payload.get("iteration")),
        "has_tool_calls": bool(payload.get("has_tool_calls")),
        "usage": usage,
    }
    finish_reason = str(llm_output.get("finish_reason") or "").strip().lower()
    if finish_reason == "partial":
        # 无 finish chunk 的静默截断：标 partial 与完整回合区分，聚合口径不变
        record["status"] = "partial"
    record.update(attrs)
    store.record(record, events_path=_events_path_for_payload(payload, store))


def _record_llm_partial(store: UsageStore, event: TraceEvent) -> None:
    """Record provider-reported usage from a failed mid-stream LLM attempt.

    流式中途失败时 provider 可能已计量部分 token；以 ``kind=llm_turn`` +
    ``status=partial`` 入账，与正常回合同一聚合口径，保证账面与 provider
    扣费可对账
    """
    payload = event.payload or {}
    usage = _normalize_usage(payload.get("usage"))
    if not usage:
        return
    attrs = _attribution_fields(payload)
    if attrs is None:
        return
    record: dict[str, Any] = {
        "kind": "llm_turn",
        "status": "partial",
        "session_id": payload.get("session_id") or "",
        "turn_id": payload.get("turn_id") or "",
        "coara_id": event.coara_id,
        "coara_name": event.coara_name,
        "model": payload.get("model") or "",
        "provider": payload.get("provider") or "",
        "iteration": _usage_int(payload.get("iteration")),
        "has_tool_calls": False,
        "usage": usage,
    }
    record.update(attrs)
    store.record(record, events_path=_events_path_for_payload(payload, store))


def _record_tool_complete(store: UsageStore, event: TraceEvent) -> None:
    payload = event.payload or {}
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name:
        return
    attrs = _attribution_fields(payload)
    if attrs is None:
        return
    record: dict[str, Any] = {
        "kind": "tool",
        "session_id": payload.get("session_id") or "",
        "coara_id": event.coara_id,
        "coara_name": event.coara_name,
        "tool": tool_name,
        "tool_call_id": payload.get("tool_call_id") or "",
        "duration_ms": payload.get("duration_ms"),
        "is_error": bool(payload.get("is_error")),
        "cache_hit": bool(payload.get("cache_hit")),
    }
    record.update(attrs)
    raw_args = payload.get("usage_args")
    if isinstance(raw_args, dict) and raw_args:
        record["args"] = raw_args
    fetch_meta = payload.get("fetch_meta")
    if isinstance(fetch_meta, dict) and fetch_meta:
        record["fetch"] = fetch_meta
    search_meta = payload.get("search_meta")
    if isinstance(search_meta, dict) and search_meta:
        record["search"] = search_meta
    store.record(record, events_path=_events_path_for_payload(payload, store))


class UsageCollector:
    """EventBus sync subscriber → UsageStore queue."""

    def __init__(self, store: UsageStore) -> None:
        self.store = store

    def handle_event(self, event: TraceEvent) -> None:
        event_type = event.event_type
        if event_type == "llm_turn_complete":
            _record_llm_turn(self.store, event)
            return
        if event_type == "llm_turn_partial":
            _record_llm_partial(self.store, event)
            return
        if event_type == "tool_complete":
            _record_tool_complete(self.store, event)
            return
        if event_type in {"turn_failed", "turn_interrupted"}:
            payload = event.payload or {}
            attrs = _attribution_fields(payload)
            if attrs is None:
                return
            record = {
                "kind": event_type,
                "session_id": payload.get("session_id") or "",
                "coara_id": event.coara_id,
                "coara_name": event.coara_name,
                "message": (event.message or "")[:240],
            }
            record.update(attrs)
            self.store.record(record, events_path=_events_path_for_payload(payload, self.store))


def attach_usage_collector(root: Any) -> UsageCollector | None:
    """Wire usage collection on RootCoara (no-op when disabled)."""
    raw = getattr(config_manager, "_raw_config", {}) or {}
    cfg = load_usage_store_config(raw)
    if not cfg.enabled:
        return None

    store = UsageStore(resolve_usage_path_for_root(root), config=cfg)
    collector = UsageCollector(store)

    def on_event(event: TraceEvent) -> None:
        collector.handle_event(event)

    subscription = root.event_bus.subscribe(on_event, topic=None)
    root._usage_subscription = subscription  # noqa: SLF001
    root._usage_store = store  # noqa: SLF001
    return collector


def shutdown_usage_collector(root: Any) -> None:
    subscription = getattr(root, "_usage_subscription", None)
    if subscription is not None:
        subscription.unsubscribe()
        root._usage_subscription = None  # noqa: SLF001
    store = getattr(root, "_usage_store", None)
    if store is not None:
        store.flush(timeout=1.0)
        store.close(timeout=2.0)
        root._usage_store = None  # noqa: SLF001


__all__ = [
    "UsageCollector",
    "attach_usage_collector",
    "shutdown_usage_collector",
]
