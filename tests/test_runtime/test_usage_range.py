"""Tests for summarize_usage_range (calendar heatmap endpoint aggregation)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.runtime.usage_query import summarize_usage_range


def _write_events(usage_dir: Path, rows: list[dict]) -> Path:
    usage_dir.mkdir(parents=True, exist_ok=True)
    path = usage_dir / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def test_summarize_usage_range_groups_and_cross_breakdowns(tmp_path: Path) -> None:
    today = _today()
    now = datetime.now(tz=UTC).isoformat()
    events = _write_events(
        tmp_path / "workspaces" / "w1" / "usage",
        [
            {
                "kind": "llm_turn",
                "ts": now,
                "agent_kind": "root",
                "workspace_id": "w1",
                "provider": "p",
                "model": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40},
            },
            {
                "kind": "llm_turn",
                "ts": now,
                "agent_kind": "janitor",
                "workspace_id": "w1",
                "provider": "p",
                "model": "m2",
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
        ],
    )

    dash = summarize_usage_range(date_from=today, date_to=today, event_paths=[events])
    assert dash["totals"]["llm_turns"] == 2
    # effective prompt: 100+40=140（root，Anthropic 口径）+ 20（janitor）
    assert dash["totals"]["input_tokens"] == 160

    by_day = {row["day"]: row for row in dash["by_day"]}
    assert by_day[today]["input_tokens"] == 160

    by_kind = {row["agent_kind"]: row for row in dash["by_agent_kind"]}
    assert by_kind["root"]["input_tokens"] == 140
    assert by_kind["janitor"]["input_tokens"] == 20

    ws_agents = dash["workspace_agents"]["w1"]
    assert {row["agent_kind"]: row["input_tokens"] for row in ws_agents} == {"root": 140, "janitor": 20}

    ws_models = dash["workspace_models"]["w1"]
    assert {row["model_key"]: row["input_tokens"] for row in ws_models} == {"p/m1": 140, "p/m2": 20}

    agent_models = dash["agent_models"]["root"]
    assert agent_models[0]["model_key"] == "p/m1"
    assert agent_models[0]["input_tokens"] == 140

    assert dash["workspaces"] == [{"id": "w1", "name": "w1"}]


def test_summarize_usage_range_filters_outside_window(tmp_path: Path) -> None:
    today = _today()
    old = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat()
    events = _write_events(
        tmp_path / "workspaces" / "w1" / "usage",
        [
            {
                "kind": "llm_turn",
                "ts": old,
                "agent_kind": "root",
                "workspace_id": "w1",
                "usage": {"input_tokens": 999, "output_tokens": 1},
            },
            {
                "kind": "llm_turn",
                # 无时间戳的历史记录无法落入日区间，跳过
                "agent_kind": "root",
                "workspace_id": "w1",
                "usage": {"input_tokens": 555, "output_tokens": 1},
            },
        ],
    )
    dash = summarize_usage_range(date_from=today, date_to=today, event_paths=[events])
    assert dash["totals"]["llm_turns"] == 0
    assert dash["by_day"] == []


def test_summarize_usage_range_invalid_range_returns_empty(tmp_path: Path) -> None:
    today = _today()
    dash = summarize_usage_range(date_from=today, date_to="2020-01-01", event_paths=[])
    assert dash["error"] == "invalid_range"
    assert dash["totals"]["llm_turns"] == 0


def test_summarize_usage_range_reads_rotation_archives(tmp_path: Path) -> None:
    """轮转归档必须参与统计。

    写侧写满阈值后把活动文件另存为 ``events.<时间戳>.jsonl``（多代保留）；读侧若只认
    ``events.jsonl``，一整段历史费用会凭空消失（2026-09-12 真实发生过：v8 空间当日
    费用从 ¥68.66 掉到 ¥6.18）。历史遗留的 ``events.jsonl.1`` 同样要算。
    """
    usage_dir = tmp_path / "workspaces" / "w1" / "usage"
    today = _today()
    now = datetime.now(tz=UTC).isoformat()
    row = {
        "kind": "llm_turn",
        "ts": now,
        "agent_kind": "root",
        "workspace_id": "w1",
        "provider": "p",
        "model": "m1",
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }
    _write_events(usage_dir, [row])
    (usage_dir / "events.20260912T120000Z.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (usage_dir / "events.jsonl.1").write_text(json.dumps(row) + "\n", encoding="utf-8")

    dash = summarize_usage_range(date_from=today, date_to=today, coara_home=tmp_path)
    assert dash["totals"]["llm_turns"] == 3


def test_summarize_usage_range_counts_legacy_agent_kind_alias(tmp_path: Path) -> None:
    """改名前的 agent_kind 不得整段掉出统计：「参谋」是 aide 更名前的名字。"""
    today = _today()
    now = datetime.now(tz=UTC).isoformat()
    events = _write_events(
        tmp_path / "workspaces" / "w1" / "usage",
        [
            {
                "kind": "llm_turn",
                "ts": now,
                "agent_kind": "参谋",
                "workspace_id": "w1",
                "provider": "p",
                "model": "m1",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            }
        ],
    )

    dash = summarize_usage_range(date_from=today, date_to=today, event_paths=[events])
    assert dash["totals"]["llm_turns"] == 1
    assert [row["agent_kind"] for row in dash["by_agent_kind"]] == ["aide"]
