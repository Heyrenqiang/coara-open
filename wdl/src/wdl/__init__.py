"""wdl — 独立 WDL 执行引擎（自 coara v8 剥离）。

WDL 软件 = 引擎 + 自己的 provider + 自己的执行器。图模型与调度语义见
``wdl.core``；in-process asyncio 引擎见 ``wdl.engine``。
"""

from __future__ import annotations

from wdl.core import (
    EDGE_KINDS,
    ROUTE_MODES,
    ActivationEngine,
    Edge,
    FlowGraph,
    Issue,
    Node,
    emit_graph,
    graph_to_dict,
    implicit_dependencies,
    parse_graph,
    validate_graph,
    validate_identifier,
)
from wdl.engine import WdlEngine
from wdl.errors import WDLError

__version__ = "0.1.0"

__all__ = [
    "EDGE_KINDS",
    "ROUTE_MODES",
    "ActivationEngine",
    "Edge",
    "FlowGraph",
    "Issue",
    "Node",
    "WdlEngine",
    "WDLError",
    "emit_graph",
    "graph_to_dict",
    "implicit_dependencies",
    "parse_graph",
    "validate_graph",
    "validate_identifier",
    "__version__",
]
