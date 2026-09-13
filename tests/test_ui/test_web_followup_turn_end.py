"""P0-18: web followup TurnStream must close when turn_end misses exact turn_id."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.events import TraceEvent
from src.ui.turn_stream import TurnStream
from src.ui.web_server import WebServer


def _server_with_followup(*, turn_id: str, session_id: str = "sess-a") -> tuple[WebServer, TurnStream, MagicMock]:
    registry = MagicMock()
    sender = MagicMock()
    registry.sender_for.return_value = sender
    server = WebServer.__new__(WebServer)
    server.root = SimpleNamespace(end_registry=registry)
    server.workspace_dir = SimpleNamespace()  # unused when view_store is None
    server.coara_home = None
    server._view_store = None
    server._web_followup_view_turns = {(session_id, turn_id)}
    server._WebServer__turns = {}
    server.registry = SimpleNamespace(has_active=lambda: False, send_to_active_nowait=lambda _m: None)
    server.attach_registry = SimpleNamespace(send_to_nowait=lambda *_a, **_k: None)
    stream = TurnStream(
        turn_id,
        "web",
        "root",
        server,
        channel_id="followup-web",
        session_id=session_id,
    )
    server._turns[f"followup-{turn_id}"] = stream
    return server, stream, registry


def test_followup_turn_end_matches_exact_turn_id() -> None:
    server, stream, registry = _server_with_followup(turn_id="t-exact")
    event = TraceEvent(
        coara_id="c1",
        coara_name="root",
        event_type="turn_end",
        message="done",
        payload={"turn_id": "t-exact", "session_id": "sess-a", "origin_scope": "main_loop"},
    )
    server._on_web_followup_turn_end(event)
    assert stream.done
    assert server._web_followup_view_turns == set()
    registry.unregister.assert_called_once_with("web", registry.sender_for.return_value, "sess-a")


def test_followup_turn_end_falls_back_to_session_when_turn_id_diverges() -> None:
    """Inject snapshot turn_id != closing event (leftover / empty snapshot)."""
    server, stream, registry = _server_with_followup(turn_id="snap-old")
    # Stream still keyed by snapshot id; event carries the real closing id
    event = TraceEvent(
        coara_id="c1",
        coara_name="root",
        event_type="turn_end",
        message="done",
        payload={"turn_id": "t-real", "session_id": "sess-a", "origin_scope": "main_loop"},
    )
    server._on_web_followup_turn_end(event)
    assert stream.done
    assert server._web_followup_view_turns == set()
    registry.unregister.assert_called_once()


def test_followup_turn_end_ignores_non_main_loop_scope() -> None:
    server, stream, _registry = _server_with_followup(turn_id="t1")
    event = TraceEvent(
        coara_id="c1",
        coara_name="aide",
        event_type="turn_end",
        message="sub done",
        payload={"turn_id": "t1", "session_id": "sess-a", "origin_scope": "subagent"},
    )
    server._on_web_followup_turn_end(event)
    assert not stream.done
    assert ("sess-a", "t1") in server._web_followup_view_turns
