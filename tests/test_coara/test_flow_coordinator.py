"""flow_coordinator 核心逻辑测试（mock 子智能体，不触发真实 LLM）。"""

from __future__ import annotations

import asyncio

import pytest

from src.coara.flow_coordinator import FlowCoordinator


class FakeSubagent:
    def __init__(self, node_id: str):
        self.node_id = node_id
        self.seen_prompt = ""
        self.message_history = []

    async def process_message(self, prompt, **kwargs):
        self.seen_prompt = prompt
        yield f"{self.node_id}-result"

    async def shutdown(self):
        pass


@pytest.fixture
def coord(monkeypatch) -> FlowCoordinator:
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)
    # 默认 auto_run=True 保持既有「spawn 即跑」测试语义；显式传 auto_run=False 的测试不受影响
    orig_spawn = c.spawn_node

    async def spawn_default_auto(*args, **kwargs):
        kwargs.setdefault("auto_run", True)
        return await orig_spawn(*args, **kwargs)

    monkeypatch.setattr(c, "spawn_node", spawn_default_auto)
    return c


async def test_chain(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"])
    await coord.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"])
    # 激活收尾后停车引用即释放（state.coara=None，重复激活走懒建新建），
    # 先抓执行期引用再断言
    b_sub = coord.get_flow("t").state_of("b").coara
    status = await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("a").status == "done"
    assert f.state_of("b").status == "done"
    assert f.state_of("b").received == ["a-result"]
    assert "a-result" in b_sub.seen_prompt
    assert f.state_of("b").coara is None  # 收尾即释放，不残留已 shutdown 实例
    assert "✓ a" in status and "✓ b" in status


async def test_chinese_flow_and_node_names(coord):
    """flow 名与 node_id 支持中文；路径危险字符仍拒绝。"""
    from src.coara.flow_coordinator import _validate_identifier

    _validate_identifier("代码评审流水线", "flow")
    _validate_identifier("收集变更", "node_id")
    await coord.spawn_node(None, flow="评审", node_id="收集", task="A", routes_to=["汇总"])
    await coord.spawn_node(None, flow="评审", node_id="汇总", task="B", depends_on=["收集"])
    await coord.wait("评审")
    f = coord.get_flow("评审")
    assert f.state_of("汇总").status == "done"
    assert f.state_of("汇总").received == ["收集-result"]
    for bad in ("../evil", "a/b", "a\\b", "a b", ""):
        try:
            _validate_identifier(bad, "flow")
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝：{bad!r}")


async def test_fan_in(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["c"])
    await coord.spawn_node(None, flow="t", node_id="b", task="B", routes_to=["c"])
    await coord.spawn_node(None, flow="t", node_id="c", task="C", depends_on=["a", "b"])
    await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("c").status == "done"
    assert set(f.state_of("c").received) == {"a-result", "b-result"}


async def test_buffer_upstream_done_before_spawn(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"])
    await asyncio.sleep(0)  # 让 A 跑完
    await coord.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"])
    # b spawn 时 a 已完成，到达已入引擎队列；wait 收尾前让它跑完
    f = coord.get_flow("t")
    await asyncio.sleep(0.05)
    if f.state_of("b").status == "pending":
        coord.run("t")
    await coord.wait("t")
    assert f.state_of("b").received == ["a-result"]
    assert f.state_of("b").status == "done"


async def test_missing_dep_fails(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", depends_on=["ghost"])
    await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("a").status == "failed"
    assert "依赖" in f.state_of("a").error


async def test_deadlock_fails(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", depends_on=["b"])
    await coord.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"])
    await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("a").status == "failed"
    assert f.state_of("b").status == "failed"


async def test_build_subagent_integration(tmp_path):
    """真实 _build_subagent 构造（fake provider，不触发 LLM 调用）。"""
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path, name="parent")
    await parent.bootstrap_tools()
    await parent.initialize()
    coord = FlowCoordinator()
    sa = await coord._build_subagent(parent, "node-x")
    assert sa.identity.name == "sa-flow-node-x"
    assert sa.delegate_depth == 1
    assert sa.workspace_dir == tmp_path
    # flow 节点应带 deliver 工具（交付 + 声明后继）
    assert "deliver" in sa._tool_manager.tools


# ---------------------------------------------------------------------------
# reset：手动 /new 时释放全部 flow
# ---------------------------------------------------------------------------


async def test_reset_clears_all_flows(coord):
    await coord.spawn_node(None, flow="t1", node_id="a", task="A")
    await coord.spawn_node(None, flow="t2", node_id="b", task="B")
    assert coord.get_flow("t1") is not None
    assert coord.get_flow("t2") is not None
    await coord.reset()
    assert coord.get_flow("t1") is None
    assert coord.get_flow("t2") is None


async def test_reset_shuts_down_node_subagents(coord, monkeypatch):
    shut: list[str] = []

    class SpySubagent(FakeSubagent):
        async def shutdown(self):
            shut.append(self.node_id)

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return SpySubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)
    await coord.spawn_node(None, flow="t", node_id="a", task="A")
    await coord.spawn_node(None, flow="t", node_id="b", task="B")
    await coord.reset()
    assert sorted(shut) == ["a", "b"]


# ---------------------------------------------------------------------------
# load：未收尾节点无法续跑，标记失败（避免 load 后 run 崩空子智能体）
# ---------------------------------------------------------------------------


def test_load_marks_incomplete_nodes_failed(coord):
    data = {
        "name": "t",
        "max_hops": 100,
        "hops": 0,
        "nodes": {
            "a": {
                "task": "A",
                "input": "",
                "depends_on": [],
                "routes_to": [],
                "routes_mode": "all",
                "status": "done",
                "result": "ok",
                "error": "",
            },
            "b": {
                "task": "B",
                "input": "",
                "depends_on": ["a"],
                "routes_to": [],
                "routes_mode": "all",
                "status": "pending",
                "result": "",
                "error": "",
            },
            "c": {
                "task": "C",
                "input": "",
                "depends_on": [],
                "routes_to": [],
                "routes_mode": "all",
                "status": "running",
                "result": "",
                "error": "",
            },
        },
    }
    coord.load(data)
    f = coord.get_flow("t")
    assert f.state_of("a").status == "done"
    assert f.state_of("b").status == "failed"
    assert f.state_of("c").status == "failed"
    assert "不支持继续运行" in f.state_of("b").error
    assert "不支持续跑" in f.state_of("b").result
    # 已收尾节点的结果/错误原样保留
    assert f.state_of("a").result == "ok"


async def test_run_node_without_subagent_fails_gracefully(coord):
    """防御：coara 未构建（load 恢复 / flow 已释放）时 _run_node 标失败不崩。"""
    await coord.spawn_node(None, flow="t", node_id="a", task="A")
    f = coord.get_flow("t")
    state = f.state_of("a")
    state.coara = None
    state.status = "pending"
    await coord._run_node(f, "a")
    assert state.status == "failed"
    assert "未构建" in state.error
    # 收尾仍走通：flow 可正常结束（不会挂死 wait）
    assert f.done.is_set()


class ScriptedSubagent:
    """按脚本返回 (result, next_hint)，每次执行取脚本下一行。"""

    def __init__(self, node_id: str, script: list[tuple[str, str | None]]):
        self.node_id = node_id
        self.script = list(script)
        self.idx = 0
        self.message_history = []

    async def process_message(self, prompt, **kwargs):
        if self.idx < len(self.script):
            result, nxt = self.script[self.idx]
            self.idx += 1
        else:
            result, nxt = "fallback", None
        self._final_deliver_message = result
        self._next_node = nxt
        yield result

    async def shutdown(self):
        pass


async def test_real_flow_end_to_end(tmp_path):
    """真实 CoaraBase + FakeProvider 端到端跑 3 节点链（非 mock 子智能体）。"""
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider, make_test_coara

    provider = FakeProvider(
        [
            LLMResponse(content="研究结果：主题 X"),
            LLMResponse(content="初稿内容"),
            LLMResponse(content="发布完成"),
        ]
    )
    parent = make_test_coara(tmp_path, name="parent", provider=provider)
    await parent.bootstrap_tools()
    await parent.initialize()

    coord = FlowCoordinator()
    await coord.spawn_node(parent, flow="t", node_id="research", task="调研", routes_to=["draft"], auto_run=True)
    await coord.spawn_node(
        parent,
        flow="t",
        node_id="draft",
        task="写初稿",
        depends_on=["research"],
        routes_to=["publish"],
        auto_run=True,
    )
    await coord.spawn_node(parent, flow="t", node_id="publish", task="发布", depends_on=["draft"], auto_run=True)
    status = await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("research").status == "done"
    assert f.state_of("draft").status == "done"
    assert f.state_of("publish").status == "done"
    assert f.state_of("draft").received == ["研究结果：主题 X"]
    assert f.state_of("publish").received == ["初稿内容"]
    assert f.hops == 3
    assert "✓" in status


async def test_real_loop_end_to_end(tmp_path):
    """真实 CoaraBase 跑 review 打回 draft 的循环（report next 端到端）。"""
    from src.core.types import ToolCall
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider, make_test_coara

    provider = FakeProvider(
        [
            LLMResponse(content="研究结果"),
            LLMResponse(content="初稿 v1"),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="deliver", arguments={"message": "打回原因", "next": "draft"})],
            ),
            LLMResponse(content="初稿 v2"),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c2", name="deliver", arguments={"message": "通过", "next": "publish"})],
            ),
            LLMResponse(content="发布完成"),
        ]
    )
    parent = make_test_coara(tmp_path, name="parent", provider=provider)
    await parent.bootstrap_tools()
    await parent.initialize()

    coord = FlowCoordinator()
    await coord.spawn_node(parent, flow="t", node_id="research", task="调研", routes_to=["draft"], auto_run=True)
    await coord.spawn_node(
        parent, flow="t", node_id="draft", task="写", depends_on=["research"], routes_to=["review"], auto_run=True
    )
    await coord.spawn_node(
        parent,
        flow="t",
        node_id="review",
        task="审",
        depends_on=["draft"],
        routes_to=["publish", "draft"],
        routes_mode="one",
        auto_run=True,
    )
    await coord.spawn_node(parent, flow="t", node_id="publish", task="发", depends_on=["review"], auto_run=True)
    await coord.wait("t")
    f = coord.get_flow("t")
    assert f.state_of("publish").status == "done"
    assert f.state_of("publish").received == ["通过"]
    # draft 两轮：首轮收 research 投递，次轮收 review 打回（received 只存最近一轮）
    assert f.state_of("draft").result == "初稿 v2"
    assert f.state_of("draft").activations == 2
    assert f.hops == 6


