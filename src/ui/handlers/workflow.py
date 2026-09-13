"""Web 端工作流草案辅助：WDL 投影 parse/emit + 当前打开草案上报。

WDL 执行层已剥离为独立软件（2026-09-08）：实例 list/get/run/cancel/resume
与 workflow-settings（workflow.node profile）接口全部移除。这里只剩画布
编辑器与 FlowRoot 构建会话所需的投影转换与活跃草案登记。
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase


class WorkflowHandlers(HandlerMixinBase):
    async def _handle_wdl_parse(self, request: web.Request) -> web.Response:
        """Parse kernel projection text into a graph JSON for the React editor.

        草案文本是内核投影（nodes+edges）。这里返回图结构的 JSON
        （name/description/schedule/max_activations/nodes/edges），
        编辑器画布按图渲染。
        """
        self._check_token(request)
        from src.workflow.draft_service import parse_projection_or_none, validate_projection

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        wdl_text = body.get("wdl") if isinstance(body, dict) else None
        if not isinstance(wdl_text, str) or not wdl_text.strip():
            raise web.HTTPBadRequest(text="缺少 WDL 文本")
        errors, warnings = validate_projection(wdl_text)
        if errors:
            return web.json_response(
                {"error": "工作流校验失败（parse）：" + "；".join(errors)},
                status=400,
            )
        graph = parse_projection_or_none(wdl_text)
        if graph is None:
            return web.json_response({"error": "工作流文本解析失败"}, status=400)
        response: dict[str, Any] = {
            "document": {
                "name": graph.name,
                "description": graph.description or "",
                "schedule": graph.schedule or {"kind": "manual"},
                "max_activations": graph.max_activations,
                "nodes": {
                    nid: {
                        "task": node.task,
                        "input": node.input or "",
                        "routes": node.routes,
                        **({"max_activations": node.max_activations} if node.max_activations is not None else {}),
                        **({"provider": node.provider} if node.provider else {}),
                        **({"model": node.model} if node.model else {}),
                    }
                    for nid, node in graph.nodes.items()
                },
                "edges": [
                    {"from": e.frm, "to": e.to, **({"on": e.on} if e.on != "success" else {})} for e in graph.edges
                ],
            }
        }
        if warnings:
            response["warnings"] = warnings
        return web.json_response(response)

    async def _handle_wdl_emit(self, request: web.Request) -> web.Response:
        """Emit canonical projection text from a graph dict (React editor).

        document 结构即 parse 返回的图 JSON（nodes/edges）；round-trip 与
        FlowCoordinator.export_wdl 同源（wdl.core.serde）。
        """
        self._check_token(request)
        from src.workflow.core.model import FlowGraph, Node
        from src.workflow.core.serde import emit_graph

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        document = body.get("document") if isinstance(body, dict) else None
        if not isinstance(document, dict):
            raise web.HTTPBadRequest(text="缺少 document 对象")
        try:
            graph = FlowGraph(
                name=str(document.get("name") or "未命名"),
                description=str(document.get("description") or ""),
                schedule=dict(document.get("schedule") or {"kind": "manual"}),
            )
            raw_max = document.get("max_activations")
            if isinstance(raw_max, int) and raw_max > 0:
                graph.max_activations = raw_max
            for nid, spec in (document.get("nodes") or {}).items():
                if not isinstance(spec, dict):
                    continue
                node = Node(
                    id=str(nid),
                    task=str(spec.get("task") or ""),
                    input=str(spec.get("input") or ""),
                    routes="one" if spec.get("routes") == "one" else "all",
                )
                raw_ma = spec.get("max_activations")
                if isinstance(raw_ma, int) and raw_ma > 0:
                    node.max_activations = raw_ma
                node.provider = str(spec.get("provider") or "").strip()
                node.model = str(spec.get("model") or "").strip()
                graph.add_node(node)
            for e in document.get("edges") or []:
                if not isinstance(e, dict):
                    continue
                graph.add_edge(
                    str(e.get("from") or ""),
                    str(e.get("to") or ""),
                    on="error" if e.get("on") == "error" else "success",
                )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": f"document 结构无效：{exc}"}, status=400)
        return web.json_response({"wdl": emit_graph(graph)})

    async def _handle_workflow_active_draft(self, request: web.Request) -> web.Response:
        """上报工作台/编辑器当前打开的草案 id（编排写穿复用 + 构建对话切换 section）。"""
        self._check_token(request)
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        draft_id = str(body.get("draft_id") or "").strip() if isinstance(body, dict) else ""
        self.active_workflow_draft_id = draft_id or None
        # 每草案一个构建会话：切草案即切 section。主体未创建时惰性创建并绑定，
        # 否则构建对话历史读取会因 session_filter=None 混用所有草案。
        flow_root = self._module_roots.get("flow")
        if flow_root is None and draft_id:
            try:
                flow_root = await self._get_flow_root()
            except Exception:
                logger.exception("flow root lazy create failed (draft=%s)", draft_id)
                flow_root = None
        if flow_root is not None and draft_id:
            from src.coara.flow_root import switch_flow_draft_session

            try:
                switch_flow_draft_session(flow_root, draft_id, coara_home=getattr(self, "coara_home", None))
            except Exception:
                logger.exception("flow draft session switch failed (draft=%s)", draft_id)
        return web.json_response({"ok": True})
