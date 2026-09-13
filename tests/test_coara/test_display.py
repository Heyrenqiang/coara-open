"""Tests for CLI/Matrix tool call label formatting."""

from __future__ import annotations

from src.coara.display import format_tool_call_label


def test_format_tool_call_label() -> None:
    glob_full = format_tool_call_label(
        "glob",
        {"pattern": "**/参赛*", "path": "D:\\code_ws\\nx"},
        max_len=80,
    )
    assert "参赛" in glob_full and "D:\\code_ws\\nx" in glob_full and "glob(" in glob_full
    assert format_tool_call_label("glob", {"pattern": "*.py"}, max_len=30) == "glob(*.py)"

    long_path = "D:\\code_ws\\v8\\src\\cli\\activity_live.py"
    grep_full = format_tool_call_label(
        "grep",
        {"pattern": "chat_chunk|_emit_chat_chunk", "path": long_path},
        max_len=None,
    )
    assert grep_full == f"grep(chat_chunk|_emit_chat_chunk @ {long_path})"
    grep_short = format_tool_call_label(
        "grep",
        {"pattern": "chat_chunk|_emit_chat_chunk", "path": long_path},
        max_len=40,
    )
    assert "..." in grep_short
    assert len(grep_short) < len(grep_full)

    grep = format_tool_call_label(
        "grep",
        {"pattern": r"def \w+", "path": "D:\\code_ws\\nx\\src"},
        max_len=80,
    )
    assert "def " in grep and "D:\\code_ws\\nx\\src" in grep and "grep(" in grep

    shell = format_tool_call_label("shell", {"command": "cd D:\\code_ws\\nx && dir"}, max_len=40)
    assert "dir" in shell and "shell(" in shell

    wf = format_tool_call_label(
        "delegate",
        {"action": "run", "draft_id": "daily-report", "definition": "very long workflow text..."},
        max_len=40,
    )
    assert "run" in wf and "daily-report" in wf
    assert "very long workflow text" not in wf and "delegate" in wf
    assert format_tool_call_label("delegate", {"action": "result"}, max_len=30) == "delegate result"

    assert format_tool_call_label("ws", {"action": "list"}, max_len=30) == "ws(list)"
    assert format_tool_call_label("ws", {"action": "switch", "name": "nx"}, max_len=30) == "ws(switch nx)"

    # action comes first in typical LLM args; must still show the skill name.
    assert (
        format_tool_call_label(
            "skill",
            {"action": "activate", "name": "research"},
            max_len=None,
        )
        == "skill(activate research)"
    )
    assert format_tool_call_label("skill", {"action": "list"}, max_len=30) == "skill(list)"

    assert (
        format_tool_call_label(
            "tool",
            {"action": "activate", "name": "embody"},
            max_len=None,
        )
        == "tool(装载 embody)"
    )
    assert (
        format_tool_call_label(
            "tool",
            {"action": "search", "query": "截图"},
            max_len=None,
        )
        == "tool(搜索 截图)"
    )
    assert format_tool_call_label("tool", {"action": "search"}, max_len=30) == "tool(搜索)"

    assert (
        format_tool_call_label(
            "delegate",
            {
                "subagent_type": "explore",
                "description": "核对 ws switch/rename 与过期会话缺口",
                "prompt": "long prompt omitted from label",
            },
            max_len=None,
        )
        == "delegate explore: 核对 ws switch/rename 与过期会话缺口"
    )
    assert (
        format_tool_call_label(
            "delegate",
            {
                "subagent_type": "janitor",
                "description": "巡检",
                "background": True,
                "workspace": "shop",
            },
            max_len=None,
        )
        == "delegate janitor bg @shop: 巡检"
    )
    assert format_tool_call_label("delegate", {"subagent_type": "coaras"}, max_len=30) == "delegate coaras"

    # 工作流模式：spawn 带 flow 参数 → 显示组网信息（flow/node_id/依赖/流向）
    assert (
        format_tool_call_label(
            "delegate",
            {
                "subagent_type": "coaras",
                "description": "拆解为三个方面",
                "flow": "test10",
                "node_id": "n2",
                "depends_on": ["n1"],
                "routes_to": ["n3", "n4", "n5"],
            },
            max_len=None,
        )
        == "delegate coaras: 拆解为三个方面 [flow=test10/n2 ←n1 →n3,n4,n5]"
    )
    # 入口节点无依赖
    assert (
        format_tool_call_label(
            "delegate",
            {
                "subagent_type": "coaras",
                "description": "入口：定义主题",
                "flow": "test10",
                "node_id": "n1",
                "routes_to": ["n2"],
            },
            max_len=None,
        )
        == "delegate coaras: 入口：定义主题 [flow=test10/n1 →n2]"
    )
    # 图级 action：status/export 显示 action + flow
    assert (
        format_tool_call_label(
            "delegate",
            {"action": "status", "flow": "test10"},
            max_len=None,
        )
        == "delegate status flow=test10"
    )
    assert (
        format_tool_call_label(
            "delegate",
            {"action": "wait", "flow": "test10"},
            max_len=None,
        )
        == "delegate wait flow=test10"
    )