async def test_loop_one_mode(monkeypatch):
    """review 打回 draft 循环两轮后通过 → publish。"""
    c = FlowCoordinator()
    fakes = {
        "research": [ScriptedSubagent("research", [("research-result", None)])],
        "draft": [
            ScriptedSubagent("draft", [("draft-v1", None)]),
            ScriptedSubagent("draft", [("draft-v2", None)]),
        ],
        "review": [
            ScriptedSubagent("review", [("打回", "draft")]),
            ScriptedSubagent("review", [("通过", "publish")]),
        ],
        "publish": [ScriptedSubagent("publish", [("published", None)])],
    }

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        # 每次激活懒建新建实例（激活收尾后 state.coara 置 None，不复用旧身）
        return fakes[node_id].pop(0)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    # 重复激活靠懒建，懒建需要非 None parent（_emit_trace 可选）
    parent = RecordingParent()
    await c.spawn_node(parent, flow="t", node_id="research", task="R", routes_to=["draft"], auto_run=True)
    await c.spawn_node(
        parent, flow="t", node_id="draft", task="D", depends_on=["research"], routes_to=["review"], auto_run=True
    )
    await c.spawn_node(
        parent,
        flow="t",
        node_id="review",
        task="V",
        depends_on=["draft"],
        routes_to=["publish", "draft"],
        routes_mode="one",
        auto_run=True,
    )
    await c.spawn_node(parent, flow="t", node_id="publish", task="P", depends_on=["review"], auto_run=True)
    await c.wait("t")
    f = c.get_flow("t")
    assert f.state_of("publish").status == "done"
    assert f.state_of("publish").received == ["通过"]
    assert f.hops == 6  # research + draft×2 + review×2 + publish


