"""FlowRoot（WebUI 工作流页面专属主体）与协调器实例隔离测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import make_test_coara


def test_flow_coordinator_is_instance_scoped(tmp_path: Path) -> None:
    """协调器实例级：同实例幂等，不同实例互不可见（主体隔离前提）。"""
    a = make_test_coara(tmp_path)
    b = make_test_coara(tmp_path)

    assert a.flow_coordinator is a.flow_coordinator  # 惰性单创建
    assert a.flow_coordinator is not b.flow_coordinator
    assert a.flow_coordinator is not b.flow_coordinator


def test_orchestrator_resident_registration(tmp_path: Path) -> None:
    """orchestrator 常驻注册：实例级 should_defer=False 后直接进 LLM 工具面。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorTool

    coara = make_test_coara(tmp_path)
    tool = OrchestratorTool(parent_coara=coara)
    tool.should_defer = False
    coara.register_tool(tool, replace=True)

    tm = coara._tool_manager
    assert "orchestrator" in tm.tools
    assert tm.tools["orchestrator"].should_defer is False
    llm_names = [d["name"] for d in tm.get_tool_definitions_for_llm(coara.identity.is_owner_context)]
    assert "orchestrator" in llm_names


@pytest.mark.asyncio
async def test_create_flow_root_basics(tmp_path: Path) -> None:
    """FlowRoot 构造：独立 persona/主体标记、orchestrator 常驻、独立协调器。"""
    from src.coara.flow_root import create_flow_root

    fg = make_test_coara(tmp_path)
    fake_root = SimpleNamespace(
        foreground_coara=fg,
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
        workspace_manager=None,
    )

    flow_root = await create_flow_root(fake_root)

    assert flow_root.identity.name == "flow-root"
    assert flow_root.flow_subject == "flow"
    assert flow_root.workspace_dir == fg.workspace_dir
    assert flow_root.session_id != fg.session_id  # 独立会话主体
    # orchestrator 常驻（不在挂起池）
    tm = flow_root._tool_manager
    assert "orchestrator" in tm.tools
    llm_names = [d["name"] for d in tm.get_tool_definitions_for_llm(flow_root.identity.is_owner_context)]
    assert "orchestrator" in llm_names
    # 图协调器与主会话前台实例隔离
    assert flow_root.flow_coordinator is not fg.flow_coordinator
    # 提示词来自 prompts/agents/flow-root.md
    prompt = flow_root.identity.persona.system_prompt_template or ""
    assert "FlowRoot" in prompt or "工作流" in prompt


@pytest.mark.asyncio
async def test_flow_trace_sink_accepts_trace_event(tmp_path: Path) -> None:
    """回归：trace sink 必须接受单参数 TraceEvent（TraceEmitter 已单参数化，
    旧五参数签名会导致 CLI 端对话 TypeError）。"""
    from src.coara.flow_root import create_flow_root
    from src.core.events import TraceEvent

    fg = make_test_coara(tmp_path)
    published: list[TraceEvent] = []
    fake_root = SimpleNamespace(
        foreground_coara=fg,
        event_bus=SimpleNamespace(publish=lambda ev: published.append(ev)),
        workspace_manager=None,
    )
    flow_root = await create_flow_root(fake_root)

    flow_root._emit_trace(
        "user_message",
        "hello",
        payload={"content": "hello", "source": "web-flow", "turn_id": "t1"},
    )

    assert len(published) == 1
    ev = published[0]
    assert isinstance(ev, TraceEvent)
    assert ev.event_type == "user_message"
    assert ev.message == "hello"
    assert ev.payload["subject"] == "flow"
    assert ev.payload["origin_scope"] == "flow_loop"
