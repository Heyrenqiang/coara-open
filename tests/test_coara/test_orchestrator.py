"""orchestrator 工具测试：flow 编排 spawn/run/status/save/load 与激活注册。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import make_test_coara


class _FakeFlowCoordinator:
    def __init__(self) -> None:
        self.spawned: list[tuple] = []
        self.run_calls: list[str] = []
        self.updated: list[tuple] = []
        self.edge_calls: list[tuple] = []
        self.removed: list[tuple] = []

    async def spawn_node(
        self,
        parent,
        *,
        flow,
        node_id,
        task,
        seed,
        depends_on,
        routes_to,
        routes_mode,
        subagent_type="coaras",
        auto_run=False,
        schedule=None,
    ) -> str:
        self.spawned.append(
            (flow, node_id, task, seed, depends_on, routes_to, routes_mode, subagent_type, auto_run, schedule)
        )
        return f"节点 {node_id} 已注册"

    def update_node(self, parent, *, flow, node_id, task=None, routes_mode=None) -> str:
        self.updated.append((flow, node_id, task, routes_mode))
        return f"节点 {node_id} 已更新"

    def add_flow_edge(self, parent, *, flow, frm, to, on="success") -> str:
        self.edge_calls.append(("add", flow, frm, to, on))
        return f"边已添加：{frm} → {to}"

    def remove_flow_edge(self, parent, *, flow, frm, to, on="success") -> str:
        self.edge_calls.append(("remove", flow, frm, to, on))
        return f"边已删除：{frm} → {to}"

    def remove_flow_node(self, parent, *, flow, node_id) -> str:
        self.removed.append((flow, node_id))
        return f"节点 {node_id} 已删除"

    def run(self, flow: str) -> str:
        self.run_calls.append(flow)
        return f"flow {flow} 已启动"

    def status(self, flow: str) -> str:
        return f"flow {flow} 状态摘要"

    def export_wdl(self, flow: str) -> str:
        return "wdl: demo"

    def load(self, data: dict) -> str:
        if "graph" in data:
            from src.workflow.core.serde import parse_graph

            return parse_graph(str(data["graph"])).name
        return str(data["name"])


@pytest.mark.asyncio
async def test_orchestrator_spawn_routes_to_coordinator(tmp_path: Path, monkeypatch) -> None:
    """编排 spawn：委托 flow_coordinator 织图（停车节点，不跑普通委派）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation(
        {
            "action": "spawn",
            "description": "节点A",
            "prompt": "做任务A",
            "subagent_type": "coaras",
            "flow": "demo",
            "node_id": "a",
            "input": "种子",
            "routes_to": ["b"],
        },
        parent,
    ).execute()

    assert result.is_error is False
    assert fake.spawned == [("demo", "a", "做任务A", "种子", [], ["b"], "all", "coaras", False, None)]


@pytest.mark.asyncio
async def test_orchestrator_spawn_defaults_to_parked(tmp_path: Path, monkeypatch) -> None:
    """编排 spawn 默认停车（auto_run=False）：先登记，等 run。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation(
        {
            "action": "spawn",
            "description": "节点A",
            "prompt": "做任务A",
            "flow": "demo",
            "node_id": "a",
        },
        parent,
    ).execute()

    assert result.is_error is False
    assert fake.spawned[0][8] is False  # auto_run 默认 False
    assert fake.run_calls == []


@pytest.mark.asyncio
async def test_orchestrator_spawn_requires_core_params() -> None:
    """spawn 缺 flow/node_id/prompt/description 时报参数错误。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    with pytest.raises(ValueError, match="flow"):
        OrchestratorToolInvocation({"action": "spawn", "node_id": "a", "description": "d", "prompt": "p"}, None)
    with pytest.raises(ValueError, match="node_id"):
        OrchestratorToolInvocation({"action": "spawn", "flow": "f", "description": "d", "prompt": "p"}, None)
    with pytest.raises(ValueError, match="prompt"):
        OrchestratorToolInvocation({"action": "spawn", "flow": "f", "node_id": "a", "description": "d"}, None)
    with pytest.raises(ValueError, match="description"):
        OrchestratorToolInvocation({"action": "spawn", "flow": "f", "node_id": "a", "prompt": "p"}, None)


@pytest.mark.asyncio
async def test_orchestrator_run_starts_flow(tmp_path: Path, monkeypatch) -> None:
    """orchestrator(run, flow=...) 显式启动内部工作流。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation({"action": "run", "flow": "demo"}, parent).execute()
    assert result.is_error is False
    assert "已启动" in result.content
    assert fake.run_calls == ["demo"]


@pytest.mark.asyncio
async def test_orchestrator_spawn_records_schedule(tmp_path: Path, monkeypatch) -> None:
    """编排 spawn 可带 schedule（仅记录，不生效），透传给 flow_coordinator。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation(
        {
            "action": "spawn",
            "description": "定时工作流",
            "prompt": "做任务",
            "flow": "daily",
            "node_id": "a",
            "schedule": {"kind": "cron", "cron": "0 9 * * *"},
        },
        parent,
    ).execute()

    assert result.is_error is False
    assert fake.spawned[0][9] == {"kind": "cron", "cron": "0 9 * * *"}