class RecordingParent:
    """记录 _emit_trace 的假父 Coara，验证节点生命周期事件广播。"""

    def __init__(self):
        self.traces = []

    def _emit_trace(self, event_type, message, payload=None):
        self.traces.append((event_type, message, payload or {}))


async def test_node_emits_lifecycle_events(monkeypatch):
    """节点 spawn 广播 pending start、运行/结束广播 subagent_start/complete，让 CLI spinner 显示停车灰行 → 圈圈 → ✓。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)
    parent = RecordingParent()
    await c.spawn_node(parent, flow="t", node_id="a", task="A", auto_run=True)
    await c.wait("t")

    lifecycle = [e for e in parent.traces if e[0] in ("subagent_start", "subagent_complete", "subagent_failed")]
    assert [e[0] for e in lifecycle] == ["subagent_start", "subagent_start", "subagent_complete"]
    # 第一条：spawn 停车广播（pending 灰色行）
    park_payload = lifecycle[0][2]
    assert park_payload["status"] == "pending"
    assert park_payload["parent_activity_id"] == "flow-t"
    assert park_payload["subagent_type"] == "cooras"
    assert park_payload["subagent_id"] == "flow-t-a"
    assert park_payload["description"] == "flow t · a"
    # 第二条：真正运行广播（亮色转圈）
    run_payload = lifecycle[1][2]
    assert run_payload["status"] == ""
    assert run_payload["subagent_id"] == "flow-t-a"
    complete_payload = lifecycle[2][2]
    assert complete_payload["subagent_id"] == "flow-t-a"


async def test_node_emits_failed_event(monkeypatch):
    """节点失败广播 subagent_failed。"""
    c = FlowCoordinator()

    class BoomSubagent(FakeSubagent):
        async def process_message(self, prompt, **kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return BoomSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)
    parent = RecordingParent()
    await c.spawn_node(parent, flow="t", node_id="a", task="A", auto_run=True)
    await c.wait("t")

    lifecycle = [e for e in parent.traces if e[0] in ("subagent_start", "subagent_complete", "subagent_failed")]
    assert [e[0] for e in lifecycle] == ["subagent_start", "subagent_start", "subagent_failed"]
    assert lifecycle[2][2]["error"] == "boom"


async def test_spawn_auto_run_false_stays_parked(monkeypatch):
    """auto_run=False：spawn 只登记，节点停车不运行（等显式 run）。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], auto_run=False)
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=False)
    f = c.get_flow("t")
    assert f.state_of("a").status == "pending"
    assert f.state_of("b").status == "pending"
    assert f.state_of("a").result == ""  # 未运行


