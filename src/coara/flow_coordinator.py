"""flow 协调器 — 工作流内核（src/workflow/core）的内存宿主。

结构唯一真相是 FlowGraph（节点=智能体，边=拓扑）；运行簿记委托
ActivationEngine（wait/kick 边分类、轮次就绪、激活上限）。协调器只做：
构建子智能体、渲染输入、投递到达、级联启动、事件广播。

随时调整：图在运行中可改（改 task、增删节点与边），engine.refresh
同步分类；已跑部分即实录，新激活读到的是新图。

结果缓冲：上游先完成、下游未 spawn——到达进 engine 队列（悬空边），
下游 spawn 后就绪检查自动消化；wait 时清理仍未落地的悬空边。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.core.types import MessageRole
from src.workflow.core import (
    ActivationEngine,
    FlowGraph,
    Node,
    emit_graph,
    validate_identifier,
)
from src.workflow.core.semantics import classify_edges, template_step_refs

if TYPE_CHECKING:
    from src.coara.base import CoaraBase

_SUBAGENT_MAX_TOOL_ITERATIONS = 1500

# 兼容再导出：外部按旧路径引用校验函数
__all__ = ["FlowCoordinator", "get_flow_coordinator", "_validate_identifier"]


def _validate_identifier(name: str, kind: str) -> None:
    validate_identifier(name, kind)


@dataclass
class NodeState:
    """节点运行态（结构在 FlowGraph 上，这里是簿记）。"""

    status: str = "pending"  # pending | running | done | failed
    activations: int = 0
    result: str = ""
    error: str = ""
    received: list[str] = field(default_factory=list)  # 最近一轮到达文本
    next_hint: str | None = None  # routes=one：运行期声明的后继
    coara: Any = None  # 停车的 CoaraBase
    parent: Any = None  # 发起方 CoaraBase（UI 事件广播）
    running_task: Any = None


class Flow:
    """运行中的 flow：图 + 簿记引擎 + 节点态。"""

    def __init__(self, name: str, max_activations: int | None = None):
        self.graph = FlowGraph(name=name, max_activations=max_activations or 100)
        self.engine = ActivationEngine(self.graph)
        self.states: dict[str, NodeState] = {}
        self.hops = 0
        self.done = asyncio.Event()
        # flow 级父实例兜底：load/重建恢复的图节点态没有 parent，运行时懒建
        # 子智能体与事件广播都退到它
        self.parent: Any = None

    @property
    def schedule(self) -> dict | None:
        sch = self.graph.schedule
        return dict(sch) if sch else None

    def state(self, node_id: str) -> NodeState:
        st = self.states.get(node_id)
        if st is None:
            st = NodeState()
            self.states[node_id] = st
        return st

    def state_of(self, node_id: str) -> NodeState:
        """测试兼容访问：state_of("x")（str 键）→ 节点运行态。

        历史上测试写 f.nodes["a"]（dict 下标），迁移期把字符串参数交给
        state()；非字符串调用不再支持。
        """
        if isinstance(node_id, str):
            return self.state(node_id)
        raise TypeError(f"state_of 需要节点名字符串，得到 {type(node_id).__name__}")


class FlowCoordinator:
    """薄协调器：持有 flow 图与运行态，不做 LLM 决策。"""

    def __init__(self) -> None:
        self._flows: dict[str, Flow] = {}
        # flow 名 → 绑定草案 id（草案是唯一活模型，内存图是执行缓存）
        self._draft_ids: dict[str, str] = {}
        # flow 名 → 最近写穿/读取的草案 updated_at（外部修改检测：共同编辑 rebase 用）
        self._draft_sync: dict[str, str] = {}
        # UI 侧改过的 flow：下次 run/wait 前从草案重建图
        self._stale: set[str] = set()
        # reset（/new）世代号：节点任务启动时快照，回调失配即放弃（竞态守卫）
        self._epoch = 0

    # ── 草案绑定（编排写穿） ────────────────────────────────────────────

    def bind_draft(self, flow: str, draft_id: str) -> None:
        self._draft_ids[flow] = draft_id
        self._stale.discard(flow)

    def note_draft_sync(self, flow: str, updated_at: str) -> None:
        """登记本协调器已知的草案版本（写穿/载回后调用）。"""
        self._draft_sync[flow] = updated_at

    def draft_sync(self, flow: str) -> str | None:
        return self._draft_sync.get(flow)

    def unbind_draft(self, draft_id: str) -> None:
        for name, did in list(self._draft_ids.items()):
            if did == draft_id:
                self._draft_ids.pop(name, None)
                self._draft_sync.pop(name, None)
                self._stale.discard(name)

    def draft_id_for(self, flow: str) -> str | None:
        return self._draft_ids.get(flow)

    def flow_for_draft(self, draft_id: str) -> str | None:
        for name, did in self._draft_ids.items():
            if did == draft_id:
                return name
        return None

    def mark_stale(self, flow: str) -> None:
        """UI 侧改了绑定草案：内存图作废，下次运行前从草案重建。"""
        if flow in self._draft_ids:
            self._stale.add(flow)

    def is_stale(self, flow: str) -> bool:
        return flow in self._stale

    def bind_parent(self, flow: str, parent: Any) -> None:
        """load/重建后补父实例：事件广播与懒建子智能体用。"""
        f = self._flows.get(flow)
        if f is None:
            return
        f.parent = parent
        for state in f.states.values():
            if state.parent is None:
                state.parent = parent

    def rebuild_from_projection(self, parent: Any, *, flow: str, wdl: str) -> str:
        """从草案投影重建干净图（UI 编辑后的 stale 恢复；已跑实录丢弃）。"""
        from src.workflow.core import parse_graph

        graph = parse_graph(wdl)
        old = self._flows.pop(flow, None)
        if old is not None:
            # 移除前先放行 wait() 等待者：旧图作废，停在 f.done.wait() 的
            # 协程不该随旧 flow 一起挂死
            old.done.set()
            for state in old.states.values():
                task = state.running_task
                if task is not None and not task.done():
                    task.cancel()
        f = Flow(graph.name, max_activations=graph.max_activations)
        f.graph = graph
        f.engine = ActivationEngine(graph)
        f.parent = parent
        self._flows[graph.name] = f
        if graph.name != flow:
            # 投影里的名字变了：绑定跟新名字走
            did = self._draft_ids.pop(flow, None)
            if did is not None:
                self._draft_ids[graph.name] = did
        self._stale.discard(flow)
        self._stale.discard(graph.name)
        return graph.name

    def get_flow(self, name: str) -> Flow | None:
        return self._flows.get(name)

    def _get_or_create(self, name: str, max_activations: int | None) -> Flow:
        if name not in self._flows:
            self._flows[name] = Flow(name, max_activations=max_activations)
        return self._flows[name]

    # ── 编排（织图） ────────────────────────────────────────────────────

    async def spawn_node(
        self,
        parent: CoaraBase,
        *,
        flow: str,
        node_id: str,
        task: str,
        seed: str = "",
        depends_on: list[str] | None = None,
        routes_to: list[str] | None = None,
        routes_mode: str = "all",
        max_activations: int | None = None,
        subagent_type: str = "coaras",
        auto_run: bool = False,
        schedule: dict | None = None,
        on: str = "success",
        provider: str = "",
        model: str = "",
    ) -> str:
        """登记节点并织边。depends_on/routes_to 都落成边（拓扑唯一存储）。

        on=error 时 routes_to 的边是失败兜底路由（重试）。
        provider/model 为节点级 LLM 覆盖（空=跟随图级/全局 workflow.node）。
        """
        _validate_identifier(flow, "flow")
        _validate_identifier(node_id, "node_id")
        is_new_flow = flow not in self._flows
        f = self._get_or_create(flow, max_activations)
        f.parent = parent
        if schedule is not None and (not f.graph.schedule or f.graph.schedule == {"kind": "manual"}):
            f.graph.schedule = dict(schedule)
        if node_id in f.graph.nodes:
            return f"节点 {node_id} 已存在于 flow {flow}，跳过重复 spawn"
        # 先构建子智能体再提交图：失败时不留下 coara=None 的僵尸节点（画布也收不到 spawn）
        # provider/model 仅在非空时下传（空=跟随图级/全局，且兼容测试里的简化 fake）
        from src.workflow.node_llm import merge_node_llm

        _np, _nm = merge_node_llm(provider, model, f.graph.provider, f.graph.model)
        _llm_override = {k: v for k, v in (("provider", _np), ("model", _nm)) if v}
        coara = await self._build_subagent(
            parent,
            node_id,
            subagent_type=subagent_type,
            **_llm_override,
        )
        f.graph.add_node(
            Node(
                id=node_id,
                task=task,
                input=seed,
                routes=routes_mode,
                provider=provider.strip(),
                model=model.strip(),
            )
        )
        for dep in depends_on or []:
            self._try_add_edge(f, dep, node_id, "success")
        for tgt in routes_to or []:
            self._try_add_edge(f, node_id, tgt, on)
        f.engine.refresh(f.graph)
        # 创建停车 coaras：构造 + 引导 + 初始化，但不跑 process_message
        state = f.state(node_id)
        state.coara = coara
        state.parent = parent
        if is_new_flow:
            self._emit_flow_event(parent, "flow_started", f"flow {flow} 已创建", flow_name=flow)
        self._emit_graph_event(parent, f, "spawn", node_id)
        self._emit_node_event(
            parent,
            "subagent_start",
            f"Flow 节点登记：{node_id}",
            flow_name=flow,
            node_id=node_id,
            state=state,
            status="pending",
        )
        if auto_run:
            self._maybe_start(f, node_id)
        deps = f.graph.depends_on(node_id)
        routes = f.graph.routes_to(node_id)
        return f"节点 {node_id} 已注册：depends_on={deps or '无'}，routes_to={routes or '无'}"

    def _try_add_edge(self, f: Flow, frm: str, to: str, on: str) -> None:
        with contextlib.suppress(ValueError):  # 边已存在：幂等
            f.graph.add_edge(frm, to, on=on)

    # ── 随时调整（运行中改图，未激活生效） ──────────────────────────────

    def update_node(
        self,
        parent: CoaraBase,
        *,
        flow: str,
        node_id: str,
        task: str | None = None,
        routes_mode: str | None = None,
    ) -> str:
        """改节点任务/路由模式：下一次激活生效（已跑部分即实录不变）。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        node = f.graph.nodes.get(node_id)
        if node is None:
            return f"节点 {node_id} 不存在"
        if task is not None:
            node.task = task
        if routes_mode is not None:
            node.routes = routes_mode
        self._emit_graph_event(parent, f, "update", node_id)
        return f"节点 {node_id} 已更新（下次激活生效）"

    def add_flow_edge(self, parent: CoaraBase, *, flow: str, frm: str, to: str, on: str = "success") -> str:
        """运行中加边（新路由从此刻的投递开始生效）。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        if frm not in f.graph.nodes:
            return f"节点 {frm} 不存在"
        if to not in f.graph.nodes:
            return f"节点 {to} 不存在"
        try:
            f.graph.add_edge(frm, to, on=on)
        except ValueError as exc:
            return str(exc)
        f.engine.refresh(f.graph)
        self._emit_graph_event(parent, f, "add_edge", frm)
        return f"边已添加：{frm} → {to} (on={on})"

    def remove_flow_edge(self, parent: CoaraBase, *, flow: str, frm: str, to: str, on: str = "success") -> str:
        """运行中删边。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        if f.graph.remove_edge(frm, to, on):
            f.engine.refresh(f.graph)
            self._emit_graph_event(parent, f, "remove_edge", frm)
            return f"边已删除：{frm} → {to} (on={on})"
        return f"边不存在：{frm} → {to} (on={on})"

    def remove_flow_node(self, parent: CoaraBase, *, flow: str, node_id: str) -> str:
        """删未激活节点（已激活的节点是实录，不可删）。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        state = f.states.get(node_id)
        if state is not None and state.activations > 0:
            return f"节点 {node_id} 已运行过（实录），不可删除"
        f.graph.remove_node(node_id)
        f.states.pop(node_id, None)
        f.engine.refresh(f.graph)
        self._emit_graph_event(parent, f, "remove_node", node_id)
        return f"节点 {node_id} 已删除"

    # ── 运行 ────────────────────────────────────────────────────────────

    def snapshot(self, flow: str) -> dict[str, Any] | None:
        """flow 图全量快照（供 WebUI 实时视图初始化 / 增量校准）。"""
        f = self._flows.get(flow)
        if f is None:
            return None
        g = f.graph
        nodes: list[dict[str, Any]] = []
        for nid, node in g.nodes.items():
            state = f.states.get(nid)
            nodes.append(
                {
                    "id": nid,
                    "status": state.status if state else "pending",
                    "task": node.task[:200],
                    "result": (state.result if state else ""),
                    "activations": state.activations if state else 0,
                    "agent_id": f"sa-flow-{nid}",
                    "routes": node.routes,
                    **({"error": state.error} if state and state.error else {}),
                }
            )
        edges = [{"from": e.frm, "to": e.to, "on": e.on} for e in g.edges]
        return {
            "name": g.name,
            "schedule": f.schedule,
            "hops": f.hops,
            "nodes": nodes,
            "edges": edges,
            "wdl": emit_graph(g),
        }

    def _emit_graph_event(self, parent: Any, f: Flow, action: str, node_id: str) -> None:
        if parent is None or getattr(parent, "_emit_trace", None) is None:
            return
        node = f.graph.nodes.get(node_id)
        payload: dict[str, Any] = {
            "flow": f.graph.name,
            "action": action,
            "node_id": node_id,
            # 主体标记（"root" / "flow"）：WebUI 据此把图事件路由到对应画布。
            "subject": getattr(parent, "flow_subject", "root"),
        }
        if node is not None:
            payload["depends_on"] = f.graph.depends_on(node_id)
            payload["routes_to"] = f.graph.routes_to(node_id)
            payload["status"] = f.states.get(node_id).status if f.states.get(node_id) else "pending"
        # 全图 WDL：工作台编辑器实时灌入同一份草案视图（与 export/save 同源）。
        with contextlib.suppress(Exception):
            payload["wdl"] = emit_graph(f.graph)
        parent._emit_trace("flow_graph_changed", f"flow {f.graph.name}：{action} {node_id}", payload=payload)

    async def reset(self) -> None:
        """释放全部 flow：取消运行中的节点、shutdown 节点子智能体、清空图。"""
        self._epoch += 1  # 新世代：旧世代迟到回调一律放弃
        flows = list(self._flows.values())
        self._flows.clear()
        running: list[asyncio.Task] = []
        coaras: list[Any] = []
        for f in flows:
            # 移除前先放行 wait() 等待者，避免停在 f.done.wait() 的协程挂死
            f.done.set()
            for state in f.states.values():
                task = state.running_task
                if task is not None and not task.done():
                    with contextlib.suppress(Exception):
                        task.cancel()
                    running.append(task)
                if state.coara is not None:
                    coaras.append(state.coara)
        if running:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(*running, return_exceptions=True),
                    timeout=2.0,
                )
        for coara in coaras:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(coara.shutdown(), timeout=1.0)

    def run(self, flow: str) -> str:
        """显式启动所有就绪节点（引擎判定就绪）。唯一点火源。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        if not f.graph.nodes:
            return f"flow {flow} 还没有节点"
        started: list[str] = []
        for nid in f.graph.nodes:
            state = f.states.get(nid)
            if (state and state.status == "running") or not f.engine.is_ready(nid):
                continue
            if self._launch(f, nid):
                started.append(nid)
        if not started:
            return f"flow {flow}：没有就绪节点（依赖未满足或已启动）"
        return f"flow {flow}：已启动 {len(started)} 个节点：{'、'.join(started)}"

    # ── 调试：重置 / 重跑 / 续跑 ─────────────────────────────────────────

    def _downstream_closure(self, f: Flow, node_id: str) -> list[str]:
        """节点全部下游（沿出边 BFS，环安全，不含自身）。"""
        seen: set[str] = set()
        stack = [e.to for e in f.graph.out_edges(node_id)]
        while stack:
            cur = stack.pop()
            if cur in seen or cur == node_id:
                continue
            seen.add(cur)
            stack.extend(e.to for e in f.graph.out_edges(cur))
        return list(seen)

    async def reset_flow_nodes(self, parent: Any, *, flow: str, node_ids: list[str]) -> str:
        """调试重置：节点清回 pending、引擎簿记清零，未重置的 wait 上游最近结果
        重新投递（保证重置节点可再次就绪）。running 节点拒绝重置。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        for nid in node_ids:
            if nid not in f.graph.nodes:
                return f"节点 {nid} 不在 flow {flow} 中"
        target_set = set(node_ids)
        for nid in node_ids:
            if f.state(nid).status == "running":
                return f"节点 {nid} 正在运行，先等完成或中断后再重置"
        shut: list[Any] = []
        for nid in node_ids:
            state = f.state(nid)
            state.status = "pending"
            state.activations = 0
            state.result = ""
            state.error = ""
            state.received = []
            state.next_hint = None
            if state.coara is not None:
                shut.append(state.coara)
                state.coara = None
            f.engine.reset_node(nid)
            self._emit_graph_event(parent, f, "reset", nid)
        # 未重置的 wait 上游：最近结果重新投递，维持首激活就绪语义
        cls = classify_edges(f.graph)
        for nid in node_ids:
            for e in f.graph.in_edges(nid):
                if cls.get(e.key()) != "wait" or e.on != "success" or e.frm in target_set:
                    continue
                up = f.states.get(e.frm)
                if up is not None and up.result:
                    f.engine.record_arrival(e.frm, nid, up.result)
        for coara in shut:
            with contextlib.suppress(Exception):
                await coara.shutdown()
        return f"已重置节点：{'、'.join(node_ids)}（可 run 点火重跑）"

    async def rerun_node(self, parent: Any, *, flow: str, node_id: str, cascade: bool = False) -> str:
        """单节点重跑：重置后点火；cascade=True 连同全部下游一起重置重跑。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        if node_id not in f.graph.nodes:
            return f"节点 {node_id} 不在 flow {flow} 中"
        targets = [node_id] + (self._downstream_closure(f, node_id) if cascade else [])
        msg = await self.reset_flow_nodes(parent, flow=flow, node_ids=targets)
        if msg.startswith("已重置"):
            return msg + "；" + self.run(flow)
        return msg

    async def resume_flow(self, parent: Any, *, flow: str) -> str:
        """断点续跑：失败节点（连同下游）重置后点火，未完成节点继续推进。"""
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        failed = [nid for nid, st in f.states.items() if st.status == "failed"]
        if failed:
            closure: list[str] = []
            seen: set[str] = set()
            for nid in failed:
                if nid not in seen:
                    seen.add(nid)
                    closure.append(nid)
                for dep in self._downstream_closure(f, nid):
                    if dep not in seen:
                        seen.add(dep)
                        closure.append(dep)
            msg = await self.reset_flow_nodes(parent, flow=flow, node_ids=closure)
            if not msg.startswith("已重置"):
                return msg
        return self.run(flow)

    async def _build_subagent(
        self,
        parent: CoaraBase,
        node_id: str,
        *,
        subagent_type: str = "coaras",
        provider: str = "",
        model: str = "",
    ) -> CoaraBase:
        from src.coara.base import CoaraBase as Base
        from src.coara.builtin_agents import get_subagent
        from src.core.types import CoaraPersona
        from src.tools.builtin.delegate.delegate import delegate_system_prompt

        config = get_subagent(subagent_type)
        if config is None:
            raise ValueError(f"未知子智能体类型：{subagent_type}（仅支持内置类型）")
        # 工作流节点有独立 provider（节点级 > 图级 > workflow.node profile 链路，
        # 调用方已把节点/图级合并进 provider/model）：不继承主会话当前模型，
        # /model 切换不影响工作流节点。
        from src.workflow.node_llm import resolve_workflow_node_llm

        node_provider, node_model = resolve_workflow_node_llm(provider, model)
        subagent = Base(
            name=f"sa-flow-{node_id}",
            persona=CoaraPersona(
                name=config.name,
                role=config.role,
                system_prompt_template=delegate_system_prompt(config.system_prompt, channel=False),
                yaml_config=config.yaml_config,
            ),
            workspace_dir=Path(parent.workspace_dir),
            provider_name=node_provider,
            model=node_model,
            delegate_depth=parent.delegate_depth + 1,
            audit_session_id=parent.audit_session_id,
            max_tool_iterations=_SUBAGENT_MAX_TOOL_ITERATIONS,
            user_facing=False,
            is_owner_context=False,
            session_tape="flow",
        )
        parent_bound = parent._tool_manager.get_bound_tool_names()
        if parent_bound:
            whitelist = set(parent_bound)
        else:
            whitelist = set(parent._tool_manager.get_visible_tool_names(parent.identity.is_owner_context))
        whitelist.add("deliver")
        subagent._tool_manager.set_whitelist(whitelist)
        await subagent.bootstrap_tools()
        from src.tools.builtin.communication.deliver import DeliverTool

        subagent.register_tool(DeliverTool(parent_coara=subagent))
        await subagent.load_skills()
        await subagent.initialize()
        return subagent

    def _maybe_start(self, f: Flow, node_id: str, epoch: int | None = None) -> None:
        if epoch is not None and epoch != self._epoch:
            return  # reset 后迟到的重试回调：旧世代一律放弃
        state = f.states.get(node_id)
        if state is None or state.status == "running":
            return
        if state.running_task is not None and not state.running_task.done():
            snap = self._epoch  # 快照世代：回调发射时比对
            state.running_task.add_done_callback(lambda _t: self._maybe_start(f, node_id, snap))
            return
        if not f.engine.is_ready(node_id):
            return
        self._launch(f, node_id)

    def _launch(self, f: Flow, node_id: str) -> bool:
        """就绪则消费一轮到达并启动节点；触顶标失败。"""
        activation = f.engine.consume(node_id)
        if activation is None:
            state = f.states.get(node_id)
            if state is not None and not f.engine.can_activate(node_id) and state.status == "pending":
                state.status = "failed"
                state.error = "激活次数触顶（max_activations）"
                self._emit_node_event(
                    state.parent,
                    "subagent_failed",
                    f"Flow 节点失败：{node_id}",
                    flow_name=f.graph.name,
                    node_id=node_id,
                    state=state,
                    error=state.error,
                )
                self._check_complete(f)
            return False
        state = f.state(node_id)
        state.status = "running"
        state.activations = activation.round_index
        state.received = [text for _frm, text in activation.arrivals]
        state.next_hint = None
        # 任务必须落引用，否则 fire-and-forget 协程会被 GC 回收
        state.running_task = asyncio.create_task(self._run_node(f, node_id, epoch=self._epoch))
        return True

    def _render_input(self, f: Flow, node: Node) -> str:
        """渲染 input 模板：{{steps.x.text}} ← 最近结果（节点自带输入接口，无图级 inputs）。"""
        text = node.input or ""
        for ref in template_step_refs(text):
            state = f.states.get(ref)
            value = (state.result if state else "") or ""
            text = re.sub(r"\{\{\s*steps\." + re.escape(ref) + r"\.\w+\s*\}\}", value, text)
        return text

    def _build_prompt(self, f: Flow, node_id: str) -> str:
        node = f.graph.nodes[node_id]
        state = f.state(node_id)
        parts: list[str] = [node.task]
        inputs: list[str] = []
        rendered = self._render_input(f, node).strip()
        if rendered:
            inputs.append(rendered)
        inputs.extend(state.received)
        if inputs:
            parts.append("\n\n".join(f"【输入】\n{x}" for x in inputs))
        if node.routes == "one":
            choices = "、".join(f.graph.routes_to(node_id)) or "（无）"
            parts.append(
                f"你的可选后继节点：{choices}。完成后必须用 "
                f"deliver(message=你的结果, next=选中的后继 node_id) 交付，"
                f"从上述可选后继中选一个。"
            )
        else:
            parts.append("直接输出你的最终结果，不要客套。")
        return "\n\n".join(parts)

    def _emit_flow_event(self, parent: Any, event_type: str, message: str, *, flow_name: str) -> None:
        if parent is None or getattr(parent, "_emit_trace", None) is None:
            return
        parent._emit_trace(event_type, message, payload={"flow": flow_name})

    def _emit_node_event(
        self,
        parent: Any,
        event_type: str,
        message: str,
        *,
        flow_name: str,
        node_id: str,
        state: NodeState | None = None,
        error: str | None = None,
        status: str = "",
    ) -> None:
        if parent is None or getattr(parent, "_emit_trace", None) is None:
            return
        payload: dict[str, Any] = {
            "subagent_type": "cooras",
            "subagent_id": f"flow-{flow_name}-{node_id}",
            "description": f"flow {flow_name} · {node_id}",
            "parent_activity_id": f"flow-{flow_name}",
            "status": status,
        }
        subagent = state.coara if state else None
        if subagent is not None:
            child_session_id = getattr(subagent, "session_id", None) or ""
            child_coara_id = getattr(getattr(subagent, "identity", None), "coara_id", None) or ""
            if child_session_id:
                payload["child_session_id"] = child_session_id
            if child_coara_id:
                payload["child_coara_id"] = child_coara_id
        if error:
            payload["error"] = error
        parent._emit_trace(event_type, message, payload=payload)

    async def _run_node(self, f: Flow, node_id: str, epoch: int | None = None) -> None:
        state = f.state(node_id)
        subagent = state.coara
        parent = state.parent or f.parent
        if subagent is None and parent is not None:
            # 懒建：load/重建恢复的图只有结构没有停车 coaras，点火时现场建身
            try:
                from src.workflow.node_llm import merge_node_llm

                node = f.graph.nodes.get(node_id)
                _np, _nm = merge_node_llm(
                    node.provider if node else "",
                    node.model if node else "",
                    f.graph.provider,
                    f.graph.model,
                )
                _llm_override = {k: v for k, v in (("provider", _np), ("model", _nm)) if v}
                subagent = await self._build_subagent(parent, node_id, **_llm_override)
                state.coara = subagent
                state.parent = parent
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"flow 节点 {node_id} 子智能体构建失败: {exc}")
                subagent = None
        if subagent is None:
            state.status = "failed"
            state.error = "节点子智能体未构建（flow 已释放或仅 load 恢复）"
            state.result = f"[节点 {node_id} 失败] 子智能体未构建"
            self._emit_node_event(
                state.parent,
                "subagent_failed",
                f"Flow 节点失败：{node_id}",
                flow_name=f.graph.name,
                node_id=node_id,
                state=state,
                error=state.error,
            )
            self._on_node_done(f, node_id, epoch=epoch)
            return
        subagent._final_deliver_message = None
        subagent._next_node = None
        prompt = self._build_prompt(f, node_id)
        last_chunk = ""
        self._emit_node_event(
            state.parent,
            "subagent_start",
            f"Flow 节点启动：{node_id}",
            flow_name=f.graph.name,
            node_id=node_id,
            state=state,
        )
        try:
            from src.coara.turn_source import current_turn_source

            parent = state.parent
            node_source = current_turn_source(parent) if parent is not None else "cli-attached"
            async for chunk in subagent.process_message(prompt, source=node_source):
                if isinstance(chunk, str):
                    last_chunk = chunk
            state.result = getattr(subagent, "_final_deliver_message", None) or self._extract_final(
                subagent, last_chunk
            )
            state.next_hint = getattr(subagent, "_next_node", None)
            state.status = "done"
            self._emit_node_event(
                state.parent,
                "subagent_complete",
                f"Flow 节点完成：{node_id}",
                flow_name=f.graph.name,
                node_id=node_id,
                state=state,
            )
        except asyncio.CancelledError:
            state.status = "failed"
            state.error = "cancelled"
            state.result = f"[节点 {node_id} 被取消]"
            self._emit_node_event(
                state.parent,
                "subagent_failed",
                f"Flow 节点取消：{node_id}",
                flow_name=f.graph.name,
                node_id=node_id,
                state=state,
                error="cancelled",
            )
        except Exception as exc:  # noqa: BLE001
            state.status = "failed"
            state.error = str(exc)
            state.result = f"[节点 {node_id} 失败] {exc}"
            logger.warning(f"flow 节点 {node_id} 失败: {exc}")
            self._emit_node_event(
                state.parent,
                "subagent_failed",
                f"Flow 节点失败：{node_id}",
                flow_name=f.graph.name,
                node_id=node_id,
                state=state,
                error=str(exc),
            )
        finally:
            with contextlib.suppress(Exception):
                await subagent.shutdown()
            # 置空停车引用：节点重复激活时走懒建新建，避免复用已 shutdown
            # 实例（message_history 跨激活残留、状态 TERMINATED）
            if state.coara is subagent:
                state.coara = None
            self._on_node_done(f, node_id, epoch=epoch)

    def _on_node_done(self, f: Flow, node_id: str, epoch: int | None = None) -> None:
        # reset（/new）后旧 flow 已释放：运行中节点的 finally 仍会触发本回调，
        # 世代失配或 flow 易主即放弃——不得再为下游创建任务或在已 shutdown 的 coara 上推进
        if epoch is not None and epoch != self._epoch:
            return
        if self._flows.get(f.graph.name) is not f:
            return
        f.hops += 1
        if f.hops > f.graph.max_activations:
            logger.warning(f"flow {f.graph.name} 超出护栏（{f.graph.max_activations}），终止")
            self._fail_pending(f)
            self._finish(f)
            return
        state = f.state(node_id)
        node = f.graph.nodes.get(node_id)
        if node is None:  # 运行中被删（不应发生，防御）
            self._check_complete(f)
            return
        out_edges = f.graph.out_edges(node_id)
        if state.status == "failed":
            chosen = [e for e in out_edges if e.on == "error"]
        elif node.routes == "all":
            chosen = [e for e in out_edges if e.on == "success"]
        else:  # one
            hint = state.next_hint
            chosen = []
            if hint:
                chosen = [e for e in out_edges if e.on == "success" and e.to == hint]
                if not chosen:
                    logger.warning(f"flow 节点 {node_id} routes=one 但 next={hint!r} 不在出边目标中")
        for e in chosen:
            f.engine.record_arrival(e.frm, e.to, state.result, on=e.on)
        for e in chosen:
            self._maybe_start(f, e.to)
        self._check_complete(f)

    def _check_complete(self, f: Flow) -> None:
        if any(st.status == "running" for st in f.states.values()):
            return
        # 有就绪节点（可能来自悬空边落地）→ 级联启动全部就绪节点。
        # 不能只启动一个就 return：多个独立就绪节点同时存在时漏启动的
        # 会在 wait 的 _fail_pending 里被误标失败
        for nid in f.graph.nodes:
            state = f.states.get(nid)
            if state and state.status == "running":
                continue
            if f.engine.is_ready(nid):
                self._launch(f, nid)
        if f.graph.nodes and all(
            (f.states.get(nid) is None or f.states[nid].status in ("done", "failed")) for nid in f.graph.nodes
        ):
            self._finish(f)

    def _fail_unresolvable(self, f: Flow) -> None:
        """依赖的源节点从未 spawn（wait 边悬空）→ 置失败，避免 wait 挂死。"""
        for nid in f.graph.nodes:
            state = f.states.get(nid)
            if state is None or state.status != "pending":
                continue
            missing = [d for d in f.graph.depends_on(nid) if d not in f.graph.nodes]
            if missing:
                state.status = "failed"
                state.error = f"依赖节点未 spawn：{missing}"
                state.result = f"[节点 {nid} 失败] 依赖缺失 {missing}"

    def _fail_pending(self, f: Flow) -> None:
        """无运行中节点且仍有 pending → 依赖永远无法满足，标记失败。"""
        for nid in f.graph.nodes:
            state = f.states.get(nid)
            if state is None:
                continue
            if state.status == "pending":
                state.status = "failed"
                state.error = "依赖未满足（上游未路由到本节点或依赖缺失）"
                state.result = f"[节点 {nid} 失败] 依赖未满足"

    def _finish(self, f: Flow) -> None:
        if not f.done.is_set():
            f.done.set()
        parent = next((st.parent for st in f.states.values() if st.parent is not None), None)
        if parent is not None:
            self._emit_flow_event(parent, "flow_finished", f"flow {f.graph.name} 完成", flow_name=f.graph.name)

    async def wait(self, flow: str) -> str:
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        if not f.graph.nodes:
            return self.status(flow)
        self._fail_unresolvable(f)
        # 清理悬空边（端点未 spawn 且从未落地）：wait 即 spawn 收官，边与其
        # 缓冲到达一并作废——「就绪未启动」早退分支前同样生效，不残留半初始化态
        g = f.graph
        dangling = [e.key() for e in g.edges if e.frm not in g.nodes or e.to not in g.nodes]
        g.edges = [e for e in g.edges if e.frm in g.nodes and e.to in g.nodes]
        f.engine.refresh(g)
        f.engine.drop_arrivals(dangling)
        if not any(st.status == "running" for st in f.states.values()):
            parked_ready = [
                nid
                for nid in g.nodes
                if f.states.get(nid) is None or (f.states[nid].status == "pending" and f.engine.is_ready(nid))
            ]
            activated_any = any(st.activations > 0 for st in f.states.values())
            if parked_ready and not activated_any:
                return (
                    f"flow {flow}：{len(parked_ready)} 个节点已就绪但未启动"
                    f'（{"、".join(sorted(parked_ready))}），请先 orchestrator(action="run", flow="{flow}")'
                )
            self._fail_pending(f)
        if g.nodes and all(
            (f.states.get(nid) is None or f.states[nid].status in ("done", "failed")) for nid in g.nodes
        ):
            return self.status(flow)
        await f.done.wait()
        return self.status(flow)

    def status(self, flow: str) -> str:
        f = self._flows.get(flow)
        if f is None:
            return f"flow {flow} 不存在"
        marks = {"done": "✓", "failed": "✗", "running": "…", "pending": "○"}
        lines = [f"flow {f.graph.name}：{f.hops} 次节点执行（护栏 {f.graph.max_activations}）", ""]
        for nid in f.graph.nodes:
            state = f.states.get(nid)
            st = state.status if state else "pending"
            line = f"{marks.get(st, '?')} {nid} [{st}]"
            if state and state.activations > 1:
                line += f"（第 {state.activations} 轮）"
            if state and st == "done" and state.result:
                line += f"：{state.result[:120]}"
            elif state and state.error:
                line += f"：{state.error}"
            lines.append(line.replace("\n", " "))
        return "\n".join(lines)

    # ── 定稿投影 ────────────────────────────────────────────────────────

    def export_wdl(self, flow: str) -> str:
        """固化定稿：内核图 canonical 投影（round-trip 恒等）。"""
        f = self._flows.get(flow)
        if f is None:
            raise ValueError(f"flow {flow} 不存在")
        return emit_graph(f.graph)

    def dump(self, flow: str) -> dict[str, Any] | None:
        """序列化图结构 + 节点态（不含运行中任务），供落盘/导出。"""
        f = self._flows.get(flow)
        if f is None:
            return None
        return {
            "name": f.graph.name,
            "hops": f.hops,
            "graph": emit_graph(f.graph),
            "states": {
                nid: {
                    "status": st.status,
                    "activations": st.activations,
                    "result": st.result,
                    "error": st.error,
                }
                for nid, st in f.states.items()
            },
        }

    def load(self, data: dict[str, Any]) -> str:
        """从 dump 恢复（结构与结果；运行中节点不续跑，统一标失败）。"""
        from src.workflow.core import parse_graph

        if data.get("graph"):
            graph = parse_graph(str(data["graph"]))
            f = Flow(graph.name, max_activations=graph.max_activations)
            f.graph = graph
            f.engine = ActivationEngine(graph)
            f.hops = int(data.get("hops", 0))
            for nid, sd in (data.get("states") or {}).items():
                if nid not in graph.nodes:
                    continue
                state = f.state(nid)
                state.status = str(sd.get("status", "done"))
                state.activations = int(sd.get("activations", 0))
                state.result = str(sd.get("result", ""))
                state.error = str(sd.get("error", ""))
                if state.status not in ("done", "failed"):
                    state.status = "failed"
                    state.error = "加载的 flow 不支持继续运行（子智能体未恢复），节点已标记失败"
                    state.result = f"[节点 {nid} 失败] 加载的 flow 不支持续跑"
            self._flows[graph.name] = f
            return graph.name
        # 旧格式迁移（v8 内核化之前的 dump）
        return self._load_legacy(data)

    def _load_legacy(self, data: dict[str, Any]) -> str:
        name = str(data.get("name", ""))
        graph = FlowGraph(name=name, max_activations=int(data.get("max_hops", 100)))
        for nid, nd in (data.get("nodes") or {}).items():
            graph.add_node(
                Node(
                    id=nid,
                    task=str(nd.get("task", "")),
                    input=str(nd.get("input", "")),
                    routes=str(nd.get("routes_mode", "all")),
                )
            )
        for nid, nd in (data.get("nodes") or {}).items():
            for dep in nd.get("depends_on") or []:
                with contextlib.suppress(ValueError):
                    graph.add_edge(dep, nid)
            for tgt in nd.get("routes_to") or []:
                with contextlib.suppress(ValueError):
                    graph.add_edge(nid, tgt)
        f = Flow(name)
        f.graph = graph
        f.engine = ActivationEngine(graph)
        f.hops = int(data.get("hops", 0))
        for nid, nd in (data.get("nodes") or {}).items():
            state = f.state(nid)
            state.status = str(nd.get("status", "done"))
            state.result = str(nd.get("result", ""))
            state.error = str(nd.get("error", ""))
            if state.status not in ("done", "failed"):
                state.status = "failed"
                state.error = "加载的 flow 不支持继续运行（子智能体未恢复），节点已标记失败"
                state.result = f"[节点 {nid} 失败] 加载的 flow 不支持续跑"
        self._flows[name] = f
        return name

    @staticmethod
    def _extract_final(subagent: CoaraBase, fallback: str) -> str:
        history = subagent.message_history
        last_user = max((i for i, m in enumerate(history) if m.role == MessageRole.USER), default=-1)
        for message in reversed(history[last_user + 1 :]):
            if message.role != MessageRole.ASSISTANT or message.tool_calls:
                continue
            if isinstance(message.content, str) and message.content.strip():
                return message.content
        return fallback


_coordinator: FlowCoordinator | None = None


def get_flow_coordinator() -> FlowCoordinator:
    """进程级兜底单例：仅用于无父实例的调用（注册表展示等）。
    会话内编排一律走 ``CoaraBase.flow_coordinator``（实例级，主体隔离）。
    """
    global _coordinator
    if _coordinator is None:
        _coordinator = FlowCoordinator()
    return _coordinator
