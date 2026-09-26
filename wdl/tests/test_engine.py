"""parse → validate → 两节点串联执行 → 断言结果与事件序列（fake executor，无网络）。"""

from __future__ import annotations

import asyncio

import pytest

from wdl.core import parse_graph, validate_graph
from wdl.engine import WdlEngine

TWO_NODE_WDL = """\
name: two-node
nodes:
  a:
    task: 起草一段文字
  b:
    task: 润色
    input: "{{steps.a.text}}"
edges:
  - from: a
    to: b
"""


def _fake_executor_factory(graph_llm, node_llms, node_tools=None, tool_settings=None):
    async def fake(*, node_id: str, prompt: str, task: str) -> str:
        if node_id == "a":
            return "草稿-A"
        assert "草稿-A" in prompt  # 模板 {{steps.a.text}} 已渲染进 b 的 prompt
        return "润色-B"

    return fake


@pytest.mark.asyncio
async def test_two_node_chain_run(tmp_path) -> None:
    graph = parse_graph(TWO_NODE_WDL)
    assert [i for i in validate_graph(graph) if i.level == "error"] == []

    engine = WdlEngine(db_path=tmp_path / "instances.db", node_executor_factory=_fake_executor_factory)
    events: list[tuple[str, dict]] = []
    done = asyncio.Event()

    async def on_event(event_type: str, payload: dict) -> None:
        events.append((event_type, payload))
        if event_type in ("workflow_completed", "workflow_failed"):
            done.set()

    engine.on_event(on_event)
    await engine.start()
    try:
        task_id = await engine.start_workflow(TWO_NODE_WDL)
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        await engine.shutdown()

    types = [t for t, _ in events]
    assert "workflow_started" in types
    assert "workflow_completed" in types
    assert "workflow_failed" not in types
    assert types.index("node_started") < types.index("node_completed")
    completed_nodes = [p["step_id"] for t, p in events if t == "node_completed"]
    assert completed_nodes == ["a", "b"]

    final = next(p for t, p in events if t == "workflow_completed")
    assert final["task_id"] == task_id
    ctx = final["context"]["context"]
    assert ctx["a"]["text"] == "草稿-A"
    assert ctx["b"]["text"] == "润色-B"
