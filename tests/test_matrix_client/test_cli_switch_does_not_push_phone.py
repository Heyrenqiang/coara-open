"""CLI 切空间不得经 workspace_switched 推手机。

workspace_switched 只在 cli view 切换时发布；matrix 变更走命令路径 /
view_changed。bot 与 matrix_runner 不得再订该 topic；事件桥接函数
``push_workspace_switch_payloads`` 已删除。
"""

from __future__ import annotations

import ast
from pathlib import Path

import src.coara.mobile_sync as mobile_sync

_REPO = Path(__file__).resolve().parents[2]


def _subscribe_topic_literals(path: Path) -> set[str]:
    """AST：``*.subscribe(..., topic=<str常量>)`` 的 topic 集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "subscribe"):
            continue
        for kw in node.keywords:
            if kw.arg != "topic":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                found.add(kw.value.value)
    return found


def test_push_workspace_switch_payloads_removed() -> None:
    assert not hasattr(mobile_sync, "push_workspace_switch_payloads")


def test_matrix_modules_do_not_subscribe_workspace_switched() -> None:
    bot_topics = _subscribe_topic_literals(_REPO / "src/matrix_client/bot.py")
    runner_topics = _subscribe_topic_literals(_REPO / "src/cli/matrix_runner.py")
    assert "workspace_switched" not in bot_topics
    assert "workspace_switched" not in runner_topics
