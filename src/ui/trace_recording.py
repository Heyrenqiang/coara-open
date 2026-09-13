"""Shared trace persistence wiring for CLI / Web entrypoints.

Supports concurrent turns across workspaces: after a human switch, the
departing session may keep emitting traces. Events are routed by
``payload.workspace_dir`` into per-workspace ``TraceStore`` instances; stores
for non-foreground workspaces stay open until shutdown.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.coara.event_bus import EventBus, Subscription
from src.core.events import TraceEvent
from src.core.logger import logger
from src.ui.trace_store import TraceStore

# Events that should be durable before the dashboard reads JSONL.
_DASHBOARD_FLUSH_EVENTS = frozenset(
    {
        "conversation_message",
        "llm_turn_start",
        "llm_turn_complete",
        "tool_call",
        "tool_start",
        "tool_complete",
        "final_response",
        "completed",
        "turn_interrupted",
        "turn_failed",
        "turn_timing",
        "subagent_start",
        "subagent_complete",
        "subagent_failed",
    }
)


# Flush every Nth dashboard-relevant event instead of every one: the flush is
# synchronous in the publisher's call stack and can block the event loop up to
# 200ms, and a turn publishes many of these events. Turn-terminal events always
# flush immediately so end-of-turn state is durable promptly.
_FLUSH_EVERY_N_EVENTS = 5
_TURN_TERMINAL_EVENTS = frozenset(
    {
        "final_response",
        "completed",
        "turn_interrupted",
        "turn_failed",
    }
)


def _workspace_key(workspace_dir: Path | str) -> str:
    return str(Path(workspace_dir).expanduser().resolve())


class MultiWorkspaceTracePersistence:
    """Route EventBus traces to the correct workspace TraceStore.

    ``workspace_switched`` only moves the foreground pointer — it must not
    close the previous store while that workspace may still be finishing a turn.
    """

    def __init__(
        self,
        event_bus: EventBus,
        workspace_dir: Path | str,
        *,
        coara_home: Path | str | None = None,
        on_foreground_changed: Any | None = None,
    ) -> None:
        self._coara_home = coara_home
        self._stores: dict[str, TraceStore] = {}
        self._flush_counters: dict[str, int] = {}
        self._on_foreground_changed = on_foreground_changed
        self._fg_key = _workspace_key(workspace_dir)
        self._ensure_store(workspace_dir)
        self._subscription = event_bus.subscribe(self._on_event, topic=None)

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    @property
    def store(self) -> TraceStore:
        """Foreground workspace store (REST / dashboard reads)."""
        return self._stores[self._fg_key]

    def store_for(self, workspace_dir: Path | str) -> TraceStore:
        return self._ensure_store(workspace_dir)

    def set_foreground(self, workspace_dir: Path | str) -> TraceStore:
        store = self._ensure_store(workspace_dir)
        self._fg_key = _workspace_key(workspace_dir)
        if self._on_foreground_changed is not None:
            try:
                self._on_foreground_changed(store)
            except Exception as exc:
                logger.debug(f"trace persistence foreground callback failed: {exc}")
        return store

    def close(self) -> None:
        self._subscription.unsubscribe()
        for store in self._stores.values():
            try:
                store.flush(timeout=1.0)
                store.close()
            except Exception as exc:
                logger.debug(f"trace store close failed: {exc}")
        self._stores.clear()

    def _ensure_store(self, workspace_dir: Path | str) -> TraceStore:
        key = _workspace_key(workspace_dir)
        store = self._stores.get(key)
        if store is None:
            store = TraceStore(workspace_dir, coara_home=self._coara_home)
            self._stores[key] = store
            self._flush_counters[key] = 0
        return store

    def _resolve_store(self, event: TraceEvent) -> TraceStore:
        payload = event.payload or {}
        if event.event_type == "workspace_switched":
            wd = str(payload.get("workspace_dir") or "").strip()
            if wd:
                return self.set_foreground(wd)
            return self.store
        wd = str(payload.get("workspace_dir") or "").strip()
        if wd:
            return self._ensure_store(wd)
        # Legacy events without workspace_dir: keep previous behavior.
        return self.store

    def _on_event(self, event: TraceEvent) -> None:
        store = self._resolve_store(event)
        key = _workspace_key(store.workspace_dir)
        store.append_event(event)
        store.sync_runtime_from_event(event)
        if event.event_type not in _DASHBOARD_FLUSH_EVENTS:
            return
        self._flush_counters[key] = self._flush_counters.get(key, 0) + 1
        if event.event_type in _TURN_TERMINAL_EVENTS or self._flush_counters[key] % _FLUSH_EVERY_N_EVENTS == 0:
            store.flush(timeout=0.2)


def subscribe_trace_persistence(event_bus: EventBus, store: TraceStore) -> Subscription:
    """Persist all trace events to *one* workspace store (tests / simple hosts).

    Prefer :class:`MultiWorkspaceTracePersistence` for long-lived CLI/Web roots
    that allow mid-turn workspace switches.
    """
    flush_counter = 0

    def on_event(event: TraceEvent) -> None:
        nonlocal flush_counter
        store.append_event(event)
        store.sync_runtime_from_event(event)
        if event.event_type not in _DASHBOARD_FLUSH_EVENTS:
            return
        flush_counter += 1
        if event.event_type in _TURN_TERMINAL_EVENTS or flush_counter % _FLUSH_EVERY_N_EVENTS == 0:
            store.flush(timeout=0.2)

    return event_bus.subscribe(on_event, topic=None)


def make_workspace_switched_handler(
    root: Any,
    coara_home: Any,
    trace_holder: list,
    subscriptions: list | None = None,
) -> Any:
    """Update the foreground TraceStore pointer after ``workspace_switched``.

    When ``trace_holder[0]`` is backed by :class:`MultiWorkspaceTracePersistence`
    (``trace_holder[2]`` optional router ref), only the foreground pointer moves —
    other workspace stores stay open for in-flight turns.

    Legacy ``[store, sub]`` holders without a router still recreate a single store
    (tests / minimal hosts).
    """

    def on_workspace_switched(event: Any) -> None:
        if getattr(event, "event_type", "") != "workspace_switched":
            return
        router = None
        if len(trace_holder) >= 3:
            router = trace_holder[2]
        if router is None:
            router = getattr(root, "trace_persistence", None)
        if isinstance(router, MultiWorkspaceTracePersistence):
            new_store = router.set_foreground(root.foreground_coara.workspace_dir)
            # Keep subscription stable; only refresh the store pointer.
            if len(trace_holder) >= 2:
                trace_holder[0] = new_store
            else:
                trace_holder[:] = [new_store, router.subscription, router]
            return

        # Legacy single-store swap (closes previous — unsafe if origin turn still runs).
        old_store, old_sub = trace_holder[0], trace_holder[1]
        old_store.flush(timeout=1.0)
        old_store.close()
        old_sub.unsubscribe()
        if subscriptions is not None and old_sub in subscriptions:
            subscriptions.remove(old_sub)
        new_store = TraceStore(root.foreground_coara.workspace_dir, coara_home=coara_home)
        new_sub = subscribe_trace_persistence(root.event_bus, new_store)
        if subscriptions is not None:
            subscriptions.append(new_sub)
        trace_holder[:] = [new_store, new_sub]

    return on_workspace_switched


def install_multi_workspace_trace_persistence(
    root: Any,
    workspace_dir: Path | str,
    *,
    coara_home: Path | str | None = None,
    trace_holder: list | None = None,
) -> MultiWorkspaceTracePersistence:
    """Install routed persistence on *root* and optionally seed *trace_holder*."""

    def _on_fg(store: TraceStore) -> None:
        if trace_holder is not None and trace_holder:
            trace_holder[0] = store

    persistence = MultiWorkspaceTracePersistence(
        root.event_bus,
        workspace_dir,
        coara_home=coara_home,
        on_foreground_changed=_on_fg,
    )
    root.trace_persistence = persistence
    if trace_holder is not None:
        trace_holder[:] = [persistence.store, persistence.subscription, persistence]
    return persistence
