"""Tests for pricing override store and load_pricing_map merge."""

from __future__ import annotations

import pytest

from src.runtime import usage_pricing as pricing_mod
from src.runtime.pricing_store import (
    list_pricing_entries,
    load_overrides,
    save_override,
)


def test_save_and_load_override(tmp_path) -> None:
    save_override(tmp_path, "deepseek", "deepseek-chat", {"input": 2.0, "output": 8.0, "cache_hit": 0.5})
    ov = load_overrides(tmp_path)
    assert ov["deepseek/deepseek-chat"] == {"input": 2.0, "output": 8.0, "cache_hit": 0.5}
    assert ov["deepseek/deepseek-chat"]["input"] == 2.0


def test_save_override_untouched_fields_preserved(tmp_path) -> None:
    save_override(tmp_path, "a", "m", {"input": 1.0, "cache_hit": 0.25})
    save_override(tmp_path, "a", "m", {"output": 2.0})
    # 二次覆盖不覆盖的字段保留（此处 output 是新加的，input 应仍保留）
    assert load_overrides(tmp_path)["a/m"] == {"input": 1.0, "cache_hit": 0.25, "output": 2.0}


def test_save_override_delete(tmp_path) -> None:
    save_override(tmp_path, "a", "m", {"input": 1.0})
    assert load_overrides(tmp_path)
    # 全空 → 删除该项
    save_override(tmp_path, "a", "m", {})
    assert "a/m" not in load_overrides(tmp_path)
    save_override(tmp_path, "a", "m", {"input": 1.0})
    # 显式 None → 删除该项
    save_override(tmp_path, "a", "m", None)
    assert load_overrides(tmp_path) == {}


def test_load_pricing_map_merges_overrides(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = {
        "deepseek/deepseek-chat": pricing_mod.ModelPricing(input_per_m=1.0, output_per_m=4.0),
    }
    monkeypatch.setattr(pricing_mod, "load_base_pricing_map", lambda *a, **k: dict(base))
    monkeypatch.setattr(pricing_mod, "_default_coara_home", lambda: str(tmp_path))
    save_override(tmp_path, "deepseek", "deepseek-chat", {"output": 6.0})  # 覆盖 output
    save_override(tmp_path, "custom", "c1", {"input": 3.0})  # 覆盖策略新增模型
    merged = pricing_mod.load_pricing_map(tmp_path)
    assert merged["deepseek/deepseek-chat"].output_per_m == 6.0
    assert merged["deepseek/deepseek-chat"].input_per_m == 1.0  # 未覆盖保留默认
    assert merged["custom/c1"].input_per_m == 3.0  # 新增纳入
    assert merged["custom/c1"].output_per_m == 0.0


def test_list_pricing_entries_factory_only(tmp_path) -> None:
    save_override(tmp_path, "deepseek", "deepseek-flash", {"input": 9.0})
    save_override(tmp_path, "agnes", "agnes-2.5-flash", {"input": 1.0})
    entries = list_pricing_entries(tmp_path)
    by_key = {e["model_key"]: e for e in entries}
    providers = {e["provider"] for e in entries}
    assert providers == {"deepseek", "kimi", "zhipu", "minimax"}
    assert "agnes/agnes-2.5-flash" not in by_key
    assert by_key["deepseek/deepseek-flash"]["source"] == "override"
    assert by_key["deepseek/deepseek-flash"]["pricing"]["input"] == 9.0
    assert by_key["deepseek/deepseek-flash"]["pricing"]["output"] == 4.0
    assert by_key["zhipu/glm-5.3-flash"]["pricing"] == {"input": 0.8, "cache_hit": 0.23, "output": 2.8}
    assert by_key["minimax/MiniMax-M2.7-highspeed"]["pricing"]["output"] == 16.8
    assert "kimi/kimi-for-coding" not in by_key
