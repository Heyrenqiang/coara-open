"""三端共享的显示口径常量与判据（聊天流噪音工具行 + 子智能体折叠组序）。

本文件由 scripts/dev/gen_envelopes.py 从 docs/protocol/coara-envelopes.json 生成，请勿手改。
真源版本：v2（2026-09-11）
"""

from __future__ import annotations

import re

#: 聊天流里不画的过程噪音行规则（真源顺序即判据顺序）。
HIDDEN_TOOL_LINE_RULES: tuple[dict[str, object], ...] = (
    {
        "id": "delegate_wait",
        "hide_when_tool_name_in": (),
        "label_regex": "^delegate\\s+wait\\b",
        "label_ok_when_tool_name_in": ('', 'delegate'),
    },
    {
        "id": "send_file",
        "hide_when_tool_name_in": ('send_file',),
        "label_regex": "^send_file\\b",
        "label_ok_when_tool_name_in": ('',),
    },
    {
        "id": "todo_park",
        "hide_when_tool_name_in": (),
        "label_regex": "^todo[\\s(-]+park\\b",
        "label_ok_when_tool_name_in": ('', 'todo'),
    },
    {
        "id": "plan",
        "hide_when_tool_name_in": (),
        "label_regex": "^plan[\\s(-]+plan\\b",
        "label_ok_when_tool_name_in": ('', 'plan'),
    },
)

#: 折叠区组序（子智能体产出：任务指令 → 过程 → 最终结果）。
FOLD_GROUP_ORDER: tuple[str, ...] = ('brief', 'process', 'result')

#: 折叠区组名（三端同一文案）。
FOLD_GROUP_TITLES: dict[str, str] = {
    "brief": "任务指令",
    "process": "过程",
    "result": "最终结果",
}

#: 折叠区默认展开态。
FOLD_GROUP_DEFAULT_OPEN: dict[str, bool] = {
    "brief": False,
    "process": True,
    "result": True,
}

#: 预编译规则：(id, 正则, 按 tool_name 直接判隐的集合, 按 label 判隐时允许的 tool_name 集合)。
_COMPILED_RULES = tuple(
    (
        str(rule["id"]),
        re.compile(str(rule["label_regex"])),
        frozenset(str(x) for x in rule["hide_when_tool_name_in"]),
        frozenset(str(x) for x in rule["label_ok_when_tool_name_in"]),
    )
    for rule in HIDDEN_TOOL_LINE_RULES
)


def is_hidden_tool_line(
    tool_name: str = "",
    label: str = "",
    *,
    rule_ids: tuple[str, ...] | None = None,
) -> bool:
    """这条工具行是否属于「不画」的过程噪音行（只看不画：数据与顺序都不动）。

    rule_ids 给定时只用其中几条——CLI 折叠块只借用 todo park / plan 两条，
    delegate 的显示由它自己的摘要行机制决定。
    """
    name = str(tool_name or "").strip()
    text = str(label or "").strip()
    for rule_id, regex, by_name, label_ok in _COMPILED_RULES:
        if rule_ids is not None and rule_id not in rule_ids:
            continue
        if name and name in by_name:
            return True
        if name in label_ok and regex.search(text):
            return True
    return False
