"""工作流内核 — 编排图唯一核心。

FlowGraph（节点 + 边）是工作流的唯一表达：编排工具织图、UI 画布渲染、
外部引擎执行、WDL 定稿落盘，全部消费同一图模型。WDL 文本只是图的
canonical 序列化投影（serde），不是独立语言。

模块：
- model:      图模型（Node / Edge / FlowGraph 与派生视图）
- serde:      canonical YAML 序列化（emit / parse，round-trip 恒等）
- semantics:  校验、就绪计算与多激活语义（内外宿主共用）
"""

from wdl.core.model import (
    EDGE_KINDS,
    ROUTE_MODES,
    Edge,
    FlowGraph,
    Node,
    validate_identifier,
)
from wdl.core.semantics import (
    ActivationEngine,
    Issue,
    implicit_dependencies,
    validate_graph,
)
from wdl.core.serde import emit_graph, graph_to_dict, parse_graph

__all__ = [
    "EDGE_KINDS",
    "ROUTE_MODES",
    "Edge",
    "FlowGraph",
    "Node",
    "validate_identifier",
    "emit_graph",
    "graph_to_dict",
    "parse_graph",
    "ActivationEngine",
    "Issue",
    "implicit_dependencies",
    "validate_graph",
]
