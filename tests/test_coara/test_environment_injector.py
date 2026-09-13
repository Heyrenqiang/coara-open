"""Tests for first-turn environment context seed (via context modules)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.coara.injections.environment_injector import (
    ENV_CONTEXT_PREFIX,
    WS_PROTOCOL_PREFIX,
    build_environment_seed_messages,
    git_status_snapshot,
)
from src.utils.win_proc import no_window_creationflags


def test_build_environment_seed_omits_git_by_default(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True, creationflags=no_window_creationflags()
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        creationflags=no_window_creationflags(),
    )

    messages = build_environment_seed_messages(tmp_path)
    assert len(messages) >= 2
    env = next(m for m in messages if ENV_CONTEXT_PREFIX in m.content)
    content = env.content
    assert "<系统消息>" in content
    assert "Shell：" not in content
    assert "Git：" not in content
    assert "Coara ID" not in content
    assert "江西赣州信丰" in content
    assert str(tmp_path) in content
    # ws overview is a separate message
    assert any(WS_PROTOCOL_PREFIX in m.content for m in messages)
    assert WS_PROTOCOL_PREFIX not in content


def test_build_environment_seed_includes_git_for_coder(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True, creationflags=no_window_creationflags()
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        creationflags=no_window_creationflags(),
    )

    messages = build_environment_seed_messages(tmp_path, include_git=True)
    env = next(m for m in messages if ENV_CONTEXT_PREFIX in m.content)
    content = env.content
    assert "Git：" in content
    assert "##" in content or "main" in content.lower() or "master" in content.lower()


def test_git_status_snapshot_not_a_repo(tmp_path: Path) -> None:
    assert git_status_snapshot(tmp_path) == "不是 git 仓库"


def test_git_status_snapshot_truncates_many_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    many_lines = "## main\n" + "\n".join(f" M file{i}.txt" for i in range(20))
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        return subprocess.CompletedProcess(cmd, 0, many_lines, "")

    monkeypatch.setattr("src.coara.injections.environment_injector.subprocess.run", fake_run)
    snapshot = git_status_snapshot(tmp_path)
    assert "已截断" in snapshot
    assert snapshot.count("\n") <= 13
    assert calls["n"] == 2
