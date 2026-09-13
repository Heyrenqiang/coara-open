"""Tests for the ptc tool (Code Mode / PTC)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import src.tools.builtin.code_mode.ptc as ptc_mod
from src.tools.builtin.code_mode.ptc import PtcTool, build_ptc_description
from src.tools.builtin.file_io.read import ReadTool
from tests.helpers import make_test_coara


@pytest.fixture
def coara(tmp_path: Path):
    c = make_test_coara(tmp_path)
    c.register_tool(
        ReadTool(
            read_state_store=c._file_read_states,
            workspace_root=tmp_path,
            vfs_resolver=None,
            parent_coara=c,
        )
    )
    c.register_tool(PtcTool(workspace_root=tmp_path, parent_coara=c))
    return c


def _run(coara, code: str, description: str = "test program") -> ptc_mod.ToolResult:
    tool = coara._tool_manager.tools["ptc"]
    invocation = tool.create_invocation({"code": code, "description": description})
    return asyncio.run(invocation.execute())


def test_program_prints_and_returns(coara) -> None:
    result = _run(coara, 'print("hello")\nreturn {"sum": 3 + 4}')
    assert not result.is_error
    assert "hello" in result.content
    assert "7" in result.content


def test_precheck_blocks_import_before_execution(coara) -> None:
    """静态预检：import 在提交时即拦截，带行号，不执行任何子调用。"""
    code = "x = 1\nfrom pathlib import Path\nreturn x"
    result = _run(coara, code)
    assert result.is_error
    assert "静态预检" in result.content
    assert "第 2 行" in result.content
    assert "禁止 import" in result.content


def test_precheck_blocks_open_and_eval(coara) -> None:
    code = 'f = open("x.txt")\nreturn eval("1+1")'
    result = _run(coara, code)
    assert result.is_error
    assert "禁止 open()" in result.content
    assert "禁止 eval()" in result.content
    assert "tools.read" in result.content


def test_builtin_pathlib_datetime_usable(coara) -> None:
    """pathlib / datetime 已内置：程序直接用不再需要 import。"""
    result = _run(coara, 'p = pathlib.Path("a") / "b"\nreturn f"{p}|{datetime.datetime.now().year >= 2026}"')
    assert not result.is_error
    assert "a\\b" in result.content or "a/b" in result.content
    assert "True" in result.content


def test_program_calls_tool(coara, tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("line one\nline two\n", encoding="utf-8")
    code = f"content = await tools.read(path={str(target)!r})\nreturn len(content.splitlines())"
    result = _run(coara, code)
    assert not result.is_error
    assert "2" in result.content


def test_tool_failure_raises_tool_call_error(coara) -> None:
    code = (
        "try:\n"
        '    await tools.read(path="/definitely/not/here.txt")\n'
        "    return 'no error'\n"
        "except ToolCallError as e:\n"
        "    return 'caught: ' + e.toolName"
    )
    result = _run(coara, code)
    assert not result.is_error
    assert "caught: read" in result.content


def test_tool_not_visible_raises(coara) -> None:
    result = _run(coara, "return await tools.nonexistent()")
    assert result.is_error
    assert "不可见" in result.content or "ToolCallError" in result.content


def test_ptc_self_call_rejected(coara) -> None:
    """程序内不可再调 tools.ptc（防嵌套 PTC）"""
    result = _run(
        coara,
        'return await tools.ptc(code="return 1", description="nested")',
    )
    assert result.is_error
    assert "不可见" in result.content or "ToolCallError" in result.content


def test_program_exception_returns_error_with_logs(coara) -> None:
    code = 'print("before boom")\nraise ValueError("boom")'
    result = _run(coara, code)
    assert result.is_error
    assert "ValueError" in result.content
    assert "boom" in result.content
    assert "before boom" in result.content


def test_program_import_is_forbidden_with_hint(coara) -> None:
    """import 语句报清晰提示，而非暴露裁剪细节的 __import__ not found。"""
    result = _run(coara, "import os\nreturn 'never'")
    assert result.is_error
    assert "禁止 import" in result.content
    assert "__import__ not found" not in result.content


def test_program_timeout(coara, monkeypatch) -> None:
    monkeypatch.setattr(ptc_mod, "_MAX_PROGRAM_SECONDS", 1)
    result = _run(coara, "while True:\n    await asyncio.sleep(0.1)")
    assert result.is_error
    assert "超时" in result.content


def test_build_ptc_description() -> None:
    desc = build_ptc_description(["read", "grep", "ptc", "shell"])
    assert "await tools." in desc
    assert "何时用" in desc
    assert "grep、read、shell" in desc
    assert "ptc" not in desc.split("当前可调工具为 ", 1)[1].split("\n", 1)[0]


def test_description_property_is_live(coara) -> None:
    """description 现算可见清单，不依赖注册时一次性赋值"""
    tool = coara._tool_manager.tools["ptc"]
    desc = tool.description
    assert "read" in desc
    assert "当前可调工具为 " in desc
    assert tool.definition["description"] == desc
    schema = tool.parameters_schema["properties"]
    assert "函数体" in schema["code"]["description"]
    assert "展示" in schema["description"]["description"]