async def test_run_starts_ready_nodes_then_cascades(monkeypatch):
    """run() 显式启动就绪节点（入口），完成后按依赖级联推进。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], auto_run=False)
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=False)
    msg = c.run("t")
    assert "已启动" in msg and "a" in msg
    assert "b" not in msg  # b 依赖未满足，不在本轮启动

    await c.wait("t")
    f = c.get_flow("t")
    assert f.state_of("a").status == "done"
    assert f.state_of("b").status == "done"
    assert f.state_of("b").received == ["a-result"]


async def test_run_without_ready_nodes(monkeypatch):
    """没有就绪节点时 run 返回提示，不误启动。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", depends_on=["ghost"], auto_run=False)
    msg = c.run("t")
    assert "没有就绪节点" in msg
    f = c.get_flow("t")
    assert f.state_of("a").status == "pending"


async def test_schedule_recorded_not_effective(monkeypatch):
    """schedule 仅记录（内部不生效），spawn 不触发任何定时启动。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(
        None,
        flow="t",
        node_id="a",
        task="A",
        auto_run=False,
        schedule={"kind": "cron", "cron": "0 9 * * *"},
    )
    f = c.get_flow("t")
    assert f.schedule == {"kind": "cron", "cron": "0 9 * * *"}
    assert f.state_of("a").status == "pending"  # 没有定时器，不会自己跑


async def test_export_wdl_includes_recorded_schedule(monkeypatch):
    """内部记录的 schedule 投影进 WDL（外部生效），内部仍不触发。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(
        None,
        flow="t",
        node_id="a",
        task="A",
        auto_run=False,
        schedule={"kind": "cron", "cron": "0 9 * * *"},
    )
    import yaml

    doc = yaml.safe_load(c.export_wdl("t"))
    assert doc["schedule"] == {"kind": "cron", "cron": "0 9 * * *"}


