"""工作流调试（rerun/resume/reset）与模板库测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.coara.flow_coordinator import FlowCoordinator
from tests.helpers import make_test_coara


@pytest.fixture
def stub_build(monkeypatch) -> None:
    """打桩子智能体构建（单测只验簿记语义，不构造真 coaras）。"""

    async def _fake_build(self, parent, node_id, *, subagent_type: str = "coaras"):
        async def _noop_shutdown() -> None:
            return None

        return SimpleNamespace(shutdown=_noop_shutdown)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", _fake_build)


async def _spawn_two_node_flow(parent, coord: FlowCoordinator) -> None:
    await coord.spawn_node(
        parent,
        flow="f",
        node_id="a",
        task="任务A",
        seed="",
        depends_on=[],
        routes_to=[],
        routes_mode="all",
        subagent_type="cooras",
        auto_run=False,
        schedule=None,
    )
    await coord.spawn_node(
        parent,
        flow="f",
        node_id="b",
        task="任务B",
        seed="",
        depends_on=["a"],
        routes_to=[],
        routes_mode="all",
        subagent_type="cooras",
        auto_run=False,
        schedule=None,
    )


def _mark_ran(coord: FlowCoordinator) -> None:
    """簿记模拟一次完整跑：a→b 均 done、引擎各消费一轮。"""
    f = coord.get_flow("f")
    f.engine.record_arrival("a", "b", "A1")
    assert f.engine.consume("a") is not None
    assert f.engine.consume("b") is not None
    f.state("a").status = "done"
    f.state("a").result = "A1"
    f.state("a").activations = 1
    f.state("b").status = "done"
    f.state("b").result = "B1"
    f.state("b").activations = 1


@pytest.mark.asyncio
async def test_reset_flow_nodes_clears_and_reseeds(tmp_path: Path, stub_build) -> None:
    """reset：状态与引擎簿记清零；未重置的 wait 上游最近结果重新投递。"""
    parent = make_test_coara(tmp_path)
    coord = FlowCoordinator()
    await _spawn_two_node_flow(parent, coord)
    _mark_ran(coord)
    f = coord.get_flow("f")

    msg = await coord.reset_flow_nodes(parent, flow="f", node_ids=["b"])
    assert msg.startswith("已重置")
    assert f.state("b").status == "pending"
    assert f.state("b").result == ""
    assert f.engine.activations_of("b") == 0
    # b 的 wait 上游 a（未重置，结果 A1）已重新投递 → b 再次就绪
    assert f.engine.is_ready("b") is True

    # 全量重置：a 是源节点可直接就绪；b 等待新投递
    await coord.reset_flow_nodes(parent, flow="f", node_ids=["a", "b"])
    assert f.engine.is_ready("a") is True
    assert f.engine.is_ready("b") is False


@pytest.mark.asyncio
async def test_reset_rejects_running_node(tmp_path: Path, stub_build) -> None:
    parent = make_test_coara(tmp_path)
    coord = FlowCoordinator()
    await _spawn_two_node_flow(parent, coord)
    f = coord.get_flow("f")
    f.state("a").status = "running"
    msg = await coord.reset_flow_nodes(parent, flow="f", node_ids=["a"])
    assert "正在运行" in msg


@pytest.mark.asyncio
async def test_rerun_cascade_resets_downstream(tmp_path: Path, monkeypatch, stub_build) -> None:
    """rerun(cascade)：目标与全部下游一起重置后点火（run 打桩防真跑）。"""
    parent = make_test_coara(tmp_path)
    coord = FlowCoordinator()
    await _spawn_two_node_flow(parent, coord)
    _mark_ran(coord)

    closure = coord._downstream_closure(coord.get_flow("f"), "a")
    assert set(closure) == {"b"}

    started: list[str] = []
    monkeypatch.setattr(coord, "run", lambda flow: started.append(flow) or "已启动", raising=False)
    msg = await coord.rerun_node(parent, flow="f", node_id="a", cascade=True)
    f = coord.get_flow("f")
    assert f.state("a").status == "pending"
    assert f.state("b").status == "pending"
    assert started == ["f"]
    assert "已启动" in msg


@pytest.mark.asyncio
async def test_resume_resets_failed_and_runs(tmp_path: Path, monkeypatch, stub_build) -> None:
    """resume：失败节点（含下游）重置后点火。"""
    parent = make_test_coara(tmp_path)
    coord = FlowCoordinator()
    await _spawn_two_node_flow(parent, coord)
    f = coord.get_flow("f")
    f.state("a").status = "failed"
    f.state("a").error = "x"

    started: list[str] = []
    monkeypatch.setattr(coord, "run", lambda flow: started.append(flow) or "已启动", raising=False)
    await coord.resume_flow(parent, flow="f")
    assert f.state("a").status == "pending"
    assert f.state("a").error == ""
    assert started == ["f"]


def test_builtin_templates_validate() -> None:
    """模板库全部通过内核校验（防后续改坏）。"""
    from src.workflow.core.semantics import validate_graph
    from src.workflow.core.serde import parse_graph

    templates_dir = Path(__file__).resolve().parents[2] / "src" / "workflow" / "templates"
    files = list(templates_dir.glob("*.yaml"))
    assert len(files) >= 3
    for path in files:
        graph = parse_graph(path.read_text(encoding="utf-8"))
        errors = [i.message for i in validate_graph(graph) if i.level == "error"]
        assert errors == [], f"{path.name}: {errors}"
