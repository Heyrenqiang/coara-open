"""_emit_trace 无 payload 时也要补 source（completed 等收尾事件）。"""

from __future__ import annotations

from types import SimpleNamespace

from src.coara.base import CoaraBase


def test_emit_trace_none_payload_gets_active_source(monkeypatch) -> None:
    captured: list[dict] = []

    class _Emitter:
        def emit(self, event_type, message, *, payload=None, level="info", origin_scope=None):
            captured.append({"type": event_type, "payload": payload})

    coara = object.__new__(CoaraBase)
    coara._active_turn_source = "cli-attached"
    coara._trace_emitter = _Emitter()
    coara.identity = SimpleNamespace(user_facing=True, is_owner_context=True)
    monkeypatch.setattr(coara, "is_flow_subject", lambda: False)

    coara._emit_trace("completed", "done")
    assert captured and captured[0]["type"] == "completed"
    assert (captured[0]["payload"] or {}).get("source") == "cli-attached"


def test_emit_trace_does_not_override_explicit_source(monkeypatch) -> None:
    captured: list[dict] = []

    class _Emitter:
        def emit(self, event_type, message, *, payload=None, level="info", origin_scope=None):
            captured.append(payload or {})

    coara = object.__new__(CoaraBase)
    coara._active_turn_source = "cli-attached"
    coara._trace_emitter = _Emitter()
    coara.identity = SimpleNamespace(user_facing=True, is_owner_context=True)
    monkeypatch.setattr(coara, "is_flow_subject", lambda: False)

    coara._emit_trace("completed", "done", payload={"source": "web"})
    assert captured[0].get("source") == "web"


def test_emit_trace_stamps_remote_channel_id(monkeypatch) -> None:
    captured: list[dict] = []

    class _Emitter:
        def emit(self, event_type, message, *, payload=None, level="info", origin_scope=None):
            captured.append(payload or {})

    coara = object.__new__(CoaraBase)
    coara._active_turn_source = "cli-attached"
    coara._trace_emitter = _Emitter()
    coara.identity = SimpleNamespace(user_facing=True, is_owner_context=True)
    monkeypatch.setattr(coara, "is_flow_subject", lambda: False)
    monkeypatch.setattr(
        "src.coara.turn_context.get_turn_channel_id",
        lambda: "conn-a",
    )

    coara._emit_trace("thinking_progress", "…")
    assert captured[0].get("source") == "cli-attached"
    assert captured[0].get("channel_id") == "conn-a"
