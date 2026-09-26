"""Tests for path-glob rules injection."""

from __future__ import annotations

from pathlib import Path

from src.coara.rules_glob import (
    discover_rules,
    match_rules_for_paths,
    maybe_build_rules_messages,
    path_matches_glob,
)
from src.core.types import MessageRole


def test_path_matches_glob_python_files() -> None:
    assert path_matches_glob("src/coara/base.py", "src/**/*.py")
    assert not path_matches_glob("docs/README.md", "src/**/*.py")


def test_discover_and_match_rules(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".coara" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "python.mdc").write_text(
        '---\nglobs:\n  - "src/**/*.py"\npriority: 10\n---\nUse type hints.\n',
        encoding="utf-8",
    )
    rules = discover_rules(tmp_path)
    assert len(rules) == 1
    matched = match_rules_for_paths(rules, ["src/coara/base.py"])
    assert matched[0].name == "python"


def test_maybe_build_rules_messages_ignore_at_mention(tmp_path: Path, monkeypatch) -> None:
    """@ 文件引用体系已退役（08-31）：@ 不再解析路径，仅 extra_paths 触发规则。"""
    rules_dir = tmp_path / ".coara" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "android.mdc").write_text(
        '---\nglobs:\n  - "android-app/**/*.kt"\n---\nPrefer Compose.\n',
        encoding="utf-8",
    )
    kt_path = tmp_path / "android-app" / "app" / "src"
    kt_path.mkdir(parents=True)
    kt_file = kt_path / "Main.kt"
    kt_file.write_text("fun main() {}", encoding="utf-8")
    monkeypatch.setattr(
        "src.coara.rules_glob.get_rules_glob_config",
        lambda: type("Cfg", (), {"enabled": True, "max_total_chars": 8000, "max_rule_chars": 2000})(),
    )
    # @ 不再解析：纯 @ 路径不注入
    messages = maybe_build_rules_messages(f"please check @{kt_file.relative_to(tmp_path).as_posix()}", tmp_path)
    assert len(messages) == 0
    # 显式 extra_paths 仍触发
    messages = maybe_build_rules_messages(
        "please check",
        tmp_path,
        extra_paths=[kt_file.relative_to(tmp_path).as_posix()],
    )
    assert len(messages) == 1
    assert messages[0].role == MessageRole.USER
    assert "Compose" in str(messages[0].content)
    assert "android" in str(messages[0].content).lower()


def test_maybe_build_rules_messages_match_extra_paths(tmp_path: Path, monkeypatch) -> None:
    rules_dir = tmp_path / ".coara" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "python.mdc").write_text(
        '---\nglobs:\n  - "src/**/*.py"\n---\nUse type hints.\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.coara.rules_glob.get_rules_glob_config",
        lambda: type("Cfg", (), {"enabled": True, "max_total_chars": 8000, "max_rule_chars": 2000})(),
    )
    messages = maybe_build_rules_messages(
        "please continue",
        tmp_path,
        extra_paths=["src/coara/base.py"],
    )
    assert len(messages) == 1
    assert "type hints" in str(messages[0].content)
