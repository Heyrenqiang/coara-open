"""Tests for usage attribution and dashboard aggregation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.events import TraceEvent
from src.runtime.usage_attribution import attribution_from_coara, parse_agent_kind, resolve_agent_kind
from src.runtime.usage_collector import UsageCollector
from src.runtime.usage_query import resolve_workspace_display_name, summarize_usage_dashboard
from src.runtime.usage_store import UsageStore, UsageStoreConfig


def test_resolve_workspace_display_name_alias_then_slug() -> None:
    assert (
        resolve_workspace_display_name(
            "nx-c34a1b2c3d",
            aliases={"nx-c34a1b2c3d": "nx"},
        )
        == "nx"
    )
    assert resolve_workspace_display_name("v8-bd788e1c25") == "v8"
    assert resolve_workspace_display_name("plain-id") == "plain-id"
    assert resolve_workspace_display_name("") == "(unknown)"


def test_parse_and_resolve_agent_kind() -> None:
    assert parse_agent_kind("janitor") == "janitor"
    assert parse_agent_kind("other") is None
    assert parse_agent_kind("") is None
    assert parse_agent_kind(None) is None
    assert resolve_agent_kind(persona="daily") == "daily"
    assert resolve_agent_kind(persona="", user_facing=True) == "root"
    assert resolve_agent_kind(persona="", user_facing=False) == "root"


def test_attribution_from_coara_prefers_persona_for_subagents(tmp_path: Path) -> None:
    coara = SimpleNamespace(
        workspace_dir=tmp_path,
        workspace_manager=None,
        identity=SimpleNamespace(
            name="sa-janitor-abcd1234",
            user_facing=False,
            persona=SimpleNamespace(name="janitor"),
        ),
    )
    attrs = attribution_from_coara(coara)
    assert attrs["agent_kind"] == "janitor"
    assert attrs["persona"] == "janitor"
    assert attrs["workspace_dir"] == str(tmp_path)


def test_usage_store_routes_to_per_record_path(tmp_path: Path) -> None:
    default_path = tmp_path / "default" / "events.jsonl"
    other_path = tmp_path / "other" / "events.jsonl"
    store = UsageStore(default_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    store.record({"kind": "llm_turn", "usage": {"input_tokens": 1}}, events_path=other_path)
    store.flush(timeout=2.0)
    store.close(timeout=2.0)
    assert not default_path.exists()
    rows = other_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["kind"] == "llm_turn"
    assert "_events_path" not in payload


def test_collector_writes_attribution_fields(tmp_path: Path) -> None:
    default_path = tmp_path / "fg" / "events.jsonl"
    store = UsageStore(default_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)
    collector.handle_event(
        TraceEvent(
            coara_id="c1",
            coara_name="sa-janitor-abcd1234",
            event_type="llm_turn_complete",
            message="llm",
            payload={
                "session_id": "s1",
                "model": "m",
                "provider": "p",
                "has_tool_calls": False,
                "agent_kind": "janitor",
                "persona": "janitor",
                "workspace_id": "ws-a",
                "workspace_name": "Alpha",
                "llm_output": {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 4,
                    }
                },
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    payload = json.loads(default_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload["agent_kind"] == "janitor"
    assert payload["workspace_id"] == "ws-a"
    assert payload["workspace_name"] == "Alpha"
    assert payload["usage"]["input_tokens"] == 10


def test_summarize_usage_dashboard_groups_by_kind_and_workspace(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    older = (now - timedelta(days=2)).isoformat()
    recent = now.isoformat()
    ws1 = tmp_path / "workspaces" / "w1" / "usage"
    ws2 = tmp_path / "workspaces" / "w2" / "usage"
    ws1.mkdir(parents=True)
    ws2.mkdir(parents=True)
    (ws1 / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "llm_turn",
                        "ts": recent,
                        "agent_kind": "root",
                        "workspace_id": "w1",
                        "workspace_name": "One",
                        "model": "m1",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "cache_read_input_tokens": 40,
                        },
                    }
                ),
                json.dumps(
                    {
                        "kind": "llm_turn",
                        "ts": recent,
                        "agent_kind": "janitor",
                        "workspace_id": "w1",
                        "workspace_name": "One",
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                    }
                ),
                json.dumps(
                    {
                        "kind": "llm_turn",
                        "ts": recent,
                        # Missing agent_kind — skipped (no legacy name inference)
                        "coara_name": "sa-janitor-abcd1234",
                        "workspace_id": "w1",
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                    }
                ),
                json.dumps(
                    {
                        "kind": "llm_turn",
                        "ts": older,
                        "agent_kind": "root",
                        "workspace_id": "w1",
                        "usage": {"input_tokens": 999, "output_tokens": 1},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (ws2 / "events.jsonl").write_text(
        json.dumps(
            {
                "kind": "llm_turn",
                "ts": recent,
                "agent_kind": "daily",
                "workspace_id": "w2",
                "workspace_name": "Two",
                "usage": {"input_tokens": 30, "output_tokens": 3, "cached_tokens": 7},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # days=1 should exclude the older root turn
    dash = summarize_usage_dashboard(
        days=1,
        coara_home=tmp_path,
        event_paths=[ws1 / "events.jsonl", ws2 / "events.jsonl"],
    )
    # Anthropic-style root turn: effective prompt = 100+40=140, hit=40/140
    # janitor: 20 input, no cache
    # daily (OpenAI cached_tokens): effective prompt = 30, hit=7/30
    assert dash["totals"]["input_tokens"] == 190  # 140 + 20 + 30
    assert dash["totals"]["output_tokens"] == 18
    assert dash["totals"]["cache_read_tokens"] == 47  # 40 + 0 + 7
    assert dash["totals"]["llm_turns"] == 3
    assert dash["totals"]["cache_hit_rate"] == round(47 / 190, 4)

    by_kind = {row["agent_kind"]: row for row in dash["by_agent_kind"]}
    assert by_kind["root"]["input_tokens"] == 140
    assert by_kind["root"]["cache_hit_rate"] == round(40 / 140, 4)
    assert by_kind["janitor"]["input_tokens"] == 20
    assert by_kind["daily"]["input_tokens"] == 30

    by_model = {row["model_key"]: row for row in dash["by_model"]}
    assert by_model["m1"]["input_tokens"] == 140
    assert by_model["m1"]["cache_hit_rate"] == round(40 / 140, 4)
    assert "(unknown)" in by_model

    # Display ignores event workspace_name; no registry → storage id as-is.
    by_ws = {row["workspace_id"]: row for row in dash["by_workspace"]}
    assert by_ws["w1"]["workspace_name"] == "w1"
    assert by_ws["w2"]["workspace_name"] == "w2"

    filtered = summarize_usage_dashboard(
        days=1,
        workspace_id="w2",
        event_paths=[ws1 / "events.jsonl", ws2 / "events.jsonl"],
    )
    assert filtered["totals"]["input_tokens"] == 30
    assert filtered["totals"]["llm_turns"] == 1
    assert filtered["totals"]["cache_hit_rate"] == round(7 / 30, 4)
    assert len(filtered["recent"]) == 1
    assert filtered["recent"][0]["agent_kind"] == "daily"


def test_summarize_usage_dashboard_uses_registry_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(tz=UTC).isoformat()
    ws_id = "nx-c34a1b2c3d"
    usage_dir = tmp_path / "workspaces" / ws_id / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "kind": "llm_turn",
                "ts": now,
                "agent_kind": "root",
                "workspace_id": ws_id,
                "workspace_name": ws_id,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.runtime.usage_query._load_registry_workspace_aliases",
        lambda _home: {ws_id: "nx"},
    )

    dash = summarize_usage_dashboard(
        days=7,
        coara_home=tmp_path,
        event_paths=[usage_dir / "events.jsonl"],
    )
    assert dash["by_workspace"][0]["workspace_name"] == "nx"
    assert dash["workspaces"][0]["name"] == "nx"
