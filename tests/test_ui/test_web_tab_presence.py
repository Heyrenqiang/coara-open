"""Tests for browser tab presence（跨内核重启记住「刚才有 Web 标签」）。"""

from __future__ import annotations

from pathlib import Path

from src.ui import web_tab_presence as presence


def test_mark_seen_and_read_roundtrip(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    presence.mark_tab_seen(workspace, now=1000.0)
    loaded = presence.read_presence(workspace)
    assert loaded.last_seen_at == 1000.0
    assert presence.presence_file(workspace).is_file()


def test_is_tab_fresh_within_ttl(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    presence.mark_tab_seen(workspace, now=1000.0)
    loaded = presence.read_presence(workspace)
    assert presence.is_tab_fresh(loaded, now=1000.0 + presence.FRESH_TTL_SECONDS - 1)
    assert not presence.is_tab_fresh(loaded, now=1000.0 + presence.FRESH_TTL_SECONDS + 1)


def test_bye_marks_absent_until_next_signal(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    presence.mark_tab_seen(workspace, now=1000.0)
    presence.mark_tab_left(workspace, now=1001.0)
    assert not presence.is_tab_fresh(presence.read_presence(workspace), now=1002.0)
    presence.mark_tab_seen(workspace, now=1003.0)
    assert presence.is_tab_fresh(presence.read_presence(workspace), now=1004.0)


def test_second_click_inside_window(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first, _ = presence.note_open_attempt(workspace, now=500.0)
    assert first is False
    second, _ = presence.note_open_attempt(
        workspace, now=500.0 + presence.SECOND_CLICK_WINDOW_SECONDS - 1
    )
    assert second is True
    third, _ = presence.note_open_attempt(
        workspace, now=500.0 + presence.SECOND_CLICK_WINDOW_SECONDS + 60
    )
    assert third is False


def test_decide_open_action_matrix() -> None:
    decide = presence.decide_open_action
    assert decide(has_active=True, fresh=False, second_click=False) == presence.ACTION_FOCUS_ACTIVE
    assert decide(has_active=True, fresh=True, second_click=True) == presence.ACTION_FOCUS_ACTIVE
    assert decide(has_active=False, fresh=True, second_click=False) == presence.ACTION_FOCUS_RECENT
    assert decide(has_active=False, fresh=True, second_click=True) == presence.ACTION_OPEN
    assert decide(has_active=False, fresh=False, second_click=False) == presence.ACTION_OPEN


def test_missing_or_broken_file_reads_as_zero(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert presence.read_presence(workspace) == presence.TabPresence()
    path = presence.presence_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert presence.read_presence(workspace).last_seen_at == 0.0


def test_presence_file_lives_under_workspace_home(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "coara-home"
    path = presence.presence_file(workspace, coara_home=home)
    assert home in path.parents
    assert path.name == "web_tab_presence.json"
    assert path.parent.name == "runtime"