@pytest.mark.asyncio
async def test_orchestrator_status(tmp_path: Path, monkeypatch) -> None:
    """图级 action：status 委托 flow_coordinator；export 已并入 save(flow=…)。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    status = await OrchestratorToolInvocation({"action": "status", "flow": "demo"}, parent).execute()
    assert status.is_error is False
    assert "状态摘要" in status.content

    with pytest.raises(ValueError, match="未知 action"):
        OrchestratorToolInvocation({"action": "export", "flow": "demo"}, parent)

    missing = await OrchestratorToolInvocation({"action": "status"}, parent).execute()
    assert missing.is_error is True
    assert "flow" in missing.content


@pytest.mark.asyncio
async def test_orchestrator_load_unknown_flow(tmp_path: Path) -> None:
    """load 未固化的 flow：提示先 save（快照路径遍历面已随旧实现移除）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    result = await OrchestratorToolInvocation({"action": "load", "flow": "ghost"}, parent).execute()
    assert result.is_error is True
    assert "保存文件" in result.content


@pytest.mark.asyncio
async def test_orchestrator_load_from_draft(tmp_path: Path, monkeypatch) -> None:
    """load：按 flow 名载回最新定稿草案（save(flow=…) 固化的产物）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation
    from src.workflow.draft_store import WorkflowDraftStore

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    WorkflowDraftStore().save("name: demo\nnodes:\n  a:\n    task: 任务A\n")

    loaded = await OrchestratorToolInvocation({"action": "load", "flow": "demo"}, parent).execute()
    assert loaded.is_error is False
    assert "demo" in loaded.content

    with pytest.raises(ValueError, match="flow"):
        OrchestratorToolInvocation({"action": "load"}, parent)


@pytest.mark.asyncio
async def test_orchestrator_registered_as_deferred(tmp_path: Path) -> None:
    """挂起注册路径：register_root_scoped_tools 注册 orchestrator 且 should_defer（平时不暴露）。
    delegate 保持纯委派（无 flow 参数）。"""
    from src.coara.tool_registry import register_root_scoped_tools
    from src.tools.builtin.delegate.delegate import DelegateTool

    parent = make_test_coara(tmp_path)
    await parent.bootstrap_tools()
    await parent.initialize()

    register_root_scoped_tools(parent)

    assert "orchestrator" in parent._tool_manager.tools
    tool = parent._tool_manager.tools["orchestrator"]
    assert tool.should_defer is True  # 挂起：平时不占工具面
    props = tool.parameters_schema["properties"]
    assert "flow" in props
    assert "node_id" in props
    assert "auto_run" in props
    assert "schedule" in props
    assert "spawn" in props["action"]["enum"]
    assert "run" in props["action"]["enum"]
    assert "export" not in props["action"]["enum"]  # export 已并入 save(flow=…)
    # 未揭示前不在 LLM 工具面（deferred 隐藏）
    llm_names = [d["name"] for d in parent._tool_manager.get_tool_definitions_for_llm(parent.identity.is_owner_context)]
    assert "orchestrator" not in llm_names
    # delegate 保持纯委派：无 flow 参数
    assert "flow" not in DelegateTool.parameters_schema["properties"]
    assert "delegate" in parent._tool_manager.tools


@pytest.mark.asyncio
async def test_orchestrator_update_routes_to_coordinator(tmp_path: Path, monkeypatch) -> None:
    """update action：新任务指令经 task 参数透传给 flow_coordinator（schema 与实现一致）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation(
        {"action": "update", "flow": "demo", "node_id": "a", "task": "新任务", "routes_mode": "one"},
        parent,
    ).execute()
    assert result.is_error is False
    assert fake.updated == [("demo", "a", "新任务", "one")]

    # 只改任务不改路由模式
    await OrchestratorToolInvocation(
        {"action": "update", "flow": "demo", "node_id": "a", "task": "只改任务"},
        parent,
    ).execute()
    assert fake.updated[-1] == ("demo", "a", "只改任务", None)

    # task 与 routes_mode 都缺 → 报错
    bad = await OrchestratorToolInvocation(
        {"action": "update", "flow": "demo", "node_id": "a"},
        parent,
    ).execute()
    assert bad.is_error is True
    assert "task" in bad.content


