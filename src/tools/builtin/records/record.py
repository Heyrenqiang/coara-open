"""record tool — write/manage agent-side local records.

Registered on foreground WorkspaceSession (cache-aligned with janitor) and on
janitor / daily. Root host itself does not need this tool for user turns.
User favorites are hand-click only (Web/手机/端上按钮 → facade)；本工具不写 origin=user。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.records.agent_types import MEMORY_TYPES
from src.records.facade import RecordsFacade

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.records.store import RecordsStore


class RecordTool(BaseTool):
    name = "record"
    display_name = "Record"
    description = """记笔记，一条一条写入本地记录

- add：新记一条（content 正文；可选 title / type / tags）
- update：按 id 改一条正文
- remove：按 id 删一条，只删 agent 笔记，勿动用户收藏
- archive / unarchive：按 id 归档或恢复
- digest_write：写某日简报（date=YYYY-MM-DD, content=Markdown）

记重要的东西,记事实，记用户的观点想法意图..."""
    kind = ToolKind.OTHER
    category = "records"
    owner_only = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add",
                    "update",
                    "remove",
                    "archive",
                    "unarchive",
                    "digest_write",
                ],
                "description": "操作类型",
            },
            "content": {
                "type": "string",
                "description": "正文（add / update）/ 日报 Markdown（digest_write）",
            },
            "title": {"type": "string", "description": "标题，可选"},
            "type": {
                "type": "string",
                "enum": sorted(MEMORY_TYPES),
                "description": "笔记类型，默认 event（add）",
            },
            "context": {"type": "string", "description": "当时情境（add）"},
            "reason": {"type": "string", "description": "决策理由等（add）"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "标签列表",
            },
            "scope": {
                "type": "string",
                "enum": ["session", "workspace", "user", "global"],
                "description": "作用域，默认 user（add）",
            },
            "id": {"type": "string", "description": "记录 ID（update/remove/archive/unarchive）"},
            "date": {
                "type": "string",
                "description": "日报日期 YYYY-MM-DD（digest_write）",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        facade: RecordsFacade | None = None,
        *,
        store: RecordsStore | None = None,
        parent_coara: CoaraBase | None = None,
    ):
        super().__init__()
        if facade is not None:
            self._facade = facade
        else:
            self._facade = RecordsFacade(store)
        self._parent = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return RecordInvocation(params, self._facade, self._parent)


class RecordInvocation(ToolInvocation):
    def __init__(
        self,
        params: dict[str, Any],
        facade: RecordsFacade,
        parent: CoaraBase | None,
    ):
        super().__init__(params)
        self._facade = facade
        self._parent = parent
        self.action = str(params.get("action") or "").strip()

    def get_description(self) -> str:
        return f"record:{self.action}"

    def _session_id(self) -> str:
        if self._parent is not None:
            return str(getattr(self._parent, "session_id", "") or "") or "unknown"
        return "unknown"

    def _provenance(self) -> dict[str, Any]:
        """录像带溯源坐标（自动加盖，不经 LLM 手写）。"""
        if self._parent is None:
            return {}
        import time as _time

        workspace = str(getattr(self._parent, "workspace_dir", "") or "")
        if not workspace:
            return {}
        tape_start: float | None = None
        persona = getattr(getattr(self._parent, "identity", None), "persona", None)
        persona_name = str(getattr(persona, "name", "") or "").strip().lower()
        if persona_name == "janitor":
            try:
                from src.coara.workspace_protocol import load_janitor_activity
                from src.core.coara_home import resolve_coara_home

                manager = getattr(self._parent, "workspace_manager", None)
                home = getattr(manager, "coara_home", None) or resolve_coara_home(Path(workspace), None)
                tape_start = load_janitor_activity(workspace, str(home))
            except Exception:
                tape_start = None
        return {"workspace": workspace, "tape_start": tape_start, "tape_end": _time.time()}

    def _tags(self) -> list[str]:
        tags_raw = self.params.get("tags") or []
        if not isinstance(tags_raw, list):
            return []
        return [str(t).strip() for t in tags_raw if str(t).strip()]

    async def execute(self, signal: Any = None) -> ToolResult:
        action = self.action
        if action == "add":
            origin = str(self.params.get("origin") or "").strip().lower()
            if origin == "user":
                return ToolResult.error(
                    "用户收藏只能由端上手点（文件页/手机卡片等），不能用 record 代写"
                )
            if origin and origin != "agent":
                return ToolResult.error(
                    f"record 仅写 agent 笔记，不支持 origin={origin!r}（省略或 agent）"
                )
            return self._wrap(
                await self._facade.add_agent(
                    content=str(self.params.get("content") or ""),
                    title=str(self.params.get("title") or ""),
                    type_=str(self.params.get("type") or "event").strip() or "event",
                    context=str(self.params.get("context") or ""),
                    reason=str(self.params.get("reason") or ""),
                    scope=str(self.params.get("scope") or "user").strip() or "user",
                    tags=self._tags(),
                    session_id=self._session_id(),
                    **self._provenance(),
                )
            )
        if action == "update":
            return self._wrap(
                await self._facade.update(
                    str(self.params.get("id") or ""),
                    str(self.params.get("content") or ""),
                )
            )
        if action == "remove":
            return self._wrap(
                await self._facade.remove(
                    str(self.params.get("id") or ""),
                    origin="agent",
                )
            )
        if action == "archive":
            return self._wrap(await self._facade.archive(str(self.params.get("id") or "")))
        if action == "unarchive":
            return self._wrap(await self._facade.unarchive(str(self.params.get("id") or "")))
        if action == "digest_write":
            day = str(self.params.get("date") or "").strip()
            content = str(self.params.get("content") or "").strip()
            if not day:
                return ToolResult.error("digest_write 需要 date（YYYY-MM-DD）")
            if not content:
                return ToolResult.error("digest_write 需要 content")
            return self._wrap(await self._facade.write_digest(day, content))
        if action == "source":
            return ToolResult.error("source 已迁到 local_search(action=source, id=…)")
        return ToolResult.error(f"未知 action: {action}")

    @staticmethod
    def _wrap(result: Any) -> ToolResult:
        if result.is_error:
            return ToolResult.error(result.message)
        return ToolResult.success(result.message, metadata=result.metadata or {})
