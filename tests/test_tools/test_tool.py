"""Tests for the deferred-tool gateway (search/activate) and whitelist filtering."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.coara.tool_manager import ToolManager
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.tools.builtin.integration.tool import (
    ToolGatewayInvocation,
    _expand_search_keywords,
    _score_deferred_tool,
)


class _FakeDeferredTool(BaseTool):
    name = "capture_fullscreen"
    description = "截取所有显示器的完整屏幕，保存为 PNG，返回文件路径。"
    category = "system"
    kind = ToolKind.OTHER
    should_defer = True
    parameters_schema = {"type": "object", "properties": {}}

    def create_invocation(self, params):
        class _Inv(ToolInvocation):
            async def execute(self, signal=None):
                return ToolResult.success("ok")

        return _Inv(params)


def test_deferred_tools_respect_agent_whitelist() -> None:
    manager = ToolManager()
    manager.set_whitelist({"read", "tool"})
    manager.register_tool(_FakeDeferredTool())

    summaries = manager.get_deferred_tool_summaries(is_owner_ctx=True)
    assert not any(s["name"] == "capture_fullscreen" for s in summaries)

    # 白名单放行后可见
    manager.set_whitelist({"read", "tool", "capture_fullscreen"})
    summaries = manager.get_deferred_tool_summaries(is_owner_ctx=True)
    assert any(s["name"] == "capture_fullscreen" for s in summaries)


def test_revealed_tools_hidden_from_tool_search_pool() -> None:
    manager = ToolManager()
    manager.register_tool(_FakeDeferredTool())
    manager.reveal_tool("capture_fullscreen")

    pool = manager.get_deferred_tool_summaries(is_owner_ctx=True)

    assert not any(s["name"] == "capture_fullscreen" for s in pool)


class _FakeBuiltinDeferredTool(BaseTool):
    name = "local_search"
    description = "本地记录检索：联合搜索记忆与收藏。"
    category = "records"
    kind = ToolKind.OTHER
    should_defer = True
    parameters_schema = {"type": "object", "properties": {}}

    def create_invocation(self, params):
        class _Inv(ToolInvocation):
            async def execute(self, signal=None):
                return ToolResult.success("ok")

        return _Inv(params)


def test_inject_deferred_tool_list_renders_names_and_descriptions() -> None:
    from src.coara.base import CoaraBase

    manager = ToolManager()
    manager.register_tool(_FakeDeferredTool())
    manager.register_tool(_FakeBuiltinDeferredTool())

    prompt = CoaraBase._inject_deferred_tool_list("头\n${COARA_DEFERRED_TOOL_LIST}\n尾", manager, True)

    # 挂起工具：名字 + 描述
    assert "`local_search` — 本地记录检索" in prompt
    assert "`capture_fullscreen` — 截取所有显示器" in prompt

    # 静态清单：工具激活（reveal）后仍保留在清单中，前缀不随激活变化
    manager.reveal_tool("local_search")
    prompt = CoaraBase._inject_deferred_tool_list("${COARA_DEFERRED_TOOL_LIST}", manager, True)
    assert "`local_search` — 本地记录检索" in prompt
    assert "capture_fullscreen" in prompt

    # 全部激活后清单依旧完整（不显示空态）
    manager.reveal_tool("capture_fullscreen")
    prompt = CoaraBase._inject_deferred_tool_list("${COARA_DEFERRED_TOOL_LIST}", manager, True)
    assert "`local_search` — 本地记录检索" in prompt
    assert "capture_fullscreen" in prompt
    assert "无挂起" not in prompt


def test_screenshot_query_matches_capture_tools() -> None:
    keywords = _expand_search_keywords("截图")
    assert "capture" in keywords
    score = _score_deferred_tool(
        "capture_fullscreen",
        "截取所有显示器的完整屏幕，保存为 PNG，返回文件路径。",
        keywords,
    )
    assert score > 0


@pytest.mark.asyncio
async def test_search_returns_candidates_without_revealing() -> None:
    coara = MagicMock()
    coara.identity.is_owner_context = True
    coara._invalidate_prompt_cache = MagicMock()

    manager = ToolManager()
    manager.set_whitelist({"tool", "capture_fullscreen"})
    manager.register_tool(_FakeDeferredTool())
    coara._tool_manager = manager

    inv = ToolGatewayInvocation({"action": "search", "query": "截图"}, coara)
    result = await inv.execute()

    assert not result.is_error
    assert "capture_fullscreen" in result.content
    assert "capture_fullscreen" not in manager._revealed


@pytest.mark.asyncio
async def test_search_without_query_returns_full_catalog() -> None:
    coara = MagicMock()
    coara.identity.is_owner_context = True
    coara._invalidate_prompt_cache = MagicMock()

    manager = ToolManager()
    manager.register_tool(_FakeDeferredTool())
    coara._tool_manager = manager

    result = await ToolGatewayInvocation({"action": "search"}, coara).execute()

    assert not result.is_error
    assert "capture_fullscreen" in result.content
    assert "截取所有显示器" in result.content  # 全量目录带描述
    assert "capture_fullscreen" not in manager._revealed  # 只看不用


@pytest.mark.asyncio
async def test_activate_reveals_deferred_tool_by_name() -> None:
    coara = MagicMock()
    coara.identity.is_owner_context = True
    coara._invalidate_prompt_cache = MagicMock()

    manager = ToolManager()
    manager.set_whitelist({"tool", "capture_fullscreen"})
    manager.register_tool(_FakeDeferredTool())
    coara._tool_manager = manager

    inv = ToolGatewayInvocation({"action": "activate", "name": "capture_fullscreen"}, coara)
    result = await inv.execute()

    assert not result.is_error
    assert "已装载" in result.content
    assert "capture_fullscreen" in manager._revealed

    # 重复 activate 幂等
    again = await ToolGatewayInvocation({"action": "activate", "name": "capture_fullscreen"}, coara).execute()
    assert "已在本会话装载过" in again.content

    # 未知名称报错并提示 search
    missing = await ToolGatewayInvocation({"action": "activate", "name": "nonexistent"}, coara).execute()
    assert missing.is_error
    assert "search" in missing.content
