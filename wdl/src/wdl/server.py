"""wdl 内嵌 Web 服务 — 画布工作台后端。

一个 aiohttp 进程同时承担三件事：

1. 静态托管 workbench 前端构建产物（``wdl/workbench/dist``）。
2. REST API：WDL 文件 CRUD（根目录可配，默认 cwd）、WDL parse/emit
   （core.serde round-trip）、实例 run/list/get/cancel/resume。
3. WS ``/ws``：把 ``WdlEngine.on_event`` 的进度事件（workflow_started /
   node_started / node_completed / node_failed / workflow_completed /
   workflow_failed / workflow_cancelled）实时转发给所有已连接浏览器。

复用 wdl 包现有 engine/persistence/serde，不另起体系。关停时先退引擎
（其内部收口 persistence 的 aiosqlite 线程），再退 web runner，防进程挂死。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from wdl.core.model import FlowGraph, Node
from wdl.core.semantics import validate_graph
from wdl.core.serde import emit_graph, parse_graph
from wdl.engine import WdlEngine
from wdl.logging import logger

DEFAULT_PORT = 8177

# WDL 文件名：字母数字开头，可含 - _ .，必须以 .wdl 结尾（防目录穿越）
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.wdl$")

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # src/wdl/server.py → 包根（wdl/）
DEFAULT_STATIC_DIR = _PACKAGE_ROOT / "workbench" / "dist"


def _default_wdl() -> str:
    return emit_graph(
        FlowGraph(
            name="未命名",
            description="",
            schedule={"kind": "manual"},
        )
    )


def _resolve_file(root: Path, name: str) -> Path:
    """把 URL 段里的文件名解析成 root 下的绝对路径；非法名抛 ValueError。"""
    if not _FILENAME_RE.match(name):
        raise ValueError(f"非法文件名：{name!r}（仅允许 *.wdl 文件名，不含路径分隔符）")
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise ValueError(f"路径越出根目录：{name!r}")
    return path


class WdlWorkbenchServer:
    """画布工作台服务：静态站点 + REST + WS 事件转发。"""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        root_dir: str | Path | None = None,
        engine: WdlEngine | None = None,
        static_dir: str | Path | None = None,
    ) -> None:
        self.port = port
        self.root_dir = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
        self.engine = engine if engine is not None else WdlEngine()
        self.static_dir = Path(static_dir).resolve() if static_dir is not None else DEFAULT_STATIC_DIR
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._started = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._register_routes()
        self.engine.on_event(self._on_engine_event)
        await self.engine.start()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await self.site.start()
        logger.info(f"WdlWorkbenchServer listening on http://127.0.0.1:{self.port}（根目录 {self.root_dir}）")

    async def stop(self) -> None:
        for ws in list(self._sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        self._sockets.clear()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
        await self.engine.shutdown()
        self._started = False

    def _register_routes(self) -> None:
        r = self.app.router
        r.add_get("/api/files", self._handle_file_list)
        r.add_post("/api/files", self._handle_file_create)
        r.add_get("/api/files/{name}", self._handle_file_get)
        r.add_put("/api/files/{name}", self._handle_file_put)
        r.add_delete("/api/files/{name}", self._handle_file_delete)
        r.add_post("/api/wdl/parse", self._handle_wdl_parse)
        r.add_post("/api/wdl/emit", self._handle_wdl_emit)
        r.add_get("/api/instances", self._handle_instance_list)
        r.add_post("/api/instances/run", self._handle_instance_run)
        r.add_get("/api/instances/{instance_id}", self._handle_instance_get)
        r.add_post("/api/instances/{instance_id}/cancel", self._handle_instance_cancel)
        r.add_post("/api/instances/{instance_id}/resume", self._handle_instance_resume)
        r.add_get("/ws", self._handle_ws)
        if self.static_dir.is_dir():
            r.add_get("/", self._handle_index)
            r.add_static("/assets", self.static_dir / "assets")

    async def _handle_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.static_dir / "index.html")

    # ── WS：引擎事件转发 ─────────────────────────────────────────────────

    async def _on_engine_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._sockets:
            return
        instance_id = str(payload.get("task_id") or payload.get("instance_id") or "")
        message = json.dumps(
            {"type": event_type, "instance_id": instance_id, "payload": payload},
            ensure_ascii=False,
        )
        stale: list[web.WebSocketResponse] = []
        for ws in list(self._sockets):
            try:
                await ws.send_str(message)
            except Exception:  # noqa: BLE001 — 坏连接摘除，不拖垮事件分发
                stale.append(ws)
        for ws in stale:
            self._sockets.discard(ws)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str(json.dumps({"type": "pong"}))
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self._sockets.discard(ws)
        return ws

    # ── WDL 文件 CRUD ────────────────────────────────────────────────────

    def _file_row(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        return {
            "name": path.name,
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        }

    async def _handle_file_list(self, request: web.Request) -> web.Response:
        files = sorted(
            (self._file_row(p) for p in self.root_dir.glob("*.wdl") if p.is_file()),
            key=lambda row: row["updated_at"],
            reverse=True,
        )
        return web.json_response({"files": files, "root": str(self.root_dir)})

    async def _handle_file_create(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "无效的 JSON 请求体"}, status=400)
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "缺少文件名"}, status=400)
        name = name.strip()
        if not name.endswith(".wdl"):
            name += ".wdl"
        try:
            path = _resolve_file(self.root_dir, name)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if path.exists():
            return web.json_response({"error": f"文件已存在：{name}"}, status=409)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = _default_wdl()
        path.write_text(text, encoding="utf-8")
        return web.json_response({"name": name, "wdl": text}, status=201)

    async def _handle_file_get(self, request: web.Request) -> web.Response:
        try:
            path = _resolve_file(self.root_dir, request.match_info["name"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not path.is_file():
            return web.json_response({"error": f"文件不存在：{path.name}"}, status=404)
        return web.json_response({"name": path.name, "wdl": path.read_text(encoding="utf-8")})

    async def _handle_file_put(self, request: web.Request) -> web.Response:
        try:
            path = _resolve_file(self.root_dir, request.match_info["name"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not path.is_file():
            return web.json_response({"error": f"文件不存在：{path.name}"}, status=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "无效的 JSON 请求体"}, status=400)
        wdl_text = body.get("wdl") if isinstance(body, dict) else None
        if not isinstance(wdl_text, str):
            return web.json_response({"error": "缺少 wdl 文本"}, status=400)
        path.write_text(wdl_text, encoding="utf-8")
        return web.json_response({"name": path.name, "saved": True, "updated_at": self._file_row(path)["updated_at"]})

    async def _handle_file_delete(self, request: web.Request) -> web.Response:
        try:
            path = _resolve_file(self.root_dir, request.match_info["name"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not path.is_file():
            return web.json_response({"error": f"文件不存在：{path.name}"}, status=404)
        path.unlink()
        return web.json_response({"deleted": True, "name": path.name})

    # ── WDL parse / emit（编辑器画布 ↔ 文本双向同步） ─────────────────────

    async def _handle_wdl_parse(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "无效的 JSON 请求体"}, status=400)
        wdl_text = body.get("wdl") if isinstance(body, dict) else None
        if not isinstance(wdl_text, str) or not wdl_text.strip():
            return web.json_response({"error": "缺少 WDL 文本"}, status=400)
        try:
            graph = parse_graph(wdl_text)
        except ValueError as exc:
            return web.json_response({"error": f"工作流解析失败：{exc}"}, status=400)
        issues = validate_graph(graph)
        errors = [i.message for i in issues if i.level == "error"]
        if errors:
            return web.json_response({"error": "工作流校验失败：" + "；".join(errors)}, status=400)
        warnings = [i.message for i in issues if i.level != "error"]
        document: dict[str, Any] = {
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
            "edges": [{"from": e.frm, "to": e.to, **({"on": e.on} if e.on != "success" else {})} for e in graph.edges],
        }
        response: dict[str, Any] = {"document": document}
        if warnings:
            response["warnings"] = warnings
        return web.json_response(response)

    async def _handle_wdl_emit(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "无效的 JSON 请求体"}, status=400)
        document = body.get("document") if isinstance(body, dict) else None
        if not isinstance(document, dict):
            return web.json_response({"error": "缺少 document 对象"}, status=400)
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

    # ── 实例 run / list / get / cancel / resume ──────────────────────────

    async def _handle_instance_run(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "无效的 JSON 请求体"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "无效的请求体"}, status=400)
        inputs = body.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        wdl_text = body.get("wdl")
        file_name = body.get("file")
        if isinstance(wdl_text, str) and wdl_text.strip():
            text = wdl_text
        elif isinstance(file_name, str) and file_name.strip():
            try:
                path = _resolve_file(self.root_dir, file_name.strip())
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            if not path.is_file():
                return web.json_response({"error": f"文件不存在：{path.name}"}, status=404)
            text = path.read_text(encoding="utf-8")
        else:
            return web.json_response({"error": "缺少 wdl 文本或 file 文件名"}, status=400)
        try:
            parse_graph(text)  # 先解析，失败不建实例
        except ValueError as exc:
            return web.json_response({"error": f"工作流解析失败：{exc}"}, status=400)
        instance_id = await self.engine.start_workflow(text, inputs)
        await self.engine.persistence.save_instance_origin(instance_id, {"source": "workbench"})
        return web.json_response({"instance_id": instance_id, "status": "running"})

    async def _handle_instance_list(self, request: web.Request) -> web.Response:
        status = request.query.get("status", "").strip()
        status_filter = [s.strip() for s in status.split(",") if s.strip()] or None
        instances = await self.engine.persistence.list_instances(status_filter=status_filter)
        return web.json_response({"instances": instances})

    async def _handle_instance_get(self, request: web.Request) -> web.Response:
        view = await self.engine.persistence.get_instance_run_view(request.match_info["instance_id"])
        if view is None:
            return web.json_response({"error": "实例不存在"}, status=404)
        return web.json_response(view)

    async def _handle_instance_cancel(self, request: web.Request) -> web.Response:
        instance_id = request.match_info["instance_id"]
        record = await self.engine.persistence.load_instance(instance_id)
        if record is None:
            return web.json_response({"error": "实例不存在"}, status=404)
        await self.engine.cancel_instance(instance_id)
        return web.json_response({"cancelled": True, "instance_id": instance_id})

    async def _handle_instance_resume(self, request: web.Request) -> web.Response:
        instance_id = request.match_info["instance_id"]
        record = await self.engine.persistence.load_instance(instance_id)
        if record is None:
            return web.json_response({"error": "实例不存在"}, status=404)
        if record.status.value not in ("interrupted", "waiting", "failed"):
            return web.json_response(
                {"error": f"实例状态 {record.status.value} 不可恢复"},
                status=409,
            )
        await self.engine.resume_instance(instance_id)
        return web.json_response({"resumed": True, "instance_id": instance_id})


async def serve(
    *,
    port: int = DEFAULT_PORT,
    root_dir: str | Path | None = None,
    engine: WdlEngine | None = None,
    static_dir: str | Path | None = None,
) -> None:
    """起服务直到被取消（KeyboardInterrupt 由调用方兜底）。"""
    server = WdlWorkbenchServer(port=port, root_dir=root_dir, engine=engine, static_dir=static_dir)
    await server.start()
    print(f"WDL 工作台已启动：http://127.0.0.1:{port}（根目录 {server.root_dir}）")
    if not server.static_dir.is_dir():
        print(f"提示：未找到前端构建产物 {server.static_dir}，仅 API 可用；构建见 workbench/README")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
