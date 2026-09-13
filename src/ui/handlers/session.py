from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase

# 快照分页口径：默认与上限（/api/session/messages 与切空间响应共用同一组常量，
# 端上按同一把尺取历史，不允许两处各写一个数）。
_DEFAULT_HISTORY_LIMIT = 100
_MAX_HISTORY_LIMIT = 500


class SessionHandlers(HandlerMixinBase):
    async def _handle_session_messages(self, request: web.Request) -> web.Response:
        """Return the Web UI conversation log for the current workspace.

        常态读取走 web 会话视图存储（web_views/{subject}__{session_id}.jsonl）——
        web 聊天区的唯一数据源；录像带 projection 不参与聊天区显示。

        Query params:
            limit: Max messages to return (default 100, capped at 500).
        """
        return await self._session_messages_impl(request, "web")

    async def _handle_flow_session_messages(self, request: web.Request) -> web.Response:
        """工作流页面（FlowRoot 主体）的会话历史：source=web-flow，同文件隔离。"""
        return await self._session_messages_impl(request, "web-flow")

    async def _handle_module_session_messages(self, request: web.Request) -> web.Response:
        """按 subject 返回模块主体（agentic 会话）历史：source=web-<subject>。

        flow → web-flow，与 /api/flow-session/messages 等价。

        Query params:
            subject: 模块主体 subject（必填；须解析到一个启用 agentic 会话的模块）。
            limit: Max messages to return (default 100, capped at 500).
        """
        self._check_token(request)
        subject = str(request.query.get("subject") or "").strip()
        if not self._is_module_subject(subject):
            return web.json_response({"error": f"unknown module subject: {subject!r}"}, status=404)
        # flow 读历史前确保构建主体存在并切到当前草案——否则 flow_root 从未创建
        # 时 session_filter=None 会把所有草案的历史混在一起。
        if subject == "flow":
            try:
                flow_root = self._module_roots.get("flow")
                if flow_root is None or not getattr(flow_root, "session_id", None):
                    await self._get_flow_root()
            except Exception:
                logger.exception("flow root ensure failed on history read")
        return await self._session_messages_impl(request, f"web-{subject}")

    def _resolve_view_subject_session(self, source: str) -> tuple[str, str] | None:
        """前端 source → 视图文件键 (subject, session_id)；解析不出返回 None。"""
        if source == "web":
            session_id = str(getattr(self._view_coara(), "session_id", "") or "")
            return ("root", session_id) if session_id else None
        subject = source.removeprefix("web-")
        module_root = self._module_roots.get(subject)
        session_id = str(getattr(module_root, "session_id", "") or "") if module_root is not None else ""
        if not session_id and subject == "flow":
            from src.workflow import draft_sessions

            active = getattr(self, "active_workflow_draft_id", None)
            if active:
                session_id = draft_sessions.session_for(active, getattr(self, "coara_home", None))
        return (subject, session_id) if session_id else None

    def _resolve_view_target(
        self, source: str, workspace_dir: str | Path | None = None
    ) -> tuple[str, str, Path] | None:
        """前端 source → 视图带定位 (subject, session_id, workspace_dir)；解析不出返回 None。

        ``workspace_dir`` 非空时进入**只读预取**模式：只读该空间的线，不动任何运行时
        状态（不切 workspace、不动活动计时、不写缓存）。它只对主会话（source="web"，
        每空间一条 ``conversation.jsonl``）有意义；模块线按会话分文件、跨空间不存在，
        传了也忽略。目标空间的 session_id 只做只读查（查不到就给空串——root 线的文件名
        与 session_id 无关，端上真正切换时会拿到切换响应里的权威 session_id）。
        """
        if workspace_dir is not None and source == "web":
            target_dir = Path(workspace_dir).expanduser()
            if not target_dir.is_absolute():
                return None
            return "root", self._readonly_session_id_for(target_dir), target_dir.resolve()
        resolved = self._resolve_view_subject_session(source)
        if resolved is None:
            return None
        subject, session_id = resolved
        workspace_dir = (
            self.workspace_dir
            if subject == "root"
            else Path(getattr(self.trace_store, "workspace_dir", self.workspace_dir))
        )
        return subject, session_id, workspace_dir

    def _readonly_session_id_for(self, workspace_dir: Path) -> str:
        """只读查某空间当前会话 id：查不到返回空串（绝不 ensure/创建会话）。"""
        try:
            from src.core.coara_home import workspace_id_for

            coara = self.root.resolve_workspace_coara(workspace_id_for(workspace_dir))
        except Exception:  # noqa: BLE001 — 未缓存/未注册都只是「不知道」
            return ""
        return str(getattr(coara, "session_id", "") or "")

    def _view_path_for(self, target: tuple[str, str, Path]) -> Path:
        from src.ui.web_views import resolve_web_view_path

        subject, session_id, workspace_dir = target
        return resolve_web_view_path(workspace_dir, coara_home=self.coara_home, subject=subject, session_id=session_id)

    def _line_epoch(self, target: tuple[str, str, Path]) -> str:
        """该线的 epoch（含重建世代）——快照与实时帧对账的身份锚点。"""
        from src.ui.web_views import view_line_epoch

        subject, _session_id, workspace_dir = target
        store = getattr(self, "_view_store", None)
        generation = 0
        if store is not None:
            try:
                generation = store.line_generation(self._view_path_for(target))
            except Exception:  # noqa: BLE001 — 世代读不到按 0（未重建）
                generation = 0
        return view_line_epoch(workspace_dir, subject, generation=generation)

    def _load_view_messages(
        self,
        source: str,
        limit: int,
        since_seq: int = 0,
        target: tuple[str, str, Path] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, dict[str, str], dict[str, list[dict[str, Any]]], dict[str, str]]:
        """常态 hydrate：从 web 会话视图存储聚合聊天行（web 聊天区唯一数据源）。

        实时显示（TurnStream 帧）与刷新恢复同读这份逐帧落盘的视图文件，
        录像带 projection 不再参与 web 聊天区。返回
        (messages, total, latest_view_seq, subagent_results, subagent_diffs, subagent_briefs)。

        ``subagent_results`` = ``{tool_call_id: text}``：子智能体最终答复不进
        消息流（否则刷新后复活成 assistant 气泡），只经这份映射回到端上的
        delegate 折叠里。``subagent_diffs`` = ``{父 call_id: [帧…]}``：子智能体
        自己的工具行与 diff 同理归集到发起它的那条 delegate 工具行的展开区。
        ``subagent_briefs`` = ``{父 call_id: 指令全文}``：delegate 任务指令也不
        再作为正文消息出现（老帧按 ``<任务指令>`` 文本判据同样归集）。

        三张映射都只收 ``view_seq > since_seq`` 的帧并按常量封顶（见 web_views）。

        *target* 由调用方预先解析（快照响应也要这几个字段）；不给时本方法自解。
        """
        from src.ui.web_views import WebViewStore

        subagent_results: dict[str, str] = {}
        subagent_diffs: dict[str, list[dict[str, Any]]] = {}
        subagent_briefs: dict[str, str] = {}
        if target is None:
            target = self._resolve_view_target(source)
        if target is None:
            return [], 0, 0, subagent_results, subagent_diffs, subagent_briefs
        subject, _session_id, _workspace_dir = target
        path = self._view_path_for(target)
        # 异步 JSONL 写完前就读会漏帧：关页再开像「内容没了」。读前先 flush。
        view_store = getattr(self, "_view_store", None)
        if view_store is not None:
            with contextlib.suppress(Exception):
                view_store.flush(timeout=1.0)
        # 主会话逐帧（与实时每条 chunk 帧独立气泡一致）；模块会话合并（实时为追加同一气泡）。
        messages, total, latest_seq = WebViewStore.build_messages(
            path,
            limit=limit,
            merge_chunks=subject != "root",
            since_seq=since_seq,
            subagent_out=subagent_results,
            subagent_diffs_out=subagent_diffs,
            subagent_briefs_out=subagent_briefs,
        )
        return messages, total, latest_seq, subagent_results, subagent_diffs, subagent_briefs

    async def _load_view_snapshot(
        self,
        source: str,
        *,
        limit: int = _DEFAULT_HISTORY_LIMIT,
        since_seq: int = 0,
        workspace_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """目标空间线的权威快照（**唯一构造点**）：/api/session/messages 与切空间响应共用。

        形状：``{messages, total, latest_seq, workspace_dir, session_id, epoch,
        subagent_results, subagent_diffs, subagent_briefs, runtime}``。
        epoch＝``workspace_dir::subject[#世代]`` 标识「这是哪条线」，latest_seq 是
        快照覆盖到的最后一帧——端侧按 view_seq 去重与 gap 检测的基准。

        *workspace_dir* 非空时只读该空间的线（预取用）：不动任何运行时状态，只是
        ``runtime`` 仍描述**调用端自己当前的视图回合态**（不代表被预取空间的回合态），
        端上提交预取内容时不要用它，等真正切换的响应。
        """
        target = self._resolve_view_target(source, workspace_dir)
        messages, total, latest_seq, subagent_results, subagent_diffs, subagent_briefs = await asyncio.to_thread(
            self._load_view_messages, source, limit, since_seq, target
        )
        # 权威快照里带上该空间的 runtime 摘要（running / turn_source / session_id）：
        # 刷新与切空间走的是同一条路径，回合态必须和消息同源一起回来——靠心跳推送
        # 会在「快照未变不推」时失真，端侧就只能拿缓存猜。
        runtime: dict[str, Any] | None = None
        try:
            runtime = self._current_runtime()
        except Exception:  # noqa: BLE001 — 夹具/异常态下不给回合态
            runtime = None
        if target is None:
            return {
                "messages": [],
                "total": 0,
                "latest_seq": 0,
                "workspace_dir": "",
                "session_id": "",
                "epoch": "",
                "subagent_results": {},
                "subagent_diffs": {},
                "subagent_briefs": {},
                "runtime": runtime,
            }
        subject, session_id, target_dir = target
        return {
            "messages": messages,
            "total": total,
            "latest_seq": latest_seq,
            "workspace_dir": str(target_dir),
            "session_id": session_id,
            "epoch": self._line_epoch(target),
            "subagent_results": subagent_results,
            "subagent_diffs": subagent_diffs,
            "subagent_briefs": subagent_briefs,
            "runtime": runtime,
        }

    async def _session_messages_impl(self, request: web.Request, source: str) -> web.Response:
        self._check_token(request)
        try:
            limit = max(1, min(_MAX_HISTORY_LIMIT, int(request.query.get("limit", str(_DEFAULT_HISTORY_LIMIT)))))
        except ValueError:
            limit = _DEFAULT_HISTORY_LIMIT
        # 增量游标：端侧带上自己已经渲染到的 view_seq，只取之后的帧追加。
        try:
            since_seq = max(0, int(request.query.get("after_view_seq", "0")))
        except ValueError:
            since_seq = 0
        # 只读预取：带 workspace_dir 时读那条线（不切当前会话）。非绝对路径拒绝——
        # 与文件工具的绝对路径约定一致，避免相对路径被解释成进程 cwd 下的某个空间。
        workspace_dir = str(request.query.get("workspace_dir") or "").strip()
        if workspace_dir and not Path(workspace_dir).expanduser().is_absolute():
            return web.json_response({"error": "workspace_dir 必须是绝对路径"}, status=400)
        snapshot = await self._load_view_snapshot(
            source, limit=limit, since_seq=since_seq, workspace_dir=workspace_dir or None
        )
        return web.json_response(snapshot)

    async def _handle_session_new(self, request: web.Request) -> web.Response:
        """Web 顶栏「新会话」：重开 web view 空间会话（图形等价 /new，斜杠已拦）。"""
        self._check_token(request)
        from src.coara.commands import execute_command

        try:
            view_coara = self.root.resolve_view_coara("web")
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        result = await execute_command(
            self.root,
            "/new",
            target_coara=view_coara,
            origin_source="web",
        )
        if result is None:
            return web.json_response({"error": "无法开始新会话"}, status=500)
        # 空间一条线：/new 是线上一个分隔标记（显示与刷新回放一致）。
        self._persist_timeline_divider("新会话", subject="root")
        data = result.data or {}
        return web.json_response(
            {
                "session_id": data.get("session_id") or getattr(view_coara, "session_id", None),
                "output": result.output,
                "action": result.action,
            }
        )

    async def _handle_session_model(self, request: web.Request) -> web.Response:
        """Web 顶栏模型切换：切 web 视图空间模型（图形等价 /model，斜杠已拦）。"""
        self._check_token(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        key = str((body or {}).get("key") or "").strip()
        if not key or key.startswith("-") or any(c.isspace() for c in key):
            return web.json_response({"error": "缺少或无效的模型"}, status=400)

        from src.coara.commands import execute_command

        try:
            view_coara = self.root.resolve_view_coara("web")
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)

        result = await execute_command(
            self.root,
            f"/model {key}",
            target_coara=view_coara,
            origin_source="web",
        )
        if result is None:
            return web.json_response({"error": "无法切换模型"}, status=500)
        data = result.data or {}
        if data.get("error"):
            return web.json_response({"error": result.output or "切换模型失败", "output": result.output}, status=400)

        provider = str(data.get("provider") or "").strip()
        model = str(data.get("model") or "").strip()
        if provider:
            # 本端（web 顶栏）切模型不落分隔线：用户刚点的动作，顶栏已显示当前
            # 模型，线上再报一遍是噪音。他端切模型另走 handlers/workspace.py
            # （origin != web）——那个动作本端看不见，才需要一条线。
            module_roots = getattr(self, "_module_roots", None) or {}
            for module_root in list(module_roots.values()):
                try:
                    module_root.switch_llm(provider, model or None)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to sync module root LLM after session model switch")

        return web.json_response(
            {
                "provider": provider,
                "model": model,
                "deferred": bool(data.get("deferred")),
                "output": result.output,
                "action": result.action,
            }
        )
