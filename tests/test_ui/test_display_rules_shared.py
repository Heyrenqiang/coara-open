"""三端共享的显示口径常量：判据行为矩阵 + 与协议真源一致。

真源：`docs/protocol/coara-envelopes.json` 的 `display_rules` 段
生成物：`src/core/display_rules.py`（Python）· `lib/displayRules.generated.ts`（web）·
`coara/DisplayRules.kt`（Android）

背景：过去「哪些工具行不显示」在 web / 手机 / CLI 各写一遍，靠人盯着同步；
2026-09-25 下沉到协议真源。本文件把判据语义钉死——改真源后这里必须仍然全绿。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.display_rules import (
    FOLD_GROUP_DEFAULT_OPEN,
    FOLD_GROUP_ORDER,
    FOLD_GROUP_TITLES,
    HIDDEN_TOOL_LINE_RULES,
    is_hidden_tool_line,
)

_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "protocol" / "coara-envelopes.json"


def test_spec_and_generated_agree() -> None:
    """生成物必须与协议真源逐字一致（真源改了没重新生成 → 这里红）。"""
    spec = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))["display_rules"]
    assert list(FOLD_GROUP_ORDER) == [g["id"] for g in spec["subagent_fold_groups"]]
    assert {g["id"]: g["title"] for g in spec["subagent_fold_groups"]} == FOLD_GROUP_TITLES
    assert {g["id"]: bool(g.get("default_open", True)) for g in spec["subagent_fold_groups"]} == (
        FOLD_GROUP_DEFAULT_OPEN
    )
    assert [r["id"] for r in HIDDEN_TOOL_LINE_RULES] == [r["id"] for r in spec["hidden_tool_lines"]]


def test_hidden_tool_line_matrix() -> None:
    """noise 行的判据矩阵（web / 手机 / CLI 共用同一语义）。"""
    cases = [
        # delegate wait 隐；delegate 的实事（spawn / resume / message / stop）不隐
        ("delegate", "delegate wait", True),
        ("", "delegate wait", True),
        ("delegate", "delegate spawn: 任务", False),
        ("", "delegate spawn: 任务", False),
        ("delegate", "delegate resume: x", False),
        ("delegate", "delegate message: x", False),
        ("delegate", "delegate stop: x", False),
        # send_file：tool_name 直判；缺字段的老帧看 label
        ("send_file", "send_file(a.png)", True),
        ("", "send_file(a.png)", True),
        # todo park 隐；todo 的其它动作不隐
        ("todo", "todo - park 卡点", True),
        ("", "todo(park 卡点)", True),
        ("todo", "todo - update 计划", False),
        # plan：只有 action=plan 隐；enter / exit 是模式开关，要上屏
        ("plan", "plan - plan", True),
        ("", "plan - plan", True),
        ("", "plan(plan x)", True),
        ("plan", "plan - enter", False),
        ("", "plan - enter", False),
        ("plan", "plan - exit", False),
        ("", "plan", False),
        # 无关工具行照常显示
        ("shell", "shell - ls", False),
        ("", "shell - ls", False),
        ("", "", False),
    ]
    for name, label, want in cases:
        assert is_hidden_tool_line(name, label) is want, (name, label)


def test_cli_subset_only() -> None:
    """CLI 折叠块借 send_file / todo park / plan 三条（delegate 的显示由它自己的摘要行机制决定）。"""
    rules = ("send_file", "todo_park", "plan")
    assert is_hidden_tool_line("", "todo - park x", rule_ids=rules) is True
    assert is_hidden_tool_line("", "send_file(x)", rule_ids=rules) is True
    assert is_hidden_tool_line("", "plan - plan", rule_ids=rules) is True
    # enter / exit 是模式开关，要上屏
    assert is_hidden_tool_line("", "plan - enter", rule_ids=rules) is False
    assert is_hidden_tool_line("", "delegate wait", rule_ids=rules) is False
