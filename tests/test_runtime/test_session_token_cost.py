"""Session cumulative cost for LLM log detail (devtools)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.usage_pricing import ModelPricing
from src.runtime.usage_query import enrich_usage_with_cost, summarize_session_token_cost


def test_summarize_session_token_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = tmp_path / "usage" / "events.jsonl"
    events.parent.mkdir(parents=True)
    rows = [
        {
            "kind": "llm_turn",
            "session_id": "sess-1",
            "provider": "demo",
            "model": "m1",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cache_read_input_tokens": 800,
            },
        },
        {
            "kind": "llm_turn",
            "session_id": "sess-1",
            "provider": "demo",
            "model": "m1",
            "usage": {
                "input_tokens": 2000,
                "output_tokens": 50,
                "cache_read_input_tokens": 1500,
            },
        },
        {
            "kind": "llm_turn",
            "session_id": "other",
            "provider": "demo",
            "model": "m1",
            "usage": {"input_tokens": 9999, "output_tokens": 9},
        },
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "src.runtime.usage_pricing.load_pricing_map",
        lambda *_a, **_k: {
            "demo/m1": ModelPricing(input_per_m=10.0, cache_hit_per_m=1.0, output_per_m=20.0),
        },
    )
    monkeypatch.setattr(
        "src.runtime.usage_query.resolve_usage_events_path",
        lambda _ws, *, coara_home=None: events,
    )

    out = summarize_session_token_cost("sess-1", workspace_dir=tmp_path)
    assert out is not None
    assert out["llm_turns"] == 2
    # Anthropic 口径：input + cache_read 计入有效输入
    assert out["input_tokens"] == 5300
    assert out["output_tokens"] == 150
    assert out["cache_read_tokens"] == 2300
    assert out["cost_state"] == "priced"
    assert out["cost_total"] > 0
    assert out["cost_total_display"].startswith("¥")

    assert summarize_session_token_cost("missing", workspace_dir=tmp_path) is None


def test_enrich_usage_with_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.runtime.usage_pricing.load_pricing_map",
        lambda *_a, **_k: {
            "demo/m1": ModelPricing(input_per_m=10.0, cache_hit_per_m=1.0, output_per_m=20.0),
        },
    )
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cache_read_input_tokens": 500,
    }
    enriched = enrich_usage_with_cost(usage, provider="demo", model="m1")
    assert enriched is usage
    assert usage["cost_state"] == "priced"
    assert usage["cost_total_display"].startswith("¥")
    # total_prompt=1500 → miss=(1500-500)*10/1e6, hit=500*1/1e6, out=100*20/1e6
    assert abs(usage["cost_total"] - (0.01 + 0.0005 + 0.002)) < 1e-9


def test_enrich_llm_log_detail_sums_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.runtime.usage_query import enrich_llm_log_detail

    monkeypatch.setattr(
        "src.runtime.usage_pricing.load_pricing_map",
        lambda *_a, **_k: {
            "demo/m1": ModelPricing(input_per_m=10.0, cache_hit_per_m=0.0, output_per_m=20.0),
        },
    )
    monkeypatch.setattr(
        "src.runtime.usage_query._collect_session_llm_usages",
        lambda *_a, **_k: [],
    )
    payload = {
        "provider_name": "demo",
        "model": "m1",
        "session_id": "s1",
        "conversation": [
            {
                "round": 1,
                "user": "hi",
                "iterations": [
                    {
                        "assistant": {"content": "a"},
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 100,
                            "total_tokens": 1100,
                            "cache_read_input_tokens": 800,
                        },
                    },
                    {
                        "assistant": {"content": "b"},
                        "usage": {"prompt_tokens": 2000, "completion_tokens": 50, "total_tokens": 2050},
                    },
                ],
            },
            {
                "round": 2,
                "user": "again",
                "iterations": [],
            },
        ],
        "response": {
            "content": "final",
            "usage": {"prompt_tokens": 3000, "completion_tokens": 10, "total_tokens": 3010},
        },
    }
    enrich_llm_log_detail(payload)
    r1 = payload["conversation"][0]
    r2 = payload["conversation"][1]
    assert r1["llm_turns"] == 2
    assert r2["llm_turns"] == 1  # 当前 response 并入最后一回合
    assert r1["cost_state"] == "priced"
    assert r2["cost_state"] == "priced"
    # 有效输入口径：首轮 (1000+800) + 2000；末回合 3000
    assert r1["input_tokens"] == 3800
    assert r1["cache_read_tokens"] == 800
    assert r2["input_tokens"] == 3000
    assert payload["session_cost"]["input_tokens"] == 6800
    assert payload["session_cost"]["cache_read_tokens"] == 800
    # miss = (effective - cache_read) * input；本测 cache_hit 价为 0
    # iter1 miss=1000 out=100; iter2 miss=2000 out=50; resp miss=3000 out=10
    expected = (1000 * 10 + 100 * 20 + 2000 * 10 + 50 * 20 + 3000 * 10 + 10 * 20) / 1_000_000
    assert abs(payload["session_cost"]["cost_total"] - expected) < 1e-9
    assert payload["session_cost"]["llm_turns"] == 3
    assert payload["session_cost"]["rounds"] == 2
    assert payload["conversation"][0]["iterations"][0]["usage"]["cost_total_display"].startswith("¥")


def test_enrich_llm_log_detail_unpriced_is_per_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """早先未配价不得粘滞污染后续零费用回合的 cost_state。"""
    from src.runtime.usage_query import enrich_llm_log_detail

    calls = {"n": 0}

    def fake_load(*_a, **_k):
        calls["n"] += 1
        # 第一次调用无价，后续有价——用侧路：按 model 不匹配模拟
        return {}

    monkeypatch.setattr("src.runtime.usage_pricing.load_pricing_map", fake_load)
    monkeypatch.setattr(
        "src.runtime.usage_query._collect_session_llm_usages",
        lambda *_a, **_k: [],
    )
    payload = {
        "provider_name": "demo",
        "model": "m1",
        "conversation": [
            {
                "round": 1,
                "iterations": [
                    {"usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101}},
                ],
            },
            {
                "round": 2,
                "iterations": [
                    {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
                ],
            },
        ],
    }
    enrich_llm_log_detail(payload)
    assert payload["conversation"][0]["cost_state"] == "unpriced"
    # 第二回合无有效输入、费用 0 → zero（不得因第一回合 unpriced 被标成 unpriced）
    assert payload["conversation"][1]["cost_state"] == "zero"
