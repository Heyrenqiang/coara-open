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


def test_list_pricing_entries_override_only_when_no_providers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 无 providers 配置（config_manager._config None）时，覆盖文件里已有的模型也能列出
    save_override(tmp_path, "p", "m", {"input": 1.0})
    entries = list_pricing_entries(tmp_path)
    by_key = {e["model_key"]: e for e in entries}
    assert "p/m" in by_key
    assert by_key["p/m"]["source"] == "override"
    assert by_key["p/m"]["pricing"]["input"] == 1.0
