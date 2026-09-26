"""内核图模型 — 节点 + 边，控制流全部塌缩为图拓扑。

设计约定（唯一性根基）：

- 节点只有一种：智能体节点。创建即有身份（id 即名字）与任务（task）。
  审批等待、确定性调用都是智能体节点内部的行为，不是图的节点类型。
- 边是唯一拓扑存储：依赖（depends_on）与路由（routes_to）都是从边集
  派生的只读视图，任何宿主不得独立指定，避免同一图有两种权威写法。
- 控制流全部是图的形状：并行=扇出、汇聚=扇入、分支=one 路由、
  循环=回边、重试=error 边回指。图上没有 if/for/while/merge 节点。
- 路由模式挂在源节点上：出边集合要么 all（全部投递）要么 one（每次
  激活只选一条，交互宿主由节点运行期挑，无人值守宿主由边上静态条件挑）。
- 数据流用最小模板表达在 node.input（{{steps.y.text}}），模板引用即隐式依赖
  （校验时推导），不设独立 data 边。
- 循环合法性不由图校验裁决，由激活上限兜底（全局 max_activations，
  节点可覆写）——图允许任意回边，运行保证终止。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 源节点出边路由模式
ROUTE_MODES = frozenset({"all", "one"})

# 边触发条件：success（正常完成投递）| error（失败时投递，重试路由）
EDGE_KINDS = frozenset({"success", "error"})

# flow 名 / node_id：unicode 词字符（中文可用），拒绝空白与路径危险字符
_NAME_RE = re.compile(r"^[\w-]+$", re.UNICODE)

DEFAULT_MAX_ACTIVATIONS = 100


def validate_identifier(name: str, kind: str = "节点") -> None:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"{kind}名称含非法字符（仅允许中文、字母、数字、下划线、连字符）：{name!r}")


@dataclass(slots=True)
class Edge:
    """有向边。from → to；on=error 表示失败投递（重试/兜底路由）。"""

    frm: str
    to: str
    on: str = "success"

    def key(self) -> tuple[str, str, str]:
        return (self.frm, self.to, self.on)


@dataclass(slots=True)
class Node:
    """编排节点 = 智能体：身份（id）+ 任务（task）+ 数据进 + 路由模式。

    id:    节点名，即智能体身份名
    task:  完整任务指令（目标/范围/方法/规则/验收/回报格式）
    input: 数据进模板：{{steps.y.text}} 引用上游节点结果；亦可写字面量种子。
    外部运行传参直接编辑节点 input（节点自带输入接口，无图级 inputs 声明）
    routes: 出边路由模式 all（广播全部出边）| one（每次激活选一条出边）
    """

    id: str
    task: str = ""
    input: str = ""
    routes: str = "all"
    max_activations: int | None = None  # 节点级激活上限（None=继承全局）
    # 节点级 LLM 覆盖（空串=跟随图级/全局 workflow.node profile）：
    # 只给 provider 时模型取该 provider 的默认模型（见 src/workflow/node_llm.py）
    provider: str = ""
    model: str = ""
    # 节点级工具白名单：None=未声明（无工具，单回合 LLM）；[]=全量内置；
    # ["read_file", ...]=仅列出的内置工具（见 wdl.tools.TOOL_SCHEMAS）
    tools: list[str] | None = None

    def effective_max_activations(self, graph_max: int) -> int:
        return self.max_activations if self.max_activations is not None else graph_max


@dataclass(slots=True)
class FlowGraph:
    """工作流图：唯一核心数据结构。节点是智能体，边是拓扑。"""

    name: str
    description: str = ""
    # 图级 LLM 覆盖（可选）：非空时本图节点优先用它，否则回退全局
    # workflow.node profile（见 src/workflow/node_llm.py）。
    provider: str = ""
    model: str = ""
    schedule: dict[str, Any] = field(default_factory=lambda: {"kind": "manual"})
    max_activations: int = DEFAULT_MAX_ACTIVATIONS
    # 图级工具循环参数（max_tool_rounds / shell_timeout；空=继承 providers.yaml settings 或默认）
    settings: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    # ── 结构操作 ────────────────────────────────────────────────────────

    def add_node(self, node: Node) -> None:
        if node.id in self.nodes:
            raise ValueError(f"节点已存在：{node.id}")
        self.nodes[node.id] = node

    def add_edge(self, frm: str, to: str, on: str = "success") -> Edge:
        edge = Edge(frm=frm, to=to, on=on)
        if any(e.key() == edge.key() for e in self.edges):
            raise ValueError(f"边已存在：{frm} → {to} (on={on})")
        self.edges.append(edge)
        return edge

    def remove_edge(self, frm: str, to: str, on: str = "success") -> bool:
        for e in self.edges:
            if e.key() == (frm, to, on):
                self.edges.remove(e)
                return True
        return False

    def remove_node(self, node_id: str) -> bool:
        if node_id not in self.nodes:
            return False
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if e.frm != node_id and e.to != node_id]
        return True

    # ── 派生视图（只读，禁止独立写） ────────────────────────────────────

    def out_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.frm == node_id]

    def in_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.to == node_id]

    def depends_on(self, node_id: str) -> list[str]:
        """扇入等待集（按边声明顺序去重）。"""
        seen: list[str] = []
        for e in self.in_edges(node_id):
            if e.frm not in seen:
                seen.append(e.frm)
        return seen

    def routes_to(self, node_id: str) -> list[str]:
        """扇出目标集（按边声明顺序去重）。"""
        seen: list[str] = []
        for e in self.out_edges(node_id):
            if e.to not in seen:
                seen.append(e.to)
        return seen

    def source_ids(self) -> list[str]:
        """入度为零的源节点。"""
        targeted = {e.to for e in self.edges}
        return [nid for nid in self.nodes if nid not in targeted]

    def sink_ids(self) -> list[str]:
        """出度为零的汇节点。"""
        sourcing = {e.frm for e in self.edges}
        return [nid for nid in self.nodes if nid not in sourcing]
