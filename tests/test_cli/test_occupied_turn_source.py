"""他端运行指示的归属源（CLI 侧）。

有回合在跑 + 当前段归属不是 cli/cli-attached。RootShim 合成
root.occupied_turn_source；spinner 只消费该入口。
"""

from __future__ import annotations

from typing import Any

from src.cli.spinner import BackgroundSpinner


class _AccessorRoot:
    """RootShim 形态：运行源由 root 统一给出。"""

    def __init__(self, source: str) -> None:
        self._source = source

    def occupied_turn_source(self) -> str:
        return self._source


class _LegacyRoot:
    """无 accessor：回退 has_active_turn + _active_turn_source。"""

    def __init__(self, *, active: bool, source: str) -> None:
        self._active_turn = active
        self.foreground_coara: Any = type("_Fg", (), {"_active_turn_source": source})()

    def has_active_turn(self) -> bool:
        return self._active_turn


def _spinner(root: Any) -> BackgroundSpinner:
    spinner = BackgroundSpinner()
    spinner._root = root
    return spinner


def test_prefers_root_occupied_source() -> None:
    assert _spinner(_AccessorRoot("web"))._occupied_turn_source() == "web"


def test_accessor_empty_means_no_occupancy() -> None:
    assert _spinner(_AccessorRoot(""))._occupied_turn_source() == ""


def test_fallback_reads_local_turn_source() -> None:
    """无 accessor：本端回合被他端接管时归属切端，仍要亮。"""
    assert _spinner(_LegacyRoot(active=True, source="web"))._occupied_turn_source() == "web"


def test_fallback_hides_own_end_source() -> None:
    assert _spinner(_LegacyRoot(active=True, source="cli-attached"))._occupied_turn_source() == ""


def test_fallback_idle_is_empty() -> None:
    assert _spinner(_LegacyRoot(active=False, source="web"))._occupied_turn_source() == ""


def test_occupied_mark_renders_in_status_slot(monkeypatch: Any) -> None:
    """他端运行：标记画在输入分割线上方的状态槽（与本端转圈同槽）。"""
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    spinner = _spinner(_AccessorRoot("web"))
    monkeypatch.setattr(spinner, "_background_ws_segments", lambda: (FormattedText(), 0))
    monkeypatch.setattr(spinner, "_foreground_cli_spinner_active", lambda: False)
    monkeypatch.setattr(spinner, "_background_tasks_snapshot", lambda: SimpleNamespace(count=0))

    text = "".join(chunk for _, chunk in spinner())

    assert "Web 端运行中" in text


def test_idle_has_no_occupied_mark(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    spinner = _spinner(_AccessorRoot(""))
    monkeypatch.setattr(spinner, "_background_ws_segments", lambda: (FormattedText(), 0))
    monkeypatch.setattr(spinner, "_foreground_cli_spinner_active", lambda: False)
    monkeypatch.setattr(spinner, "_background_tasks_snapshot", lambda: SimpleNamespace(count=0))

    text = "".join(chunk for _, chunk in spinner())

    assert "端运行中" not in text