async def test_snapshot_returns_graph(monkeypatch):
    """snapshot 返回图全量（节点/边/状态），供实时视图初始化。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], auto_run=False)
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=False)
    snap = c.snapshot("t")
    assert snap is not None
    assert {n["id"] for n in snap["nodes"]} == {"a", "b"}
    assert {n["status"] for n in snap["nodes"]} == {"pending"}
    assert {n["agent_id"] for n in snap["nodes"]} == {"sa-flow-a", "sa-flow-b"}
    assert {"from": "a", "to": "b", "on": "success"} in snap["edges"]
    assert snap["schedule"] == {"kind": "manual"}
    assert c.snapshot("missing") is None


async def test_wait_hints_parked_ready_nodes(monkeypatch):
    """wait 对「就绪但未启动」的节点给提示，不误标失败。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", auto_run=False)
    status = await c.wait("t")
    assert "已就绪但未启动" in status
    assert 'orchestrator(action="run"' in status
    f = c.get_flow("t")
    assert f.state_of("a").status == "pending"  # 未被误标失败


async def test_wait_hint_then_run_completes(monkeypatch):
    """#18 复核：wait 提示「就绪未启动」后 flow 不残留半初始化，run 可正常推进至完成。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", auto_run=False)
    hint = await c.wait("t")
    assert "已就绪但未启动" in hint

    c.run("t")
    final = await c.wait("t")
    assert "a [done]" in final
    assert c.get_flow("t").state_of("a").status == "done"


async def test_wait_prunes_dangling_edge_arrivals(monkeypatch):
    """#18：wait 清理悬空边时缓冲到达一并作废——后 spawn 的下游不得凭残留到达将就绪。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], auto_run=True)
    await asyncio.sleep(0)  # a 跑完：b 未 spawn，到达进悬空边 a→b 队列
    f = c.get_flow("t")
    assert f.state_of("a").status == "done"
    assert f.engine.pending_total("b") == 1  # 残留到达确实存在

    await c.wait("t")  # spawn 收官：悬空边与其到达一并清理
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=False)
    assert f.engine.pending_total("b") == 0  # 残留到达已作废
    assert not f.engine.is_ready("b")


async def test_reset_ignores_stale_node_done(monkeypatch):
    """#19：reset（/new）后迟到的节点完成回调不得推进旧 flow（不建新任务、不崩）。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], auto_run=True)
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=False)
    f = c.get_flow("t")
    assert f is not None and f.state_of("a").status == "running"

    await c.reset()  # 释放全部 flow（/new 语义）
    # 模拟运行中节点的 finally 在 reset 之后触发迟到回调
    c._on_node_done(f, "a")
    # 守卫生效：不崩、不创建新任务推进 b
    assert c.get_flow("t") is None
    assert f.state_of("b").status == "pending"  # b 未被误启动


async def test_reset_ignores_stale_maybe_start_callback(monkeypatch):
    """#19：reset 后 running_task 迟到的重试回调不得为旧 flow 再点火（世代失配放弃）。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["a"], on="error", auto_run=True)
    f = c.get_flow("t")
    st = f.state_of("a")
    task = st.running_task
    assert st.status == "running"
    # 模拟 reset 前已注册的多轮重试回调（旧世代快照）+ 待消费到达
    snap = c._epoch
    f.engine.record_arrival("a", "a", "x", on="error")
    task.add_done_callback(lambda _t: c._maybe_start(f, "a", snap))

    await c.reset()  # 取消任务、shutdown 子智能体、世代 +1
    await asyncio.sleep(0)  # 发射迟到回调
    assert st.running_task is task  # 未在已 shutdown 的旧 coara 上再造任务