@pytest.mark.asyncio
async def test_orchestrator_edge_add_remove(tmp_path: Path, monkeypatch) -> None:
    """edge action：加/删边透传 flow_coordinator（on 默认 success；remove 标志删边）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    added = await OrchestratorToolInvocation(
        {"action": "edge", "flow": "demo", "frm": "a", "to": "b"},
        parent,
    ).execute()
    assert added.is_error is False
    assert fake.edge_calls == [("add", "demo", "a", "b", "success")]

    removed = await OrchestratorToolInvocation(
        {"action": "edge", "flow": "demo", "frm": "a", "to": "b", "remove": True},
        parent,
    ).execute()
    assert removed.is_error is False
    assert fake.edge_calls[-1] == ("remove", "demo", "a", "b", "success")

    # 缺 frm/to → 构造期报参数错误
    with pytest.raises(ValueError, match="edge"):
        OrchestratorToolInvocation({"action": "edge", "flow": "demo"}, parent)


@pytest.mark.asyncio
async def test_orchestrator_remove_node(tmp_path: Path, monkeypatch) -> None:
    """remove action：删未运行节点透传 flow_coordinator。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    fake = _FakeFlowCoordinator()
    parent._flow_coordinator = fake  # 实例级协调器：注入 fake（主体隔离后的取用路径）

    result = await OrchestratorToolInvocation(
        {"action": "remove", "flow": "demo", "node_id": "a"},
        parent,
    ).execute()
    assert result.is_error is False
    assert fake.removed == [("demo", "a")]


@pytest.mark.asyncio
async def test_orchestrator_remove_missing_node_id_raises(tmp_path: Path, monkeypatch) -> None:
    """remove 缺 node_id：构造期报参数错误（与 edge 缺参一致）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)
    with pytest.raises(ValueError, match="node_id"):
        OrchestratorToolInvocation({"action": "remove", "flow": "demo"}, parent)


@pytest.mark.asyncio
async def test_orchestrator_update_edge_remove_guard_parent(tmp_path: Path) -> None:
    """update/edge/remove 在无父 Coara 上下文时报错（而非向 flow_coordinator 传 None）。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    for params in (
        {"action": "update", "flow": "demo", "node_id": "a", "task": "t"},
        {"action": "edge", "flow": "demo", "frm": "a", "to": "b"},
        {"action": "remove", "flow": "demo", "node_id": "a"},
    ):
        result = await OrchestratorToolInvocation(params, None).execute()
        assert result.is_error is True
        assert "父" in result.content


@pytest.mark.asyncio
async def test_orchestrator_wait_delegates_to_coordinator(tmp_path: Path, monkeypatch) -> None:
    """wait 委托 flow_coordinator.wait。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    parent = make_test_coara(tmp_path)

    class _Waitable:
        async def wait(self, flow: str) -> str:
            return f"flow {flow} 全部完成"

    parent._flow_coordinator = _Waitable()
    result = await OrchestratorToolInvocation({"action": "wait", "flow": "demo"}, parent).execute()
    assert result.is_error is False
    assert "全部完成" in result.content


def _merge_invocation():
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation

    return OrchestratorToolInvocation({"action": "status", "flow": "demo"}, None)


def test_merge_into_existing_draft_missing_returns_none(tmp_path: Path) -> None:
    """草案不存在（已被删）：返回 None，调用方回退新建。"""
    from src.workflow.draft_store import WorkflowDraftStore

    store = WorkflowDraftStore(coara_home=tmp_path)
    assert _merge_invocation()._merge_into_existing_draft(store, "draft-不存在", "name: x\nnodes:\n  a: {}\n") is None


def test_merge_into_existing_draft_blank_canvas_adopts_incoming(tmp_path: Path) -> None:
    """空画布（工作台占位草案：节点无任务无输入且无边）：直接采用本流内容。"""
    from src.workflow.draft_store import WorkflowDraftStore

    store = WorkflowDraftStore(coara_home=tmp_path)
    draft = store.save("name: workbench\nnodes:\n  节点1: {}\nedges: []\n", source_subagent="web-workbench")
    incoming = "name: 新流\nnodes:\n  a:\n    task: 干活\nedges: []\n"
    assert _merge_invocation()._merge_into_existing_draft(store, draft.draft_id, incoming) == incoming


def test_merge_into_existing_draft_merges_without_overwrite(tmp_path: Path) -> None:
    """有内容的草案：新流节点并入、边去重、草案名随最新流——一张画布可共存两张流。"""
    from src.workflow.core.serde import parse_graph
    from src.workflow.draft_store import WorkflowDraftStore

    store = WorkflowDraftStore(coara_home=tmp_path)
    draft = store.save(
        "name: 旧画布\nnodes:\n  旧节点:\n    task: 旧任务\nedges: []\n",
        source_subagent="web-workbench",
    )
    incoming = "name: 新流\nnodes:\n  新节点:\n    task: 新任务\nedges: []\n"
    merged = _merge_invocation()._merge_into_existing_draft(store, draft.draft_id, incoming)
    assert merged is not None
    graph = parse_graph(merged)
    # 草案名随最新流（与运行实例名一致，避免名字分叉）
    assert graph.name == "新流"
    assert set(graph.nodes) == {"旧节点", "新节点"}
    assert graph.nodes["旧节点"].task == "旧任务"
    assert graph.nodes["新节点"].task == "新任务"
