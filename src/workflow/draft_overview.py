"""当前草案画布概况 — 构建对话的尾部注入文本（ws.md 的草案版）。

每次注入时从草案投影（内核图）确定性现算：不是维护型文档，永远与画布
一致，零腐化风险。文本即指纹——内容没变不重复注入（调用方比对 sha1）。
"""

from __future__ import annotations

from datetime import datetime

from src.workflow.draft_service import parse_projection_or_none
from src.workflow.draft_store import WorkflowDraft

_TASK_PREVIEW_CHARS = 50


def _short(text: str, limit: int = _TASK_PREVIEW_CHARS) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _fmt_updated(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def build_draft_overview(draft: WorkflowDraft) -> str | None:
    """渲染草案画布概况；投影解析失败返回 None（调用方跳过注入）。"""
    graph = parse_projection_or_none(draft.wdl)
    if graph is None:
        return None

    nodes = list(graph.nodes.values())
    header = (
        f"当前打开草案：{draft.draft_id} · 流「{graph.name}」"
        f"（{len(nodes)} 节点 / {len(graph.edges)} 边，更新于 {_fmt_updated(draft.updated_at)}）"
    )
    if graph.provider or graph.model:
        header += f"\n图级模型：{'/'.join(p for p in (graph.provider, graph.model) if p)}"

    node_lines: list[str] = []
    for node in nodes:
        mark = ""
        llm = "/".join(p for p in (node.provider, node.model) if p)
        if llm:
            mark += f"（{llm}）"
        if node.routes == "one":
            mark += "（路由 one）"
        preview = _short(node.task) or "（未写任务）"
        node_lines.append(f"- {node.id}{mark}：{preview}")

    edge_parts = []
    for e in graph.edges:
        arrow = "--error-->" if e.on == "error" else "→"
        edge_parts.append(f"{e.frm} {arrow} {e.to}")

    lines = [header, "节点：", *node_lines]
    lines.append("连接：" + ("；".join(edge_parts) if edge_parts else "（无）"))
    sources, sinks = graph.source_ids(), graph.sink_ids()
    if sources:
        lines.append("入口：" + "、".join(sources))
    if sinks and sinks != sources:
        lines.append("出口：" + "、".join(sinks))
    return "\n".join(lines)


__all__ = ["build_draft_overview"]