async def test_spawn_rejects_invalid_identifiers(monkeypatch):
    """flow/node_id 必须是合法标识符（防路径逃逸与非法 key）。"""
    c = FlowCoordinator()

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return FakeSubagent(node_id)

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    with pytest.raises(ValueError, match="flow"):
        await c.spawn_node(None, flow="../evil", node_id="a", task="A")
    with pytest.raises(ValueError, match="node_id"):
        await c.spawn_node(None, flow="t", node_id="a b", task="A")


async def test_export_wdl(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="任务A", routes_to=["b"])
    await coord.spawn_node(None, flow="t", node_id="b", task="任务B", depends_on=["a"])
    await coord.wait("t")
    wdl = coord.export_wdl("t")
    import yaml

    doc = yaml.safe_load(wdl)
    assert doc["name"] == "t"
    assert set(doc["nodes"]) == {"a", "b"}
    assert doc["nodes"]["a"]["task"] == "任务A"
    assert {"from": "a", "to": "b"} in doc["edges"]  # 默认 on=success 省略
    assert "schedule" not in doc  # 默认 manual 省略


async def test_export_wdl_fan_in(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["c"])
    await coord.spawn_node(None, flow="t", node_id="b", task="B", routes_to=["c"])
    await coord.spawn_node(None, flow="t", node_id="c", task="C", depends_on=["a", "b"])
    await coord.wait("t")
    import yaml

    doc = yaml.safe_load(coord.export_wdl("t"))
    assert {e["from"] for e in doc["edges"] if e["to"] == "c"} == {"a", "b"}


async def test_dump_load(coord):
    await coord.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"])
    await coord.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"])
    await coord.wait("t")
    data = coord.dump("t")
    assert data["states"]["a"]["status"] == "done"
    # 新协调器加载后仍可导出
    c2 = FlowCoordinator()
    c2.load(data)
    assert c2.get_flow("t").state_of("b").result == "b-result"
    assert "nodes" in c2.export_wdl("t")


async def test_one_mode_invalid_next(monkeypatch):
    """next 不在 routes_to → 不路由，流程仍收尾。"""
    c = FlowCoordinator()
    fakes = {
        "a": ScriptedSubagent("a", [("a-result", "ghost")]),
        "b": ScriptedSubagent("b", [("b-result", None)]),
    }

    async def fake_build(self, parent, node_id, *, subagent_type="coaras"):
        return fakes[node_id]

    monkeypatch.setattr(FlowCoordinator, "_build_subagent", fake_build)

    await c.spawn_node(None, flow="t", node_id="a", task="A", routes_to=["b"], routes_mode="one", auto_run=True)
    await c.spawn_node(None, flow="t", node_id="b", task="B", depends_on=["a"], auto_run=True)
    import asyncio as _aio

    try:
        await _aio.wait_for(c.wait("t"), timeout=2)
    except TimeoutError:
        pass  # b 永远等不到到达：显式补一次 wait 让死锁兜底生效
        c._fail_pending(c.get_flow("t"))
        c._finish(c.get_flow("t"))
    f = c.get_flow("t")
    assert f.state_of("a").status == "done"
    # a 声明后继 ghost 无效 → 不投递 b；b 依赖未满足 → 失败（兜底）
    assert f.state_of("b").status == "failed"
