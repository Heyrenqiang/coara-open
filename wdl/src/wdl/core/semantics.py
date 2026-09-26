"""内核语义 — 校验、模板依赖推导、就绪计算与多激活判定（内外宿主共用）。

激活语义（图可含回边，非 DAG，数据流经典 back-edge 处理）：

- 边分两类（DFS 从源节点出发，确定性邻序判定）：
  * wait 边：DFS 树上前向/交叉边。on=success 的 wait 边是每轮激活的
    必需等待——每条 wait-success 边各一个到达才就绪。
  * kick 边：回边（成环）或 on=error 的边。不参与就绪判定；上游完成
    （失败走 error 边）时投递到达，作为机会性触发把节点再次激活。
- 首激活：必需等待为空的节点（源节点或环入口）立即可激活。
- 再激活：必需等待全部有到达，且至少有一条未消费到达。
- 消费：从每条有未消费到达的入边各取一个（FIFO），多余到达留给下轮。
- 每节点激活次数受上限约束（节点级 max_activations 覆写全局），
  触顶即失败终态——循环边界显式、确定、可终止。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wdl.core.model import EDGE_KINDS, ROUTE_MODES, FlowGraph, validate_identifier

# 模板引用：{{steps.y.text}}（unicode 词字符，中文可用）
_STEP_REF_RE = re.compile(r"\{\{\s*steps\.([\w-]+)\.\w+\s*\}\}")


@dataclass(slots=True)
class Issue:
    """校验问题：error 阻断运行/定稿；warning 仅提示。"""

    level: str  # error | warning
    message: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"[{self.level}] {self.message}"


def validate_graph(graph: FlowGraph) -> list[Issue]:
    """结构校验：id 合法、边引用存在、路由模式与边触发合法、上限为正。"""
    issues: list[Issue] = []
    try:
        validate_identifier(graph.name, "工作流名")
    except ValueError as exc:
        issues.append(Issue("error", str(exc)))
    if not graph.nodes:
        issues.append(Issue("error", "图没有任何节点"))
    for nid, node in graph.nodes.items():
        try:
            validate_identifier(nid, "节点名")
        except ValueError as exc:
            issues.append(Issue("error", str(exc)))
        if node.routes not in ROUTE_MODES:
            issues.append(Issue("error", f"节点 {nid} routes 非法：{node.routes}（all | one）"))
        if node.max_activations is not None and node.max_activations <= 0:
            issues.append(Issue("error", f"节点 {nid} max_activations 需为正整数"))
        if not node.task:
            issues.append(Issue("warning", f"节点 {nid} 缺少任务指令（task）"))
    seen_edge_keys: set[tuple[str, str, str]] = set()
    for e in graph.edges:
        if e.frm not in graph.nodes:
            issues.append(Issue("error", f"边引用不存在的源节点：{e.frm}"))
        if e.to not in graph.nodes:
            issues.append(Issue("error", f"边引用不存在的目标节点：{e.to}"))
        if e.on not in EDGE_KINDS:
            issues.append(Issue("error", f"边 {e.frm}→{e.to} 触发条件非法：{e.on}"))
        key = e.key()
        if key in seen_edge_keys:
            issues.append(Issue("error", f"重复边：{e.frm} → {e.to} (on={e.on})"))
        seen_edge_keys.add(key)
    # 隐式依赖（模板引用）合法性：引用的上游必须存在
    for nid, node in graph.nodes.items():
        for ref in template_step_refs(node.input):
            if ref == nid:
                issues.append(Issue("error", f"节点 {nid} 模板引用自身"))
            elif ref not in graph.nodes:
                issues.append(Issue("warning", f"节点 {nid} 模板引用不存在的节点：{ref}"))
    if graph.max_activations <= 0:
        issues.append(Issue("error", "max_activations 需为正整数"))
    return issues


def template_step_refs(text: str) -> list[str]:
    return list(dict.fromkeys(_STEP_REF_RE.findall(text or "")))


def implicit_dependencies(graph: FlowGraph) -> dict[str, list[str]]:
    """每个节点的隐式依赖（来自 input 模板引用，未显式建边的）。"""
    result: dict[str, list[str]] = {}
    for nid, node in graph.nodes.items():
        explicit = set(graph.depends_on(nid))
        refs = [r for r in template_step_refs(node.input) if r not in explicit]
        if refs:
            result[nid] = refs
    return result


# ── 边分类：wait / kick ────────────────────────────────────────────────


def classify_edges(graph: FlowGraph) -> dict[tuple[str, str, str], str]:
    """DFS 判回边：从源节点可达的环，回到栈上祖先的边是 kick（回边）；
    on=error 恒为 kick。

    纯环（无源节点不可达）不兜底：全部保持 wait，运行期无入口可激活，
    由宿主按死锁显式失败——避免"声明了等待却凭空触发"。

    确定性：源选择与邻接序均按 (id / to / on) 排序。
    """
    classification: dict[tuple[str, str, str], str] = {}
    if not graph.nodes:
        return classification
    adj: dict[str, list[tuple[str, str]]] = {
        nid: sorted((e.to, e.on) for e in graph.out_edges(nid)) for nid in graph.nodes
    }
    color: dict[str, int] = dict.fromkeys(graph.nodes, 0)  # 0 白 1 灰 2 黑

    def dfs(root: str) -> None:
        stack: list[tuple[str, int]] = [(root, 0)]
        color[root] = 1
        while stack:
            node, idx = stack[-1]
            neighbors = adj.get(node, [])
            if idx < len(neighbors):
                stack[-1] = (node, idx + 1)
                nxt, on = neighbors[idx]
                for e in graph.out_edges(node):
                    if e.to != nxt:
                        continue
                    if e.on == "error" or color.get(nxt, 0) == 1:
                        classification[e.key()] = "kick"
                    else:
                        classification[e.key()] = "wait"
                if color.get(nxt, 0) == 0:
                    color[nxt] = 1
                    stack.append((nxt, 0))
            else:
                color[node] = 2
                stack.pop()

    for s in graph.source_ids():
        if color[s] == 0:
            dfs(s)
    for e in graph.edges:
        if e.key() not in classification:
            classification[e.key()] = "wait" if e.on == "success" else "kick"
    return classification


# ── 多激活运行语义 ──────────────────────────────────────────────────────


@dataclass(slots=True)
class Activation:
    """一次节点激活的输入语境。"""

    node_id: str
    round_index: int  # 第几次激活（1 起）
    arrivals: list[tuple[str, str]]  # 本轮到达：(from, result)；error 到达带 [错误] 前缀


class ActivationEngine:
    """纯函数式激活簿记：宿主喂入到达与消费，引擎判定就绪与上限。

    宿主职责：节点执行、结果模板渲染、激活时构建提示词。
    引擎职责：到达登记、wait/kick 分类、就绪判定、激活上限裁决。
    """

    def __init__(self, graph: FlowGraph) -> None:
        self._graph = graph
        self._cls = classify_edges(graph)
        # 边 key → 到达 FIFO 队列
        self._queues: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
        # node_id → 已激活次数
        self._activations: dict[str, int] = {}

    # 图被编辑后刷新分类（随时调整支持）
    def refresh(self, graph: FlowGraph) -> None:
        self._graph = graph
        self._cls = classify_edges(graph)

    def reset_node(self, node_id: str) -> None:
        """调试重置：清该节点的激活计数与入边到达队列（状态清回 pending 由宿主负责）。"""
        self._activations.pop(node_id, None)
        for e in self._graph.in_edges(node_id):
            self._queues.pop(e.key(), None)

    def drop_arrivals(self, keys: list[tuple[str, str, str]]) -> None:
        """丢弃指定边的缓冲到达（wait 清理悬空边时同步作废，防残留到达复活）。"""
        for k in keys:
            self._queues.pop(k, None)

    def _required(self, node_id: str) -> list[tuple[str, str, str]]:
        return [
            key
            for key in (e.key() for e in self._graph.in_edges(node_id))
            if self._cls.get(key, "wait") == "wait" and key[2] == "success"
        ]

    def _queue(self, key: tuple[str, str, str]) -> list[tuple[str, str]]:
        return self._queues.setdefault(key, [])

    def activations_of(self, node_id: str) -> int:
        return self._activations.get(node_id, 0)

    def cap_of(self, node_id: str) -> int:
        node = self._graph.nodes.get(node_id)
        if node is None:
            return self._graph.max_activations
        return node.effective_max_activations(self._graph.max_activations)

    def record_arrival(self, frm: str, to: str, result_text: str, on: str = "success") -> None:
        """登记一次到达（success/error 投递都进队列，error 带 [错误] 前缀）。"""
        text = f"[错误] {result_text}" if on == "error" else result_text
        self._queue((frm, to, on)).append((frm, text))

    def pending_total(self, node_id: str) -> int:
        return sum(len(self._queue(e.key())) for e in self._graph.in_edges(node_id))

    def is_ready(self, node_id: str) -> bool:
        if self._graph.nodes.get(node_id) is None:
            return False
        if not self.can_activate(node_id):
            return False
        if self._activations.get(node_id, 0) == 0:
            # 首激活：全部必需等待边各有一个到达（源节点/环入口等待集为空）
            return all(len(self._queue(k)) > 0 for k in self._required(node_id))
        # 再激活：任一入边有未消费到达（kick 回边/重试到达即触发）
        return self.pending_total(node_id) > 0

    def can_activate(self, node_id: str) -> bool:
        return self.activations_of(node_id) < self.cap_of(node_id)

    def consume(self, node_id: str) -> Activation | None:
        """就绪则消费一轮到达，返回本次激活；未就绪或触顶返回 None。"""
        if not self.is_ready(node_id):
            return None
        self._activations[node_id] = self._activations.get(node_id, 0) + 1
        arrivals: list[tuple[str, str]] = []
        for e in self._graph.in_edges(node_id):
            q = self._queue(e.key())
            if q:
                arrivals.append(q.pop(0))
        # 首激活且无任何到达（源节点）：arrivals 为空是合法的
        return Activation(
            node_id=node_id,
            round_index=self._activations[node_id],
            arrivals=arrivals,
        )

    def snapshot(self) -> dict[str, Any]:
        """簿记快照（调试/持久化投影用）。"""
        return {
            "activations": dict(self._activations),
            "queues": {str(k): list(v) for k, v in self._queues.items()},
        }
