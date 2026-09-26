"""磁盘工具包（dynamic tools）：加载、校验、执行、审批闸与注册去重。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import make_test_coara


def _write_package(
    root: Path,
    dirname: str = "my_tool",
    *,
    name: str = "my_tool",
    description: str = "测试工具",
    parameters: str = "",
    entry: str = "main.py",
    code: str = 'def run(**params):\n    return "ok"\n',
) -> Path:
    pkg = root / dirname
    pkg.mkdir(parents=True)
    header = f"---\nname: {name}\ndescription: {description}\n"
    if entry != "main.py":
        header += f"entry: {entry}\n"
    if parameters:
        header += parameters
    header += "---\n"
    (pkg / "TOOL.md").write_text(header, encoding="utf-8")
    (pkg / entry).write_text(code, encoding="utf-8")
    return pkg


class TestLoadToolPackage:
    def test_valid_package(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path)
        tool = load_tool_package(pkg)
        assert tool is not None
        assert tool.name == "my_tool"
        assert tool.should_defer is True
        assert tool.category == "dynamic"
        assert tool.parameters_schema["type"] == "object"

    def test_body_appended_to_description(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path)
        (pkg / "TOOL.md").write_text(
            "---\nname: my_tool\ndescription: 测试工具\n---\n\n补充说明正文\n", encoding="utf-8"
        )
        tool = load_tool_package(pkg)
        assert tool is not None and "补充说明正文" in tool.description

    @pytest.mark.parametrize(
        "name",
        ["", "1abc", "带中文", "has-dash"],
    )
    def test_invalid_name_rejected(self, tmp_path: Path, name: str) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, name=name)
        assert load_tool_package(pkg) is None

    def test_missing_entry_rejected(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path)
        (pkg / "main.py").unlink()
        assert load_tool_package(pkg) is None

    def test_missing_run_rejected(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code="X = 1\n")
        assert load_tool_package(pkg) is None

    def test_syntax_error_rejected(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code="def run(:\n")
        assert load_tool_package(pkg) is None

    def test_custom_entry(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, entry="run_main.py")
        tool = load_tool_package(pkg)
        assert tool is not None


class TestDynamicToolExecution:
    @pytest.mark.asyncio
    async def test_sync_run(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code='def run(text=""):\n    return f"got {text}"\n')
        tool = load_tool_package(pkg)
        assert tool is not None
        result = await tool.create_invocation({"text": "hi"}).execute()
        assert result.is_error is False
        assert result.content == "got hi"

    @pytest.mark.asyncio
    async def test_async_run(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code='async def run(**params):\n    return "async ok"\n')
        tool = load_tool_package(pkg)
        assert tool is not None
        result = await tool.create_invocation({}).execute()
        assert result.content == "async ok"

    @pytest.mark.asyncio
    async def test_dict_result_serialized(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code='def run(**p):\n    return {"a": 1}\n')
        tool = load_tool_package(pkg)
        assert tool is not None
        result = await tool.create_invocation({}).execute()
        assert '"a": 1' in result.content

    @pytest.mark.asyncio
    async def test_exception_becomes_error(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        pkg = _write_package(tmp_path, code='def run(**p):\n    raise RuntimeError("炸了")\n')
        tool = load_tool_package(pkg)
        assert tool is not None
        result = await tool.create_invocation({}).execute()
        assert result.is_error is True
        assert "炸了" in result.content

    def test_approval_required_by_default(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import load_tool_package

        tool = load_tool_package(_write_package(tmp_path))
        assert tool is not None
        # 配置里没进 auto_approve 名单 → 要审批（config 未加载也从严）
        assert tool.requires_approval({}) is True


class TestRegisterDynamicTools:
    def test_register_and_dedupe(self, tmp_path: Path) -> None:
        from src.tools.dynamic.loader import register_dynamic_tools

        ws = tmp_path / "ws"
        _write_package(ws / ".coara" / "tools")
        coara = make_test_coara(ws)
        # 清掉注册流程已加载的工具包，单测注册函数本身
        coara._tool_manager.tools.pop("my_tool", None)
        coara._tool_manager._deferred.discard("my_tool")

        assert register_dynamic_tools(coara) == 1
        assert "my_tool" in coara._tool_manager.tools
        assert "my_tool" in coara._tool_manager._deferred
        # 幂等：再扫不重复注册
        assert register_dynamic_tools(coara) == 0

    def test_name_collision_with_builtin_skipped(self, tmp_path: Path) -> None:
        from src.core.tool_base import BaseTool, ToolInvocation
        from src.tools.dynamic.loader import register_dynamic_tools

        class _DummyInvocation(ToolInvocation):
            def get_description(self) -> str:
                return "dummy"

            async def execute(self, signal=None):
                from src.core.tool_base import ToolResult

                return ToolResult.success("dummy")

        class _DummyReadTool(BaseTool):
            name = "read"
            description = "占位内置工具"
            display_name = "Read"

            def create_invocation(self, params):
                return _DummyInvocation(params)

        ws = tmp_path / "ws"
        _write_package(ws / ".coara" / "tools", dirname="read", name="read")
        coara = make_test_coara(ws)
        # 清掉注册流程可能已加载的同名包，再预置一个内置 read
        coara._tool_manager.tools.pop("read", None)
        coara._tool_manager._deferred.discard("read")
        existing = _DummyReadTool()
        coara.register_tool(existing)

        register_dynamic_tools(coara)
        # 内置 read 不被自建包顶掉
        assert coara._tool_manager.tools["read"] is existing
