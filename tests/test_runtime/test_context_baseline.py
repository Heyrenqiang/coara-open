from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.context.window import LlmUsageSnapshot
from src.runtime.context_baseline import (
    inject_workspace_baseline,
    load_context_baseline,
    maybe_record_baseline_from_snapshot,
    save_context_baseline,
)


def test_save_and_load_keeps_shortest_same_hash(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))

    save_context_baseline(ws, model="m1", prompt_tokens=1200, payload_hash="abc", configured_home=home)
    save_context_baseline(ws, model="m1", prompt_tokens=5000, payload_hash="abc", configured_home=home)
    bas = load_context_baseline(ws, model="m1", configured_home=home)
    assert bas is not None
    assert bas.prompt_tokens == 1200

    # hash change replaces
    save_context_baseline(ws, model="m1", prompt_tokens=800, payload_hash="def", configured_home=home)
    bas2 = load_context_baseline(ws, model="m1", configured_home=home)
    assert bas2 is not None
    assert bas2.prompt_tokens == 800
    assert bas2.payload_hash == "def"


def test_inject_workspace_baseline_sets_estimated(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    save_context_baseline(ws, model="m1", prompt_tokens=2048, payload_hash="p1", configured_home=home)

    snap = LlmUsageSnapshot()
    coara = SimpleNamespace(workspace_dir=str(ws), model_name="m1", _llm_usage_snapshot=snap)
    assert inject_workspace_baseline(coara, configured_home=home) is True
    assert snap.estimated is True
    assert snap.has_reported_input is True
    from src.llm.usage import total_prompt_tokens

    assert total_prompt_tokens(snap.usage or {}) == 2048


def test_maybe_record_skips_estimated(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    snap = LlmUsageSnapshot()
    snap.inject_estimate(999, payload_hash="x")
    coara = SimpleNamespace(workspace_dir=str(ws), model_name="m1", _llm_usage_snapshot=snap)
    maybe_record_baseline_from_snapshot(coara, configured_home=home)
    assert load_context_baseline(ws, model="m1", configured_home=home) is None


def test_inject_estimate_and_clear_turn_keeps_cost() -> None:
    snap = LlmUsageSnapshot()
    snap.record_turn(
        usage={"input_tokens": 1000, "output_tokens": 10},
        history_len=2,
        system_len=100,
        tool_count=3,
        cost=0.42,
    )
    assert snap.cumulative_cost == 0.42
    snap.clear_turn_usage(keep_cumulative=True)
    assert snap.usage is None
    assert snap.cumulative_cost == 0.42
    snap.inject_estimate(800)
    assert snap.estimated is True
    assert snap.cumulative_cost == 0.42
    snap.record_turn(
        usage={"input_tokens": 900, "output_tokens": 5},
        history_len=3,
        system_len=100,
        tool_count=3,
        cost=0.1,
    )
    assert snap.estimated is False
    assert snap.cumulative_cost == 0.52


def test_format_status_metrics_estimated_tilde() -> None:
    from src.cli.status_bar import format_status_metrics

    ctx, _tail, _pct = format_status_metrics(
        used_tokens=28200,
        ctx_window=1048600,
        cache_hit_ratio=None,
        estimated=True,
    )
    assert ctx.strip().startswith("~")
    ctx2, _, _ = format_status_metrics(
        used_tokens=28200,
        ctx_window=1048600,
        cache_hit_ratio=None,
        estimated=False,
    )
    assert not ctx2.strip().startswith("~")
