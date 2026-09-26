"""Toolbar session usage chrome invalidation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cli.spinner import BackgroundSpinner


def test_session_cache_hit_ratio_invalidates_on_session_change(monkeypatch: pytest.MonkeyPatch):
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(session_id="sess-a", workspace_dir="D:/ws"),
        session_id="sess-a",
    )
    spinner.bind_root(root)
    spinner._session_hit_ratio_cache = 0.94
    spinner._session_hit_ratio_at = 999999.0
    spinner._session_hit_ratio_bound_id = "sess-old"

    monkeypatch.setattr("src.runtime.usage_query.resolve_usage_path_for_root", lambda _r: Path("/tmp/u.jsonl"))
    monkeypatch.setattr("src.runtime.usage_query.session_cache_hit_ratio", lambda _p, _s: 0.0)

    ratio = spinner._session_cache_hit_ratio(root.foreground_coara, None)

    assert spinner._session_hit_ratio_bound_id == "sess-a"
    assert ratio is None


def test_invalidate_session_usage_chrome_clears_bound_session():
    spinner = BackgroundSpinner()
    spinner._session_hit_ratio_cache = 0.5
    spinner._session_hit_ratio_bound_id = "sess-x"
    fut = MagicMock(done=lambda: False)
    spinner._session_hit_ratio_future = fut

    spinner.invalidate_session_usage_chrome()

    assert spinner._session_hit_ratio_cache is None
    assert spinner._session_hit_ratio_bound_id == ""
    assert spinner._session_hit_ratio_future is None
    fut.cancel.assert_called_once()
