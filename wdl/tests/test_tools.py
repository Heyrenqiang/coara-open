"""wdl.tools 内置工具集：路径约束、写入/编辑、shell 超时、白名单构建。"""

from __future__ import annotations

import pytest

from wdl.tools import ToolError, build_tools, run_tool


async def _run(specs, name: str, args: dict) -> tuple[str, bool]:
    spec = next(s for s in specs if s.name == name)
    return await run_tool(spec, args)


async def test_write_and_read_file_roundtrip(tmp_path) -> None:
    specs = build_tools(tmp_path)
    text, ok = await _run(specs, "write_file", {"path": "sub/out.txt", "content": "你好"})
    assert ok and "已写入" in text
    assert (tmp_path / "sub" / "out.txt").read_text(encoding="utf-8") == "你好"
    text, ok = await _run(specs, "read_file", {"path": "sub/out.txt"})
    assert ok and text == "你好"


async def test_path_escape_rejected(tmp_path) -> None:
    specs = build_tools(tmp_path)
    for tool, args in [
        ("write_file", {"path": "../evil.txt", "content": "x"}),
        ("write_file", {"path": str(tmp_path.parent / "abs.txt"), "content": "x"}),
        ("read_file", {"path": "../../etc/passwd"}),
        ("edit_file", {"path": "../evil.txt", "old_string": "a", "new_string": "b"}),
    ]:
        text, ok = await _run(specs, tool, args)
        assert not ok, f"{tool} 应拒绝越界路径"
        assert "越出工作目录" in text
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_edit_file_unique_match(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("foo bar foo", encoding="utf-8")
    specs = build_tools(tmp_path)
    text, ok = await _run(specs, "edit_file", {"path": "a.txt", "old_string": "foo", "new_string": "baz"})
    assert not ok and "2 处" in text
    text, ok = await _run(specs, "edit_file", {"path": "a.txt", "old_string": "bar foo", "new_string": "baz"})
    assert ok
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "foo baz"


async def test_shell_runs_in_workdir(tmp_path) -> None:
    specs = build_tools(tmp_path)
    text, ok = await _run(specs, "shell", {"command": "echo hello-wdl"})
    assert ok and "[exit=0]" in text and "hello-wdl" in text


async def test_shell_timeout(tmp_path) -> None:
    specs = build_tools(tmp_path, shell_timeout=0.5)
    text, ok = await _run(specs, "shell", {"command": 'python -c "import time; time.sleep(30)"'})
    assert not ok and "超时" in text


async def test_list_files_and_grep(tmp_path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("gamma\n", encoding="utf-8")
    specs = build_tools(tmp_path)
    text, ok = await _run(specs, "list_files", {"path": ".", "recursive": True})
    assert ok and "d/x.py" in text and "y.py" in text
    text, ok = await _run(specs, "grep", {"pattern": "bet"})
    assert ok and "d/x.py:2: beta" in text
    text, ok = await _run(specs, "grep", {"pattern": "["})
    assert not ok and "正则非法" in text


async def test_build_tools_whitelist_and_unknown(tmp_path) -> None:
    specs = build_tools(tmp_path, names=["read_file"])
    assert [s.name for s in specs] == ["read_file"]
    with pytest.raises(ToolError, match="未知工具"):
        build_tools(tmp_path, names=["nope"])
