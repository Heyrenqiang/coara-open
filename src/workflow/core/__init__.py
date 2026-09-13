"""coara 自持的工作流图内核 — 编排图唯一核心。

★ coara 自持副本，源自 wdl-engine 的 ``wdl/src/wdl/core/``（2026-09-10 搬运），
目的是让 coara 的内嵌 flow 能力不依赖 wdl-engine 是否安装。分叉声明与漂移
哨兵见 ``model.py`` 文件头。

模块（与 wdl.core 一一对应，不含 kernel_runner —— 那是 wdl 引擎侧持久宿主）：
- model:      图模型（Node / Edge / FlowGraph 与派生视图）
- serde:      canonical YAML 序列化（emit / parse，round-trip 恒等）
- semantics:  校验、就绪计算与多激活语义

FlowGraph（节点 + 边）是工作流的唯一表达：编排工具织图、UI 画布渲染、
coara 的 FlowCoordinator 执行、WDL 定稿落盘，全部消费同一图模型。
WDL 文本只是图的 canonical 序列化投影（serde），不是独立语言。
"""

from src.workflow.core.model import (
    EDGE_KINDS,
    ROUTE_MODES,
    Edge,
    FlowGraph,
    Node,
    validate_identifier,
)
from src.workflow.core.semantics import (
    Activation,
    ActivationEngine,
    Issue,
    implicit_dependencies,
    validate_graph,
)
from src.workflow.core.serde import emit_graph, graph_to_dict, parse_graph

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
    "Activation",
    "ActivationEngine",
    "Issue",
    "implicit_dependencies",
    "validate_graph",
]
