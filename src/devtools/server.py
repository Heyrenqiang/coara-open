"""coara-devtools：独立开发者 debug 服务。

启动即开 LLM log 查看器（默认 http://127.0.0.1:8090），数据来自各工作空间
``.coara/llm/llm-calls.jsonl`` 磁盘镜像；详情里按小轮 / 回合标费用（缺口时对齐
``usage/events.jsonl``），不依赖运行中的 coara 进程。

鉴权沿用 dashboard token（<coara_home>/system/dashboard_token），首次启动经
URL ``?token=xxx`` 带入；CLI 启动时打印带 token 的 URL。
"""

from __future__ import annotations

import argparse
import contextlib
import webbrowser
from pathlib import Path

from aiohttp import web

from src.core.coara_home import resolve_coara_home
from src.devtools.llm_mirror_reader import collect_latest, collect_tool_names, summarize
from src.ui.dashboard_auth import check_dashboard_token
from src.workspace.registry import open_registry

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _workspace_entries(coara_home: Path) -> list[tuple[str, Path]]:
    """(显示名, 物理路径) 列表，来自 workspaces.yaml（coara 不在跑也能读）。"""
    try:
        registry = open_registry(coara_home, coara_home)
        entries = registry.list_active()
        return [(str(getattr(e, "name", "") or ""), e.resolved_path()) for e in entries]
    except Exception:
        return []


class DevServer:
    def __init__(self, coara_home: Path, host: str, port: int) -> None:
        self.coara_home = coara_home
        self.host = host
        self.port = port
        # Snapshot index at process start so UI features stay in lockstep with
        # registered routes (hot-reload HTML against a stale process caused 404s).
        html_path = _STATIC_DIR / "index.html"
        self._index_html = (
            html_path.read_text(encoding="utf-8") if html_path.is_file() else "<h1>devtools static missing</h1>"
        )

    def _check(self, request: web.Request) -> None:
        check_dashboard_token(request, workspace_dir=self.coara_home, coara_home=self.coara_home)

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._index_html, content_type="text/html")

    async def handle_llm_calls(self, request: web.Request) -> web.Response:
        self._check(request)
        entries = _workspace_entries(self.coara_home)
        names = {str(path.resolve()): name for name, path in entries}
        collected = collect_latest(self.coara_home, entries)
        workspaces = []
        for ws_key, agents in collected.items():
            summaries = sorted(
                (summarize(e) for e in agents.values()),
                key=lambda s: str(s.get("ts") or ""),
                reverse=True,
            )
            is_pseudo = ws_key in ("工作流（独立）", "日报（全局）")
            workspaces.append(
                {
                    "workspace": ws_key,
                    "name": names.get(ws_key) or (ws_key if is_pseudo else Path(ws_key).name),
                    "agents": summaries,
                }
            )
        workspaces.sort(
            key=lambda w: str(w["agents"][0].get("ts") or "") if w["agents"] else "",
            reverse=True,
        )
        return web.json_response({"workspaces": workspaces})

    async def handle_llm_call_detail(self, request: web.Request) -> web.Response:
        self._check(request)
        workspace = (request.query.get("workspace") or "").strip()
        agent = (request.query.get("agent") or "").strip()
        if not workspace or not agent:
            return web.json_response({"error": "需要 workspace 与 agent 参数"}, status=400)
        entries = _workspace_entries(self.coara_home)
        collected = collect_latest(self.coara_home, entries)
        agents = collected.get(workspace)
        if not agents:
            return web.json_response({"error": "该工作空间暂无 LLM 调用记录"}, status=404)
        entry = agents.get(agent)
        if entry is None:
            return web.json_response({"error": "该智能体暂无 LLM 调用记录"}, status=404)
        payload = dict(entry)
        payload["workspace"] = workspace
        # 详情同样携带本会话实际调用的工具名清单（供头部/详情标签直接展示）
        payload["tool_names"] = collect_tool_names(entry)

        # 小轮 / 回合 / 会话费用：镜像 usage + 缺口时 events.jsonl 对齐补齐
        from src.runtime.usage_query import enrich_llm_log_detail

        enrich_llm_log_detail(payload, coara_home=self.coara_home, workspace_dir=workspace)
        return web.json_response(payload)

    async def handle_context_modules_get(self, request: web.Request) -> web.Response:
        self._check(request)
        from src.coara.injections.context_modules import load_module_order, order_as_dicts
        from src.core.coara_home import system_dir_for_home

        system_dir = system_dir_for_home(self.coara_home)
        order = load_module_order(system_dir=system_dir)
        return web.json_response({"order": order_as_dicts(order), "system_dir": str(system_dir)})

    async def handle_context_modules_put(self, request: web.Request) -> web.Response:
        self._check(request)
        from src.coara.injections.context_modules import order_as_dicts, save_module_order
        from src.core.coara_home import system_dir_for_home

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "需要 JSON body"}, status=400)
        raw_order = body.get("order") if isinstance(body, dict) else None
        if not isinstance(raw_order, list):
            return web.json_response({"error": "需要 order 数组"}, status=400)
        system_dir = system_dir_for_home(self.coara_home)
        try:
            path = save_module_order(raw_order, system_dir=system_dir)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        # Reload normalized
        from src.coara.injections.context_modules import load_module_order

        order = load_module_order(system_dir=system_dir)
        return web.json_response({"ok": True, "path": str(path), "order": order_as_dicts(order)})

    async def handle_context_modules_preview(self, request: web.Request) -> web.Response:
        self._check(request)
        from src.coara.injections.context_modules import preview_context_stack
        from src.core.coara_home import system_dir_for_home

        workspace = (request.query.get("workspace") or "").strip()
        if not workspace:
            entries = _workspace_entries(self.coara_home)
            workspace = str(entries[0][1]) if entries else ""
        if not workspace:
            return web.json_response({"error": "无可用工作空间"}, status=400)
        system_dir = system_dir_for_home(self.coara_home)
        segments = preview_context_stack(workspace, system_dir=system_dir)
        return web.json_response({"workspace": workspace, "segments": segments})

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/dev/llm-calls", self.handle_llm_calls)
        app.router.add_get("/api/dev/llm-calls/detail", self.handle_llm_call_detail)
        app.router.add_get("/api/dev/context-modules", self.handle_context_modules_get)
        app.router.add_put("/api/dev/context-modules", self.handle_context_modules_put)
        app.router.add_get("/api/dev/context-modules/preview", self.handle_context_modules_preview)
        return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="coara-devtools", description="coara 开发者 debug 工具（LLM log 查看器）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--coara-home", default=None, help="缺省取 COARA_HOME 环境变量，再退 cwd/.coara")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    coara_home = resolve_coara_home(Path.cwd(), args.coara_home)
    # 加载 providers 价表，详情页才能算费用（否则一律「未配价」）
    try:
        import asyncio

        from src.core.config import config_manager

        asyncio.run(config_manager.load())
    except Exception as exc:
        print(f"  warn: providers 未加载（费用可能显示未配价）: {exc}")
    from src.ui.dashboard_tokens import load_or_create_dashboard_token

    token = load_or_create_dashboard_token(coara_home, coara_home)
    server = DevServer(coara_home, args.host, args.port)
    url = f"http://{args.host}:{args.port}/?token={token}"
    print(f"coara-devtools · LLM log 查看器\n  {url}\n  coara_home: {coara_home}")
    if not args.no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    web.run_app(server.build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
