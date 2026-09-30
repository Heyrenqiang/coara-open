"""CLI 命令回执渲染：压缩成功须画分割线（不可被空正文早退吃掉）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.cli.commands import _render_result
from src.coara.commands.types import CommandResult


def test_render_compressed_draws_rule_even_when_output_empty(monkeypatch) -> None:
    """/compact 成功回执 output='' + data.compressed=True → console.rule('已压缩')。"""
    import src.cli.commands as cmd

    rules: list[tuple] = []
    prints: list[tuple] = []
    fake = MagicMock()
    fake.rule = lambda *a, **k: rules.append((a, k))
    fake.print = lambda *a, **k: prints.append((a, k))
    monkeypatch.setattr(cmd, "console", fake)

    _render_result(CommandResult(output="", data={"compressed": True, "info": {}}))

    assert len(rules) == 1
    assert rules[0][0][0] == "已压缩"
    assert prints == []


def test_render_empty_silent_command_prints_nothing(monkeypatch) -> None:
    """无正文且无 compressed：静默（如只带 open_url 的跳转命令）。"""
    import src.cli.commands as cmd

    rules: list[tuple] = []
    fake = MagicMock()
    fake.rule = lambda *a, **k: rules.append((a, k))
    fake.print = MagicMock()
    monkeypatch.setattr(cmd, "console", fake)

    _render_result(CommandResult(output="", data={"open_url": "http://x"}))

    assert rules == []
    fake.print.assert_not_called()
