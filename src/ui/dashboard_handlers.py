"""REST handlers for the embedded Web UI dashboard"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.config import config_manager
from src.core.errors import ConfigError, SkillError, SkillNotFoundError
from src.core.logger import logger
from src.core.text import preview_line
from src.ui.control_plane import (
    SkillPermissionError,
    SkillValidationError,
    build_config_envelope,
    build_meta_payload_from_binding,
    config_revision,
    discover_skills_payload,
    read_skill_content,
    write_skill_content,
)
from src.ui.dashboard_binding import resolve_dashboard_binding
from src.ui.dashboard_ingest import build_runtime_view
from src.ui.dashboard_tokens import load_or_create_dashboard_token
from src.ui.handlers.base import DashboardAuthMixin
from src.ui.trace_store import TraceStore, _parse_jsonl_text

_DRAFT_ID_PATTERN = re.compile(r"^draft-[a-f0-9]{8}$")


def _validate_draft_id(draft_id: str) -> str:
    """Raise ``ValueError`` if *draft_id* is not a valid draft identifier."""
    draft_id = str(draft_id or "").strip()
    if not _DRAFT_ID_PATTERN.match(draft_id):
        raise ValueError(f"无效的工作流草案 ID：{draft_id!r}")
    return draft_id


class DashboardRestHandlers(DashboardAuthMixin):
    """State + REST handlers borrowed by the embedded Web UI server."""

    _TOOL_OUTPUT_REF_PATTERN = re.compile(r"^[a-f0-9]{8}$")
    # Keep dashboard state cache bounded during long coara + web sessions.
    _MAX_CACHED_EVENTS = 3_000
    _MAX_CACHED_MESSAGES = 1_500
    _MAX_CACHED_SESSIONS = 40
    _MAX_WIRE_EVENTS_PER_SESSION = 350
    _TOOL_OUTPUT_WIRE_LIMIT = 500
    _CONTENT_WIRE_LIMIT = 2_000

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        coara_home: Path | str | None = None,
        store: TraceStore | None = None,
        prefer_active_runtime: bool = False,
        root: Any | None = None,
    ) -> None:
        self.root = root
        self.coara_home: Path | None = Path(coara_home).expanduser().resolve() if coara_home else None
        self._binding = resolve_dashboard_binding(
            Path(workspace_dir),
            coara_home=self.coara_home,
            prefer_active_runtime=prefer_active_runtime,
        )
        self.workspace_dir = self._binding.workspace
        if self.coara_home is None:
            self.coara_home = self._binding.coara_home
        self.store = store if store is not None else TraceStore(self.workspace_dir, coara_home=self.coara_home)
        self.auth_token = load_or_create_dashboard_token(self.workspace_dir, self.coara_home)
        # Incremental state cache to avoid re-reading entire JSONL files on
        # every /api/state call.
        self._cached_state: dict[str, Any] | None = None
        self._cached_events: list[dict[str, Any]] = []
        self._cached_messages: list[dict[str, Any]] = []
        self._event_file_size: int = 0
        self._message_last_seq: int = 0

    def register_routes(self, router: web.UrlDispatcher) -> None:
        """Register all dashboard REST routes on an aiohttp router."""
        router.add_get("/api/state", self.handle_api_state)
        router.add_get("/api/tool-outputs", self.handle_api_tool_outputs)
        router.add_get("/api/tool-output/{ref}", self.handle_api_tool_output)
        # coara 是 WDL 的生产者与文件宿主。
        router.add_get("/api/workflow-drafts", self.handle_api_workflow_drafts_list)
        router.add_post("/api/workflow-drafts", self.handle_api_workflow_draft_create)
        router.add_get("/api/workflow-drafts/{draft_id}", self.handle_api_workflow_draft_get)
        router.add_put("/api/workflow-drafts/{draft_id}", self.handle_api_workflow_draft_put)
        router.add_delete("/api/workflow-drafts/{draft_id}", self.handle_api_workflow_draft_delete)
        router.add_get("/api/workflow-templates", self.handle_api_workflow_templates)
        router.add_post("/api/workflow-drafts/from-template", self.handle_api_workflow_draft_from_template)
        router.add_get("/api/v1/meta", self.handle_api_v1_meta)
        router.add_get("/api/v1/modules", self.handle_api_v1_modules)
        router.add_get("/api/v1/skills", self.handle_api_v1_skills)
        router.add_get("/api/v1/skills/{name}/content", self.handle_api_v1_skill_content)
        router.add_post("/api/v1/skills/{name}/content", self.handle_api_v1_skill_content_save)
        router.add_get("/api/v1/tools", self.handle_api_v1_tools)
        router.add_post("/api/v1/tools/credentials", self.handle_api_v1_tool_credentials_save)
        router.add_get("/api/v1/config", self.handle_api_v1_config)
        router.add_post("/api/v1/config", self.handle_api_v1_config_save)
        router.add_get("/api/v1/providers", self.handle_api_v1_providers)
        router.add_post("/api/v1/providers", self.handle_api_v1_providers_save)
        router.add_get("/api/v1/provider-presets", self.handle_api_v1_provider_presets)
        router.add_post("/api/v1/providers/test", self.handle_api_v1_provider_test)
        router.add_get("/api/v1/model-choices", self.handle_api_v1_model_choices)
        router.add_get("/api/v1/autostart", self.handle_api_v1_autostart)
        router.add_post("/api/v1/autostart", self.handle_api_v1_autostart_set)
        router.add_get("/api/v1/user-rules", self.handle_api_v1_user_rules)
        router.add_post("/api/v1/user-rules", self.handle_api_v1_user_rules_save)
        router.add_post("/api/v1/user-rules/open", self.handle_api_v1_user_rules_open)
        router.add_get("/api/v1/thinking", self.handle_api_v1_thinking)
        router.add_post("/api/v1/thinking", self.handle_api_v1_thinking_save)
        router.add_get("/api/v1/default-model", self.handle_api_v1_default_model)
        router.add_post("/api/v1/default-model", self.handle_api_v1_default_model_save)

    def reset_state_cache(self) -> None:
        """Drop incremental-read cursors; call after swapping ``store``/``workspace_dir``."""
        self._cached_state = None
        self._cached_events = []
        self._cached_messages = []
        self._event_file_size = 0
        self._message_last_seq = 0

    async def handle_api_state(self, request: web.Request) -> web.Response:
        """遗留仪表盘状态接口：runtime + L1 会话磁带投影出的 sessions"""
        self._check_token(request)
        state = await self._build_state()
        return web.json_response(state)

    async def handle_api_tool_outputs(self, request: web.Request) -> web.Response:
        self._check_token(request)
        session_id = (request.query.get("session_id") or "").strip() or None
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))
        from src.runtime.tool_output_store import list_tool_output_index

        rows = await asyncio.to_thread(
            list_tool_output_index,
            workspace_dir=self.workspace_dir,
            session_id=session_id,
            coara_home=self.coara_home,
            limit=limit,
        )
        return web.json_response({"outputs": rows, "count": len(rows)})

    async def handle_api_tool_output(self, request: web.Request) -> web.Response:
        self._check_token(request)
        ref = (request.match_info.get("ref") or "").strip()
        if not self._TOOL_OUTPUT_REF_PATTERN.match(ref):
            raise web.HTTPBadRequest(text="Invalid ref")
        session_id = (request.query.get("session_id") or "").strip() or None
        try:
            line_offset = max(1, int(request.query.get("offset", "1")))
        except ValueError:
            line_offset = 1
        try:
            line_limit = max(1, min(int(request.query.get("limit", "500")), 5_000))
        except ValueError:
            line_limit = 500
        from src.runtime.spill_read import read_spill_api_payload
        from src.runtime.tool_output_store import find_tool_output_record

        try:
            record = await asyncio.to_thread(
                find_tool_output_record,
                workspace_dir=self.workspace_dir,
                ref=ref,
                session_id=session_id,
                coara_home=self.coara_home,
            )
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc

        payload = read_spill_api_payload(record.content, line_offset=line_offset, line_limit=line_limit)
        return web.json_response(
            {
                "ref": record.ref,
                "session_id": record.session_id,
                "tool_name": record.tool_name,
                "tool_call_id": record.tool_call_id,
                "bytes": record.bytes,
                "created_at": record.created_at,
                "arguments": record.arguments,
                **payload,
            }
        )

    def _workflow_draft_store(self):
        from src.workflow.draft_store import WorkflowDraftStore

        return WorkflowDraftStore(coara_home=self.coara_home)

    async def handle_api_workflow_templates(self, request: web.Request) -> web.Response:
        """列出内置工作流模板（src/workflow/templates/*.yaml 内核投影）。"""
        self._check_token(request)
        from src.workflow.core.serde import parse_graph

        templates = []
        templates_dir = Path(__file__).resolve().parents[1] / "workflow" / "templates"
        for path in sorted(templates_dir.glob("*.yaml")):
            try:
                graph = parse_graph(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Skipping invalid workflow template {path.name}: {exc}")
                continue
            templates.append(
                {
                    "file": path.name,
                    "name": graph.name,
                    "description": graph.description or "",
                    "nodes": len(graph.nodes),
                    "edges": len(graph.edges),
                }
            )
        return web.json_response({"templates": templates})

    async def handle_api_workflow_draft_create(self, request: web.Request) -> web.Response:
        """创建空草案（工作台常驻编辑器用）；可选 name，空名按现有草案自动编号。"""
        self._check_token(request)
        from src.workflow.draft_service import save_draft
        from src.workflow.draft_store import WorkflowDraftStore

        name = ""
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("name"), str) and body["name"].strip():
                name = body["name"].strip()
        except json.JSONDecodeError:
            pass
        if not name:
            # 草案名是列表与顶栏的显示身份，全部同名等于全部匿名。
            from src.workflow.draft_service import list_drafts

            store = self._workflow_draft_store()
            try:
                data = await asyncio.to_thread(list_drafts, store)
                used = {str(d.get("name") or "") for d in data.get("drafts", [])}
            except Exception:
                used = set()
            seq = 0
            while True:
                seq += 1
                candidate = f"未命名工作流-{seq}"
                if candidate not in used:
                    name = candidate
                    break
        # 最小可保存图：一个空智能体节点（nodes: {} 校验不通过）
        seed_wdl = f"name: {name}\nnodes:\n  节点1: {{}}\nedges: []\n"
        try:
            payload = await asyncio.to_thread(
                save_draft,
                self._workflow_draft_store(),
                WorkflowDraftStore.generate_id(),
                seed_wdl,
                source_subagent="web-workbench",
                allow_create=True,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_api_workflow_draft_from_template(self, request: web.Request) -> web.Response:
        """从模板创建草案：复制模板文本为新草案（与 save 同一套校验规范化）。"""
        self._check_token(request)
        from src.workflow.draft_service import save_draft

        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        file_name = body.get("file") if isinstance(body, dict) else None
        if not isinstance(file_name, str) or not file_name.strip():
            raise web.HTTPBadRequest(text="缺少 file（模板文件名）")
        from src.workflow.draft_store import WorkflowDraftStore

        templates_dir = Path(__file__).resolve().parents[1] / "workflow" / "templates"
        template_path = templates_dir / Path(file_name).name  # 拒绝路径穿越
        if not template_path.exists():
            raise web.HTTPNotFound(text=f"找不到工作流模板：{file_name}")
        try:
            payload = await asyncio.to_thread(
                save_draft,
                self._workflow_draft_store(),
                WorkflowDraftStore.generate_id(),
                template_path.read_text(encoding="utf-8"),
                source_subagent="template",
                allow_create=True,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(payload)

    async def handle_api_workflow_drafts_list(self, request: web.Request) -> web.Response:
        self._check_token(request)
        from src.workflow.draft_service import list_drafts

        payload = await asyncio.to_thread(list_drafts, self._workflow_draft_store())
        return web.json_response(payload)

    async def handle_api_workflow_draft_get(self, request: web.Request) -> web.Response:
        self._check_token(request)
        from src.workflow.draft_service import get_draft

        draft_id = _validate_draft_id(request.match_info.get("draft_id", ""))
        payload = await asyncio.to_thread(get_draft, self._workflow_draft_store(), draft_id)
        if payload is None:
            raise web.HTTPNotFound(text="找不到工作流草案")
        return web.json_response(payload)

    async def handle_api_workflow_draft_put(self, request: web.Request) -> web.Response:
        self._check_token(request)
        from src.workflow.draft_service import save_draft

        draft_id = _validate_draft_id(request.match_info.get("draft_id", ""))
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="无效的 JSON 请求体") from exc
        wdl_text = body.get("wdl") if isinstance(body, dict) else None
        if not isinstance(wdl_text, str) or not wdl_text.strip():
            raise web.HTTPBadRequest(text="缺少 WDL 文本")
        # 共同编辑冲突检测：客户端带读取基线 updated_at 时，草案已被他方
        # （会话写穿/其他客户端）推进 → 409，由客户端刷新后再改（不静默覆盖）
        base_updated_at = str(body.get("base_updated_at") or "").strip() if isinstance(body, dict) else ""
        if base_updated_at:
            store = self._workflow_draft_store()
            current = await asyncio.to_thread(store.get, draft_id)
            if current is not None and current.updated_at != base_updated_at:
                return web.json_response(
                    {
                        "error": "草案已被会话侧更新，画布需刷新后再编辑",
                        "conflict": True,
                        "updated_at": current.updated_at,
                    },
                    status=409,
                )
        try:
            payload = await asyncio.to_thread(
                save_draft,
                self._workflow_draft_store(),
                draft_id,
                wdl_text,
            )
        except KeyError:
            raise web.HTTPNotFound(text="找不到工作流草案") from None
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        # UI 编辑 → 会话感知：绑定了该草案的会话内 flow 标 stale，
        # 下次 run/wait 前从草案重建（会话永远面对最新图）
        self._mark_bound_flow_stale(draft_id)
        return web.json_response(payload)

    def _mark_bound_flow_stale(self, draft_id: str) -> None:
        """扫描 Root 主体（前台 + 各工作空间会话 + FlowRoot）的 flow_coordinator。"""
        root = getattr(self, "root", None)
        if root is None:
            return
        candidates: list[Any] = []
        fg = getattr(root, "foreground_coara", None)
        if fg is not None:
            candidates.append(fg)
        for session in list(getattr(root, "_sessions", {}).values()):
            coara = getattr(session, "coara", None)
            if coara is not None:
                candidates.append(coara)
        flow_root = getattr(root, "_flow_root", None)
        if flow_root is not None:
            candidates.append(flow_root)
        for coara in candidates:
            coord = getattr(coara, "flow_coordinator", None)
            if coord is None:
                continue
            flow = coord.flow_for_draft(draft_id)
            if flow is not None:
                coord.mark_stale(flow)
                logger.info(f"Flow {flow} marked stale (draft {draft_id} edited via UI)")

    async def handle_api_workflow_draft_delete(self, request: web.Request) -> web.Response:
        self._check_token(request)

        draft_id = _validate_draft_id(request.match_info.get("draft_id", ""))
        store = self._workflow_draft_store()
        draft = await asyncio.to_thread(store.get, draft_id)
        if draft is None:
            raise web.HTTPNotFound(text="找不到工作流草案")
        deleted = await asyncio.to_thread(store.delete, draft_id)
        if not deleted:
            raise web.HTTPNotFound(text="找不到工作流草案")
        return web.json_response({"deleted": True, "draft_id": draft_id})

    async def _build_state(self) -> dict[str, Any]:
        state, _snapshot = await asyncio.to_thread(self._load_state_with_snapshot)
        return state

    def _conversation_tape_path(self) -> Path:
        from src.session_log.store import resolve_session_log_path

        return resolve_session_log_path(self.workspace_dir, coara_home=self.coara_home)

    def _load_messages_full(self) -> list[dict[str, Any]]:
        """全量重放 L1 磁带投影对话行（游标对齐磁带末尾）。"""
        from src.session_log.conversation_projection import iter_conversation_rows
        from src.session_log.store import last_seq

        tape = self._conversation_tape_path()
        rows = list(iter_conversation_rows(tape))
        self._message_last_seq = last_seq(tape)
        return self._trim_cached_rows(rows, self._MAX_CACHED_MESSAGES, store=self.store)

    def _load_messages_incremental(self) -> list[dict[str, Any]]:
        """seq 游标增量投影；新事件含影子标记（回溯失效已投影行）时全量重投影。"""
        from src.session_log.conversation_projection import contains_shadow_events, project_events
        from src.session_log.store import iter_events, last_seq

        tape = self._conversation_tape_path()
        current = last_seq(tape)
        if current == self._message_last_seq:
            return self._cached_messages
        if current < self._message_last_seq:
            # 磁带被清空/换绑：全量重投影
            return self._load_messages_full()
        cursor = self._message_last_seq
        new_events = []
        for event in iter_events(tape):
            try:
                seq = int(event.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq > cursor:
                new_events.append(event)
        if contains_shadow_events(new_events):
            return self._load_messages_full()
        new_rows = project_events(new_events)
        self._message_last_seq = current
        if not new_rows:
            return self._cached_messages
        return self._trim_cached_rows(
            self._cached_messages + new_rows,
            self._MAX_CACHED_MESSAGES,
            store=self.store,
        )

    def _load_state_with_snapshot(self) -> tuple[dict[str, Any], str]:
        # Ensure async trace writes from this process are visible before incremental reads.
        self.store.flush(timeout=0.25)
        if self._cached_state is None:
            # First-time full load
            events, self._event_file_size = self.store.load_events()
            self._cached_events = self._trim_cached_rows(events, self._MAX_CACHED_EVENTS, store=self.store)
            self._cached_messages = self._load_messages_full()
            runtime = build_runtime_view(self.store.load_runtime_state())
            self._cached_state = self._compose_cached_state(runtime)
        else:
            # Incremental update
            new_events, self._event_file_size = self._read_jsonl_incremental(
                self.store.events_path, self._cached_events, self._event_file_size
            )
            new_messages = self._load_messages_incremental()
            if new_events is not self._cached_events:
                self._cached_events = new_events
            if new_messages is not self._cached_messages:
                self._cached_messages = new_messages
            runtime = self.store.load_runtime_state()
            self._cached_state = self._compose_cached_state(
                build_runtime_view(runtime),
            )
        revision = self._state_revision(self._cached_state)
        return self._cached_state, revision

    @staticmethod
    def _trim_cached_rows(
        rows: list[dict[str, Any]],
        limit: int,
        *,
        store: TraceStore | None = None,
    ) -> list[dict[str, Any]]:
        if len(rows) <= limit:
            return rows
        if store is not None:
            store.reset_session_cache()
        return rows[-limit:]

    def _compose_cached_state(self, runtime: dict[str, Any]) -> dict[str, Any]:
        sessions = self.store._build_sessions(self._cached_messages, self._cached_events)
        if len(sessions) > self._MAX_CACHED_SESSIONS:
            sessions = sessions[: self._MAX_CACHED_SESSIONS]
        return {
            "runtime": runtime,
            "sessions": self._slim_sessions_for_wire(sessions),
        }

    @classmethod
    def _slim_payload_for_wire(cls, payload: dict[str, Any], event_type: str) -> dict[str, Any]:
        slim = dict(payload)
        if event_type == "tool_call":
            output = slim.get("tool_output")
            if isinstance(output, str):
                slim["tool_output"] = preview_line(output, cls._TOOL_OUTPUT_WIRE_LIMIT)
            elif output is not None:
                encoded = json.dumps(output, ensure_ascii=False)
                if len(encoded) > cls._TOOL_OUTPUT_WIRE_LIMIT:
                    slim.pop("tool_output", None)
                    slim["tool_output_preview"] = preview_line(encoded, cls._TOOL_OUTPUT_WIRE_LIMIT)
            for key in ("output_ref", "output_bytes", "output_spilled", "session_id", "tool_call_id"):
                if key in payload:
                    slim[key] = payload[key]
        if event_type == "conversation_message" and slim.get("role") == "assistant":
            content = slim.get("content")
            if isinstance(content, str) and len(content) > cls._CONTENT_WIRE_LIMIT:
                slim["content"] = content[: cls._CONTENT_WIRE_LIMIT] + "..."
        return slim

    @classmethod
    def _slim_event_for_wire(cls, event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return event
        event_type = str(event.get("event_type") or "")
        slim_payload = cls._slim_payload_for_wire(payload, event_type)
        if slim_payload is payload:
            return event
        slim_event = dict(event)
        slim_event["payload"] = slim_payload
        return slim_event

    def _slim_sessions_for_wire(self, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        slim_sessions: list[dict[str, Any]] = []
        for session in sessions:
            events = session.get("events") or []
            if len(events) > self._MAX_WIRE_EVENTS_PER_SESSION:
                events = events[-self._MAX_WIRE_EVENTS_PER_SESSION :]
            subagent_sessions = []
            for sub in session.get("subagent_sessions") or []:
                sub_events = sub.get("events") or []
                if len(sub_events) > self._MAX_WIRE_EVENTS_PER_SESSION:
                    sub_events = sub_events[-self._MAX_WIRE_EVENTS_PER_SESSION :]
                subagent_sessions.append(
                    {
                        **sub,
                        "events": [self._slim_event_for_wire(ev) for ev in sub_events if isinstance(ev, dict)],
                    }
                )
            slim_sessions.append(
                {
                    **{k: v for k, v in session.items() if k not in ("messages", "events", "subagent_sessions")},
                    "events": [self._slim_event_for_wire(ev) for ev in events if isinstance(ev, dict)],
                    "subagent_sessions": subagent_sessions,
                }
            )
        return slim_sessions

    @staticmethod
    def _state_revision(state: dict[str, Any]) -> str:
        runtime = state.get("runtime") or {}
        sessions = state.get("sessions") or []
        parts = [
            str(runtime.get("updated_at") or ""),
            "1" if runtime.get("running") else "0",
            "1" if runtime.get("alive") else "0",
            str(len(sessions)),
        ]
        if sessions:
            top = sessions[0]
            parts.extend(
                [
                    str(top.get("updated_at") or ""),
                    str(len(top.get("events") or [])),
                ]
            )
            events = top.get("events") or []
            if events:
                last = events[-1]
                parts.extend([str(last.get("timestamp") or ""), str(last.get("event_type") or "")])
        return "|".join(parts)

    def _read_jsonl_incremental(
        self,
        path: Path,
        cached_rows: list[dict[str, Any]],
        last_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if not path.exists():
            return cached_rows, 0
        # 重读（最多 50MB）不再连解析一起持锁阻塞写线程
        with self.store.io_lock:
            current_size = path.stat().st_size
            if current_size == last_size:
                return cached_rows, last_size
            truncated = current_size < last_size
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                if not truncated:
                    handle.seek(last_size)
                raw_text = handle.read()

        new_rows = _parse_jsonl_text(raw_text)
        if truncated:
            # File was rotated or truncated — reload from scratch.
            self.store.reset_session_cache()
            return self._trim_cached_rows(new_rows, self._max_rows_for_path(path), store=self.store), current_size
        if not new_rows:
            return cached_rows, current_size
        combined = cached_rows + new_rows
        trimmed = self._trim_cached_rows(combined, self._max_rows_for_path(path), store=self.store)
        return trimmed, current_size

    def _max_rows_for_path(self, path: Path) -> int:
        if path.name == self.store.events_path.name:
            return self._MAX_CACHED_EVENTS
        return 10_000

    async def _discover_skills_payload(self) -> list[dict[str, str]]:
        return await discover_skills_payload(self.workspace_dir)

    async def handle_api_v1_meta(self, request: web.Request) -> web.Response:
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        return web.json_response(build_meta_payload_from_binding(self._binding))

    async def handle_api_v1_modules(self, request: web.Request) -> web.Response:
        """Sidebar module registry (plugin contract) for the Web UI."""
        self._check_token(request)
        from src.coara.module_registry import module_registry

        return web.json_response(module_registry.to_dict())

    async def handle_api_v1_skills(self, request: web.Request) -> web.Response:
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        skills = await self._discover_skills_payload()
        skills_cfg = config_manager.get_raw_config().get("skills", {})
        default_include = list(skills_cfg.get("default_include") or [])
        return web.json_response(
            {
                "skills": skills,
                "default_include": default_include,
                "pool_count": len(skills),
                "recommended_count": len(default_include) if default_include != ["*"] else len(skills),
            }
        )

    async def handle_api_v1_skill_content(self, request: web.Request) -> web.Response:
        """读技能 SKILL.md 原文：锁定 frontmatter + 可编辑正文。"""
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        name = (request.match_info.get("name") or "").strip()
        try:
            payload = await read_skill_content(name, self.workspace_dir, self.coara_home)
        except SkillNotFoundError as exc:
            raise web.HTTPNotFound(text=f"技能不存在：{name}") from exc
        except SkillError as exc:
            raise web.HTTPInternalServerError(text=str(exc)) from exc
        return web.json_response(payload)

    # 技能正文体积上限（字符）；防误贴超大文件撑爆激活注入
    _SKILL_BODY_MAX_CHARS = 200_000

    async def handle_api_v1_skill_content_save(self, request: web.Request) -> web.Response:
        """写回技能正文：body {"body": str}，frontmatter 原样保留。"""
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        name = (request.match_info.get("name") or "").strip()
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        body = payload.get("body")
        if not isinstance(body, str):
            raise web.HTTPBadRequest(text="body must be a string")
        if len(body) > self._SKILL_BODY_MAX_CHARS:
            raise web.HTTPBadRequest(text=f"body too long（上限 {self._SKILL_BODY_MAX_CHARS} 字符）")
        try:
            result = await write_skill_content(name, body, self.workspace_dir, self.coara_home, root=self.root)
        except SkillNotFoundError as exc:
            raise web.HTTPNotFound(text=f"技能不存在：{name}") from exc
        except SkillPermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except SkillValidationError as exc:
            raise web.HTTPBadRequest(text=f"技能内容校验未通过：{exc}") from exc
        except SkillError as exc:
            raise web.HTTPInternalServerError(text=str(exc)) from exc
        return web.json_response(result)

    async def handle_api_v1_tools(self, request: web.Request) -> web.Response:
        """配置页「工具」tab：全量工具清单 + 凭据状态（root 缺失时按全局注册表兜底）。"""
        self._check_token(request)
        from src.ui.tools_payload import build_tools_payload

        return web.json_response(build_tools_payload(self.root))

    async def handle_api_v1_tool_credentials_save(self, request: web.Request) -> web.Response:
        """写工具凭据到 system/.env。body: {"name", "values": {ENV: "..."}}。密钥不回传。"""
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        name = str(payload.get("name") or "").strip()
        values = payload.get("values")
        if not isinstance(values, dict):
            raise web.HTTPBadRequest(text="values 必须是对象")
        clean = {str(key): str(value) for key, value in values.items() if isinstance(key, str)}
        from src.ui.tool_credentials import save_tool_credentials

        try:
            written = save_tool_credentials(name, clean, root=self.root)
        except ConfigError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"ok": True, "written": written})

    async def handle_api_v1_config(self, request: web.Request) -> web.Response:
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        return web.json_response(build_config_envelope())

    async def handle_api_v1_config_save(self, request: web.Request) -> web.Response:
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        try:
            await self._save_config_payload(payload)
        except ConfigError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except Exception as exc:
            logger.error(f"Failed to save config: {exc}")
            raise web.HTTPInternalServerError(text=str(exc)) from exc
        return web.json_response({"ok": True, "revision": config_revision()})

    async def handle_api_v1_autostart(self, request: web.Request) -> web.Response:
        """开机自启状态：读 OS 层注册项（唯一事实源），不落配置文件。"""
        self._check_token(request)
        from src.cli import autostart as _autostart

        return web.json_response({"enabled": _autostart.is_enabled(), "command": _autostart._launcher_command()})

    async def handle_api_v1_autostart_set(self, request: web.Request) -> web.Response:
        """开机自启开关：body {"enabled": bool}。"""
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise web.HTTPBadRequest(text="enabled must be a boolean")
        from src.cli import autostart as _autostart

        try:
            if enabled:
                _autostart.enable()
            else:
                _autostart.disable()
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f"写入失败：{exc}") from exc
        return web.json_response({"ok": True, "enabled": _autostart.is_enabled()})

    # 用户规则体积上限（字符）；防误贴超大文件撑爆注入上下文
    _USER_RULES_MAX_CHARS = 100_000

    def _user_rules_path(self) -> Path:
        from src.core.coara_home import system_dir_for_home

        home = self.coara_home
        if home is None:
            from src.core.coara_home import resolve_coara_home

            home = resolve_coara_home(self.workspace_dir, None)
        return system_dir_for_home(home) / "user_rules.md"

    async def handle_api_v1_user_rules(self, request: web.Request) -> web.Response:
        """读用户规则（system/user_rules.md）；文件不存在返回空串。"""
        self._check_token(request)
        path = self._user_rules_path()
        content = ""
        try:
            if path.is_file():
                content = path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        return web.json_response({"content": content, "path": str(path)})

    async def handle_api_v1_user_rules_save(self, request: web.Request) -> web.Response:
        """写用户规则：body {"content": str}。环境种子首回合一播，新会话生效。"""
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        content = payload.get("content")
        if not isinstance(content, str):
            raise web.HTTPBadRequest(text="content must be a string")
        if len(content) > self._USER_RULES_MAX_CHARS:
            raise web.HTTPBadRequest(text=f"content too long（上限 {self._USER_RULES_MAX_CHARS} 字符）")
        path = self._user_rules_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f"写入失败：{exc}") from exc
        return web.json_response({"ok": True, "path": str(path)})

    async def handle_api_v1_user_rules_open(self, request: web.Request) -> web.Response:
        """用系统默认编辑器打开 user_rules.md；文件不存在先建空的。"""
        self._check_token(request)
        import os
        import subprocess
        import sys

        path = self._user_rules_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])  # noqa: S603
            else:
                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f"打开失败：{exc}") from exc
        return web.json_response({"ok": True, "path": str(path)})

    async def handle_api_v1_thinking(self, request: web.Request) -> web.Response:
        """读思考档位配置基线（llm_profiles.agent.main.thinking）。"""
        self._check_token(request)
        from src.llm.thinking_mode import persisted_thinking_value

        if config_manager._config is None:
            await config_manager.load()
        value = persisted_thinking_value()
        # 配置基线为会话级全局（内存），重启后由 llm_service.configure 从文件装载；
        # 文件真源再核对一遍，防未重启内核时读到空基线
        if not value:
            profile = config_manager._llm_profiles.get("agent.main")
            value = str(getattr(profile, "thinking", "") or "")
        return web.json_response({"thinking": value})

    async def handle_api_v1_thinking_save(self, request: web.Request) -> web.Response:
        """写思考档位：body {"thinking": "on|off|low|medium|high|""}；即时生效。"""
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        value = payload.get("thinking")
        if not isinstance(value, str):
            raise web.HTTPBadRequest(text="thinking must be a string")
        value = value.strip().lower()
        if value and value not in ("on", "off", "low", "medium", "high"):
            raise web.HTTPBadRequest(text="thinking 取值：on / off / low / medium / high")
        if config_manager._config is None:
            await config_manager.load()
        from src.llm.model_persist import persist_thinking_setting

        try:
            path = persist_thinking_setting(config_manager, value)
        except Exception as exc:
            logger.error(f"Failed to save thinking setting: {exc}")
            raise web.HTTPInternalServerError(text=str(exc)) from exc
        return web.json_response({"ok": True, "thinking": value, "path": str(path)})

    async def handle_api_v1_default_model(self, request: web.Request) -> web.Response:
        """读全局默认模型（llm_preferences 合并视图）。"""
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        raw = config_manager.get_raw_config()
        return web.json_response(
            {
                "default_provider": str(raw.get("default_provider") or ""),
                "default_model": str(raw.get("default_model") or ""),
            }
        )

    async def handle_api_v1_default_model_save(self, request: web.Request) -> web.Response:
        """设全局默认模型：body {"key": "provider/model"}；等价 /model --global，即时生效。

        只影响未绑定 provider 的空间与新会话；已绑定空间保留自身绑定。
        """
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc
        key = str(payload.get("key") or "").strip()
        if "/" not in key:
            raise web.HTTPBadRequest(text="key 形如 provider/model")
        provider, model = key.split("/", 1)
        provider, model = provider.strip(), model.strip()
        if not provider or not model:
            raise web.HTTPBadRequest(text="key 形如 provider/model")
        if config_manager._config is None:
            await config_manager.load()
        root = getattr(self, "root", None)
        if root is None:
            raise web.HTTPInternalServerError(text="内核未就绪")
        try:
            applied_provider, applied_model = root.switch_llm_global(provider, model, origin_source="web")
        except Exception as exc:
            logger.error(f"Failed to save default model: {exc}")
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"ok": True, "default_provider": applied_provider, "default_model": applied_model})

    async def handle_api_v1_model_choices(self, request: web.Request) -> web.Response:
        """统一模型组合列表：所有已配置且有 API key 的 provider 声明的可用模型。"""
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        from src.llm.model_catalog import list_model_choices

        catalog = [
            {"provider": c.provider, "model": c.model_id, "key": c.key, "label": c.label}
            for c in list_model_choices(config_manager)
        ]
        return web.json_response({"catalog": catalog})

    async def handle_api_v1_providers(self, request: web.Request) -> web.Response:
        self._check_token(request)
        if config_manager._config is None:
            await config_manager.load()
        from src.ui.control_plane import build_providers_envelope

        return web.json_response(build_providers_envelope())

    async def handle_api_v1_providers_save(self, request: web.Request) -> web.Response:
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc

        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            raise web.HTTPBadRequest(text="providers must be an object")

        # echoing the mask back never wipes inline api keys.
        raw_providers = config_manager.get_raw_config().get("providers") or {}

        def _restore_masked(new: Any, old: Any) -> Any:
            if isinstance(new, dict) and isinstance(old, dict):
                return {k: _restore_masked(v, old.get(k)) for k, v in new.items()}
            if new == "***":
                if old is None or (isinstance(old, dict) and not old):
                    # 新建位置收到掩码占位符：不存在可还原的旧值，
                    # 原样落盘会把 "***" 当真实密钥保存——拒绝并要求填真值
                    raise web.HTTPBadRequest(text="新配置项不能填掩码占位符 ***，请填写真实值或留空")
                return old
            return new

        providers = _restore_masked(providers, raw_providers)

        # 存量内联 key 也在此一并迁出（幂等，无 inline 时不碰 .env）。
        from src.core.config import migrate_inline_api_keys_to_env

        providers = migrate_inline_api_keys_to_env(providers)

        try:
            config_manager.save_providers_yaml({"providers": providers})
        except Exception as exc:
            logger.error(f"Failed to save providers: {exc}")
            raise web.HTTPInternalServerError(text=str(exc)) from exc

        # 保存即生效：重读磁盘 + 热重载 provider（新增注册/变更重建/移除关闭），
        # 填完 key 要重启或手动 /model 才生效）。
        hot_reload_ok = True
        healed_provider = ""
        try:
            await config_manager.reload()
            from src.llm.registry import reload_providers

            await reload_providers(config_manager)
            # 默认为空或已失效时对齐到有 key 的声明序首位；活会话切绑由
            # providers_changed → Root._subscribe_providers_changed 统一完成。
            from src.cli.first_run_setup import heal_default_provider_if_needed

            healed = heal_default_provider_if_needed(None)
            if healed:
                await config_manager.reload()
                healed_provider = healed
            # 模型 available[].thinking 可能刚改：按当前活动模型重装思考基线
            with contextlib.suppress(Exception):
                from src.llm.thinking_mode import apply_config_thinking

                root = self.root
                provider = str(getattr(root, "provider_name", "") or "")
                model = str(getattr(root, "model_name", "") or "")
                if not provider:
                    provider = str(config_manager.config.default_provider or "")
                if not model:
                    model = str(config_manager.config.default_model or "")
                apply_config_thinking(config_manager, provider, model)
        except Exception:
            hot_reload_ok = False
            logger.warning("providers saved but hot reload failed; restart to apply", exc_info=True)
        # 热重载失败不广播 providers_changed——否则配置页以为已生效、各端
        # chrome 刷成新值，实际 provider 实例还是旧的。失败时明告前端重启生效。
        if hot_reload_ok:
            from src.core.providers_events import broadcast_providers_changed

            broadcast_providers_changed()
            if healed_provider:
                # heal 改写偏好在第一帧之后；再广播一帧让订阅方读到新默认。
                broadcast_providers_changed()

        return web.json_response({"ok": True, "hot_reload": hot_reload_ok})

    async def handle_api_v1_provider_presets(self, request: web.Request) -> web.Response:
        self._check_token(request)
        from src.llm.provider_presets import list_provider_presets

        return web.json_response({"presets": list_provider_presets()})

    async def handle_api_v1_provider_test(self, request: web.Request) -> web.Response:
        """Lightweight connectivity check for a provider draft"""
        self._check_token(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text="Invalid JSON") from exc

        base_url = str(payload.get("base_url") or "").strip()
        if not base_url:
            return web.json_response({"ok": False, "detail": "缺少 Base URL"})
        api_key = str(payload.get("api_key") or "").strip()
        name = str(payload.get("name") or "").strip()
        if api_key == "***":
            api_key = ""
        if not api_key and name:
            # Resolve the real key from stored config (inline first, then env).
            try:
                api_key = config_manager.get_api_key(name)
            except Exception:
                api_key = ""

        import httpx

        # Probe the conventional model-list endpoint for OpenAI-compatible
        # drivers; it requires auth, so a 200/401/404 tells us reachability.
        probe = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{probe}/models", headers=headers)
        except Exception as exc:
            return web.json_response({"ok": False, "detail": f"连接失败：{exc.__class__.__name__}"})

        if resp.status_code in (200, 201):
            return web.json_response({"ok": True, "detail": "连接成功，密钥有效"})
        if resp.status_code in (401, 403):
            return web.json_response({"ok": False, "detail": "已连通，但 API Key 无效或未填写"})
        if resp.status_code == 404:
            # Endpoint reachable but no /models route — still a good sign.
            return web.json_response({"ok": True, "detail": "服务可达（未提供模型列表接口）"})
        return web.json_response({"ok": False, "detail": f"服务返回异常（HTTP {resp.status_code}）"})

    async def _save_config_payload(self, payload: dict[str, Any]) -> None:
        # Keys owned by providers.yaml (separate save API). Writing them into
        # config.yaml would shadow providers.yaml on next load (config loads later).
        _providers_domain = frozenset({"providers", "llm_profiles"})

        def _strip_for_config_save(obj: Any) -> Any:
            if isinstance(obj, dict):
                cleaned: dict[str, Any] = {}
                for k, v in obj.items():
                    if v == "***":
                        continue
                    # Display-only keys enriched for UI display
                    if isinstance(k, str) and k.startswith("_"):
                        continue
                    cleaned[k] = _strip_for_config_save(v)
                return cleaned
            if isinstance(obj, list):
                return [_strip_for_config_save(item) for item in obj]
            return obj

        cleaned = _strip_for_config_save(payload)
        if isinstance(cleaned, dict):
            for key in _providers_domain:
                cleaned.pop(key, None)
        if not cleaned:
            return
        config_manager.save_config_yaml(cleaned)
