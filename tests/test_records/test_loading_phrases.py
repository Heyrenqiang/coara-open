"""Tests for the Thinking-line witty phrase rotation."""

from __future__ import annotations

import pytest

from src.records import loading_phrases
from src.records.loading_phrases import (
    _FALLBACK_RESIDENT,
    _ROTATE_SECONDS,
    QUOTE_POOL,
    TIPS,
    WITTY_POOL,
    current_phrase,
    reshuffle,
)


def test_pools_loaded_from_library() -> None:
    assert len(WITTY_POOL) > 200
    assert len(QUOTE_POOL) >= 40
    # 名言池以原文加省略号呈现为主（统一风格：省略号收尾、不带出处括注）
    assert all(p.endswith("…") for p in QUOTE_POOL)
    assert all("（" not in p for p in QUOTE_POOL)


def test_batch_without_custom_is_sampled_not_full_pool() -> None:
    """无定制：常驻抽固定袋 + 小技巧；不是整池灌进一轮。"""
    batch = reshuffle()
    assert len(batch) == _FALLBACK_RESIDENT + len(TIPS)
    assert set(TIPS) <= set(batch)
    resident = [p for p in batch if p not in TIPS]
    assert len(resident) == _FALLBACK_RESIDENT
    assert set(resident) <= set(WITTY_POOL) | set(QUOTE_POOL)
    assert len(set(batch)) == len(batch)


def test_reshuffle_changes_batch() -> None:
    before = loading_phrases.current_batch()
    # 连抽几次，至少一次与之前不同（池子够大，随机撞车概率可忽略）
    assert any(reshuffle() != list(before) for _ in range(3))


def test_current_phrase_deterministic_per_window() -> None:
    reshuffle()
    base = float(_ROTATE_SECONDS * 1000)  # 对齐窗口边界，避免取整跨界
    assert current_phrase(base, offset=0) == current_phrase(base + _ROTATE_SECONDS - 0.1, offset=0)


def test_current_phrase_rotates_across_windows() -> None:
    batch = reshuffle()
    base = 10_000.0
    seen = {current_phrase(base + i * _ROTATE_SECONDS, offset=0) for i in range(len(batch))}
    # 转满一圈必须覆盖整批
    assert seen == set(batch)


def test_custom_pool_one_to_one_with_resident(tmp_path, monkeypatch) -> None:
    """有定制 N 条：袋内定制 N + 常驻抽 N，上屏 1:1（不含小技巧稀释）。"""
    import time as _time

    custom = tmp_path / "loading_phrases_custom.json"
    today = _time.strftime("%Y-%m-%d")
    phrases = ["昨天那个bug修好了吗", "又见面了老熟人", "第三条定制梗"]
    custom.write_text(
        '{"date": "' + today + '", "phrases": ' + __import__("json").dumps(phrases, ensure_ascii=False) + "}",
        encoding="utf-8",
    )
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", custom)
    batch = reshuffle()
    customs = [p for p in batch if p in phrases]
    residents = [p for p in batch if p not in phrases]
    assert len(customs) == len(phrases)
    assert len(residents) == len(phrases)
    assert len(batch) == 2 * len(phrases)
    assert set(residents) <= set(WITTY_POOL) | set(QUOTE_POOL)
    # 不掺小技巧，避免打破 1:1
    assert not any(p in TIPS for p in batch)


def test_custom_pool_all_included(tmp_path, monkeypatch) -> None:
    """定制词全部进袋，常驻按同数抽样。"""
    import json as _json
    import time as _time

    custom = tmp_path / "loading_phrases_custom.json"
    today = _time.strftime("%Y-%m-%d")
    phrases = [f"定制梗{i}" for i in range(5)]
    custom.write_text(
        '{"date": "' + today + '", "phrases": ' + _json.dumps(phrases, ensure_ascii=False) + "}",
        encoding="utf-8",
    )
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", custom)
    batch = reshuffle()
    customs = [p for p in batch if p.startswith("定制梗")]
    assert len(customs) == len(phrases)
    assert len(batch) == 2 * len(phrases)


def test_custom_pool_stale_date_falls_back(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "loading_phrases_custom.json"
    custom.write_text('{"date": "2020-01-01", "phrases": ["过期的旧梗"]}', encoding="utf-8")
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", custom)
    batch = reshuffle()
    assert "过期的旧梗" not in batch
    assert any(p in WITTY_POOL or p in QUOTE_POOL for p in batch)


def test_custom_pool_no_date_falls_back(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "loading_phrases_custom.json"
    custom.write_text('{"phrases": ["没写日期的词"]}', encoding="utf-8")
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", custom)
    batch = reshuffle()
    assert "没写日期的词" not in batch
    assert any(p in WITTY_POOL or p in QUOTE_POOL for p in batch)


def test_custom_pool_missing_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", tmp_path / "nope.json")
    batch = reshuffle()
    assert any(p in WITTY_POOL or p in QUOTE_POOL for p in batch)


def test_custom_pool_corrupt_falls_back(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "loading_phrases_custom.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", bad)
    batch = reshuffle()
    assert any(p in WITTY_POOL or p in QUOTE_POOL for p in batch)


@pytest.fixture(autouse=True)
def _reset_custom_path(monkeypatch):
    monkeypatch.setattr(loading_phrases, "_custom_phrases_path", None)
