"""内核 canonical 序列化 — 图 ↔ YAML 文本，round-trip 恒等。

emit_graph(g) == emit_graph(parse_graph(emit_graph(g)))，且
parse_graph(emit_graph(g)) 与 g 逐字段相等——唯一表达由序列化规范形锁死：

- 固定键序：name / description / provider / model / schedule /
  max_activations / settings / nodes / edges；节点键序 id / task / input /
  routes / max_activations / provider / model / tools；边键序 from / to / on。
- 省略默认值：task 空串、input 空串、routes=all、
  max_activations=None、schedule 为 {kind: manual}、settings 为空对象、
  tools=None（未声明=无工具）时不写，
  provider/model 空串时不写（图级与节点级同为 LLM 覆盖，默认走全局 workflow.node）。
- 节点按 id 排序、边按 (from, to, on) 排序，声明顺序不进入序列化。

WDL 定稿文本就是本投影；legacy 旧格式已随 2026-08-16 内核化整体退役，无互译层。
"""

from __future__ import annotations

from typing import Any

import yaml

from wdl.core.model import Edge, FlowGraph, Node


def _node_to_dict(node: Node) -> dict[str, Any]:
    d: dict[str, Any] = {}
    if node.task:
        d["task"] = node.task
    if node.input:
        d["input"] = node.input
    if node.routes != "all":
        d["routes"] = node.routes
    if node.max_activations is not None:
        d["max_activations"] = node.max_activations
    if node.provider:
        d["provider"] = node.provider
    if node.model:
        d["model"] = node.model
    if node.tools is not None:
        d["tools"] = list(node.tools)
    return d


def _edge_to_dict(edge: Edge) -> dict[str, Any]:
    d: dict[str, Any] = {"from": edge.frm, "to": edge.to}
    if edge.on != "success":
        d["on"] = edge.on
    return d


def graph_to_dict(graph: FlowGraph) -> dict[str, Any]:
    d: dict[str, Any] = {"name": graph.name}
    if graph.description:
        d["description"] = graph.description
    if graph.provider:
        d["provider"] = graph.provider
    if graph.model:
        d["model"] = graph.model
    if not (isinstance(graph.schedule, dict) and graph.schedule == {"kind": "manual"}):
        d["schedule"] = dict(graph.schedule or {})
    if not _is_default_max_activations(graph.max_activations):
        d["max_activations"] = graph.max_activations
    if graph.settings:
        d["settings"] = dict(graph.settings)
    d["nodes"] = {nid: _node_to_dict(n) for nid, n in sorted(graph.nodes.items())}
    d["edges"] = [_edge_to_dict(e) for e in _sorted_edges(graph)]
    return d


def _is_default_max_activations(v: int) -> bool:
    from wdl.core.model import DEFAULT_MAX_ACTIVATIONS

    return v == DEFAULT_MAX_ACTIVATIONS


def _sorted_edges(graph: FlowGraph) -> list[Edge]:
    return sorted(graph.edges, key=lambda e: (e.frm, e.to, e.on))


def emit_graph(graph: FlowGraph) -> str:
    return yaml.safe_dump(
        graph_to_dict(graph),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    )


def parse_graph(text: str) -> FlowGraph:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"工作流文本不是合法 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("工作流文本不是 YAML 对象")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("缺少 name")
    graph = FlowGraph(
        name=name,
        description=str(raw.get("description") or ""),
        provider=str(raw.get("provider") or "").strip(),
        model=str(raw.get("model") or "").strip(),
        schedule=dict(raw.get("schedule") or {"kind": "manual"}),
        max_activations=_parse_max_activations(raw.get("max_activations")),
        settings=_parse_settings(raw.get("settings")),
    )
    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        raise ValueError("缺少 nodes")
    for nid, nd in nodes_raw.items():
        if not isinstance(nd, dict):
            raise ValueError(f"节点 {nid} 不是对象")
        graph.nodes[str(nid)] = Node(
            id=str(nid),
            task=str(nd.get("task") or ""),
            input=str(nd.get("input") or ""),
            routes=str(nd.get("routes") or "all"),
            max_activations=_opt_int(nd.get("max_activations"), "max_activations"),
            provider=str(nd.get("provider") or "").strip(),
            model=str(nd.get("model") or "").strip(),
            tools=_parse_tools(nd.get("tools"), str(nid)),
        )
    edges_raw = raw.get("edges")
    if edges_raw is not None and not isinstance(edges_raw, list):
        raise ValueError("edges 需为列表")
    for er in edges_raw or []:
        if not isinstance(er, dict):
            raise ValueError("边不是对象")
        frm, to = str(er.get("from") or ""), str(er.get("to") or "")
        if not frm or not to:
            raise ValueError("边缺少 from / to")
        # YAML 1.1 把裸词 on/off/yes/no 解析为布尔（flow map {on: error} 的
        # key 会变成 True）——这里按语义兼容还原
        on_raw = er.get("on", er.get(True, "success"))
        graph.edges.append(Edge(frm=frm, to=to, on=str(on_raw or "success")))
    return graph


def _parse_max_activations(v: Any) -> int:
    from wdl.core.model import DEFAULT_MAX_ACTIVATIONS

    if v is None:
        return DEFAULT_MAX_ACTIVATIONS
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("max_activations 需为正整数")
    if v <= 0:
        raise ValueError("max_activations 需为正整数")
    return v


def _parse_settings(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError("settings 需为对象")
    out = dict(v)
    if out.get("max_tool_rounds") is not None:
        out["max_tool_rounds"] = _opt_int(out["max_tool_rounds"], "settings.max_tool_rounds")
    if out.get("shell_timeout") is not None:
        t = out["shell_timeout"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            raise ValueError("settings.shell_timeout 需为正数")
        out["shell_timeout"] = float(t)
    return out


def _parse_tools(v: Any, node_id: str) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(item, str) and item.strip() for item in v):
        raise ValueError(f"节点 {node_id} 的 tools 需为字符串列表（空列表=全量内置工具）")
    return [item.strip() for item in v]


def _opt_int(v: Any, field_name: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValueError(f"{field_name} 需为正整数或省略")
    return v
