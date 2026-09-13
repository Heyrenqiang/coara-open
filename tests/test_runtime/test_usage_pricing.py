"""Tests for LLM pricing and per-turn cost computation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.core.events import TraceEvent
from src.runtime.usage_collector import UsageCollector
from src.runtime.usage_pricing import ModelPricing, compute_turn_cost, lookup_pricing
from src.runtime.usage_query import summarize_usage_detail
from src.runtime.usage_store import UsageStore, UsageStoreConfig


def test_compute_turn_cost_anthropic_style() -> None:
    # Anthropic/MiniMax: input 不含缓存，cache_read/cache_creation 单独字段
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 5,
    }
    pricing = ModelPricing(input_per_m=1.0, cache_hit_per_m=0.02, output_per_m=2.0)
    cost = compute_turn_cost(usage, pricing)
    # total=145；缓存创建并入非命中：miss=(145-40)=105×input；hit=40×0.02；out=10×2
    assert cost["cost_miss"] == pytest.approx(105 / 1_000_000)
    assert cost["cost_hit"] == pytest.approx(40 * 0.02 / 1_000_000)
    assert cost["cost_out"] == pytest.approx(10 * 2 / 1_000_000)
    assert cost["cost_total"] == pytest.approx(0.0001258)


def test_compute_turn_cost_openai_style() -> None:
    # OpenAI/MiMo: prompt_tokens 已含缓存，cached_tokens 单独
    usage = {"input_tokens": 145, "output_tokens": 10, "cached_tokens": 40}
    pricing = ModelPricing(input_per_m=1.0, cache_hit_per_m=0.02, output_per_m=2.0)
    cost = compute_turn_cost(usage, pricing)
    assert cost["cost_miss"] == pytest.approx(105 / 1_000_000)
    assert cost["cost_hit"] == pytest.approx(40 * 0.02 / 1_000_000)
    assert cost["cost_out"] == pytest.approx(10 * 2 / 1_000_000)
    assert cost["cost_total"] == pytest.approx(0.0001258)


def test_compute_turn_cost_no_pricing_returns_zero() -> None:
    usage = {"input_tokens": 100, "output_tokens": 10}
    assert compute_turn_cost(usage, None) == {
        "cost_miss": 0.0,
        "cost_hit": 0.0,
        "cost_out": 0.0,
        "cost_total": 0.0,
    }


def test_collector_records_turn_id(tmp_path: Path) -> None:
    default_path = tmp_path / "fg" / "events.jsonl"
    store = UsageStore(default_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)
    collector.handle_event(
        TraceEvent(
            coara_id="c1",
            coara_name="root",
            event_type="llm_turn_complete",
            message="llm",
            payload={
                "session_id": "s1",
                "turn_id": "turn-9",
                "model": "m",
                "provider": "p",
                "has_tool_calls": False,
                "agent_kind": "root",
                "llm_output": {"usage": {"input_tokens": 10, "output_tokens": 2}},
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)
    payload = json.loads(default_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload["turn_id"] == "turn-9"


def test_summarize_usage_detail_groups_session_turn_iteration(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    usage_dir = tmp_path / "workspaces" / "w1" / "usage"
    usage_dir.mkdir(parents=True)
    rows = [
        {
            "kind": "llm_turn",
            "ts": now.isoformat(),
            "agent_kind": "root",
            "workspace_id": "w1",
            "session_id": "s1",
            "turn_id": "t1",
            "iteration": 1,
            "model": "m1",
            "provider": "p1",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
        {
            "kind": "llm_turn",
            "ts": now.isoformat(),
            "agent_kind": "root",
            "workspace_id": "w1",
            "session_id": "s1",
            "turn_id": "t1",
            "iteration": 2,
            "model": "m1",
            "provider": "p1",
            "usage": {"input_tokens": 200, "output_tokens": 20},
        },
        {
            # 历史记录无 turn_id：并入空 turn 组
            "kind": "llm_turn",
            "ts": now.isoformat(),
            "agent_kind": "janitor",
            "workspace_id": "w1",
            "session_id": "s1",
            "iteration": 1,
            "model": "m2",
            "provider": "p2",
            "usage": {"input_tokens": 50, "output_tokens": 5},
        },
    ]
    (usage_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    detail = summarize_usage_detail(
        days=7,
        coara_home=tmp_path,
        event_paths=[usage_dir / "events.jsonl"],
    )
    assert detail["limit"] == 3
    assert len(detail["sessions"]) == 1
    session = detail["sessions"][0]
    assert session["session_id"] == "s1"
    assert session["llm_turns"] == 3
    # 两个大轮：t1（2 小轮）+ 空（1 小轮）
    assert len(session["turns"]) == 2
    by_turn = {turn["turn_id"]: turn for turn in session["turns"]}
    assert len(by_turn["t1"]["iterations"]) == 2
    assert len(by_turn[""]["iterations"]) == 1
    # 无价格表 → 费用全 0，结构仍在
    assert session["cost_total"] == 0.0
    assert by_turn["t1"]["iterations"][0]["cost_miss"] == 0.0


def test_summarize_usage_detail_filters_workspace_and_days(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    old = (now - timedelta(days=2)).isoformat()
    usage_dir = tmp_path / "workspaces" / "w1" / "usage"
    usage_dir.mkdir(parents=True)
    (usage_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "kind": "llm_turn",
                "ts": old,
                "agent_kind": "root",
                "workspace_id": "w1",
                "session_id": "s1",
                "turn_id": "t1",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # days=1 应排除 2 天前的记录
    assert summarize_usage_detail(days=1, event_paths=[usage_dir / "events.jsonl"])["sessions"] == []
    detail = summarize_usage_detail(days=7, event_paths=[usage_dir / "events.jsonl"])
    assert len(detail["sessions"]) == 1


def test_detail_and_recent_flag_pricing_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 provider 价格时行上带 pricing_configured=False，展示层可区分「未配价」与真 0 费。"""
    now = datetime.now(tz=UTC)
    usage_dir = tmp_path / "workspaces" / "w1" / "usage"
    usage_dir.mkdir(parents=True)
    rows = [
        {
            "kind": "llm_turn",
            "ts": now.isoformat(),
            "agent_kind": "root",
            "workspace_id": "w1",
            "session_id": "s1",
            "turn_id": "t1",
            "model": "m-unpriced",
            "provider": "p",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
        {
            "kind": "llm_turn",
            "ts": now.isoformat(),
            "agent_kind": "root",
            "workspace_id": "w1",
            "session_id": "s1",
            "turn_id": "t1",
            "model": "m-priced",
            "provider": "p",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    ]
    (usage_dir / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    priced_map = {
        "p/m-priced": ModelPricing(input_per_m=1.0, cache_hit_per_m=0.5, output_per_m=2.0),
    }
    import src.runtime.usage_pricing as pricing_mod
    import src.runtime.usage_query as usage_query_mod

    monkeypatch.setattr(pricing_mod, "load_pricing_map", lambda *_a, **_k: priced_map)

    detail = summarize_usage_detail(days=7, event_paths=[usage_dir / "events.jsonl"])
    iterations = detail["sessions"][0]["turns"][0]["iterations"]
    flags = {row["model"]: row["pricing_configured"] for row in iterations}
    assert flags["m-priced"] is True
    assert flags["m-unpriced"] is False
    # 未配价行费用为 0（口径不变），配价行非 0
    costs = {row["model"]: row["cost_total"] for row in iterations}
    assert costs["m-unpriced"] == 0.0
    assert costs["m-priced"] > 0.0

    dash = usage_query_mod.summarize_usage_dashboard(days=7, event_paths=[usage_dir / "events.jsonl"])
    recent_flags = {row["model"]: row["pricing_configured"] for row in dash["recent"]}
    assert recent_flags["m-priced"] is True
    assert recent_flags["m-unpriced"] is False


def test_lookup_pricing_tolerates_renamed_models() -> None:
    """历史模型名回退到同厂价格：改名、变体后缀、上下文窗后缀都算同一个模型。

    精确匹配失败就按 0 计，等于整段历史账目消失（v8 一个月曾有 3.5 万轮这样被记成 0）。
    找不到时返回 None（不猜），由调用方计一条 unpriced。
    """
    pm = {
        "deepseek/deepseek-flash": ModelPricing(1.0, 0.02, 4.0),
        "kimi/k3": ModelPricing(20.0, 2.0, 100.0),
    }
    assert lookup_pricing(pm, "kimi", "k3").input_per_m == 20.0
    assert lookup_pricing(pm, "kimi", "k3[1m]").output_per_m == 100.0
    assert lookup_pricing(pm, "kimi", "k3-256k").cache_hit_per_m == 2.0
    assert lookup_pricing(pm, "deepseek", "deepseek-v4-flash").input_per_m == 1.0
    assert lookup_pricing(pm, "deepseek", "deepseek-v4-pro").output_per_m == 4.0
    assert lookup_pricing(pm, "agnes", "agnes-2.5-flash") is None
    assert lookup_pricing({}, "kimi", "k3") is None
    assert lookup_pricing(pm, "", "") is None
