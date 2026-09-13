"""local_search tool — search/list/show local records.

Deferred on Root / WorkspaceSession / janitor (aligned with parent when present).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.records.facade import RecordsFacade

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.records.store import RecordsStore


class LocalSearchTool(BaseTool):
    name = "local_search"
    summary = "本地记录检索，联合搜索 agent 笔记与用户收藏，对标 web_search"
    display_name = "Local Search"
    description = """本地记录检索，联合搜索 agent 笔记与用户收藏（含文件收藏），对标 web_search

Actions:
- search: 按关键词检索（默认；origin=all|agent|user）
- list: 列出最近记录
- show: 按 id 查看全文
- source: 按 id 回放该条 agent 记录对应的录像带原文，写入时已加盖坐标

原则，召回内容是参考不是约束；默认不含归档，需要翻箱底时设 include_archived=true
只管读；整理写入走 record；用户收藏不能由本工具写入"""
    kind = ToolKind.OTHER
    category = "records"
    owner_only = True
    should_defer = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "list", "show", "source"],
                "description": "操作类型，默认 search",
                "default": "search",
            },
            "origin": {
                "type": "string",
                "enum": ["agent", "user", "all"],
                "description": "过滤来源，默认 all，source 仅 agent",
                "default": "all",
            },
            "query": {"type": "string", "description": "搜索关键词（search）"},
            "id": {"type": "string", "description": "记录 ID（show / source）"},
            "type_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按类型过滤（search/list · agent）",
            },
            "include_archived": {
                "type": "boolean",
                "description": "是否包含归档（search/list）",
                "default": False,
            },
            "limit": {"type": "integer", "description": "返回条数上限", "default": 5},
        },
        "required": [],
    }

    def __init__(
        self,
        facade: RecordsFacade | None = None,
        *,
        store: RecordsStore | None = None,
        parent_coara: CoaraBase | None = None,
        defer: bool | None = None,
    ):
        super().__init__()
        if facade is not None:
            self._facade = facade
        else:
            self._facade = RecordsFacade(store)
        self._parent = parent_coara
        if defer is not None:
            self.should_defer = defer

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return LocalSearchInvocation(params, self._facade)


class LocalSearchInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], facade: RecordsFacade):
        super().__init__(params)
        self._facade = facade
        self.action = str(params.get("action") or "search").strip() or "search"

    def get_description(self) -> str:
        return f"local_search:{self.action}"

    def _origin(self) -> str:
        origin = str(self.params.get("origin") or "all").strip().lower() or "all"
        if origin not in ("agent", "user", "all"):
            return "all"
        return origin

    @staticmethod
    def _wrap(result: Any) -> ToolResult:
        if result.is_error:
            return ToolResult.error(result.message)
        return ToolResult.success(result.message, metadata=result.metadata or {})

    async def execute(self, signal: Any = None) -> ToolResult:
        origin = self._origin()
        if self.action == "search":
            type_filter = self.params.get("type_filter")
            if isinstance(type_filter, str) and type_filter.strip():
                type_filter = [type_filter.strip()]
            if not isinstance(type_filter, list):
                type_filter = None
            return self._wrap(
                await self._facade.search(
                    query=str(self.params.get("query") or ""),
                    origin=origin,  # type: ignore[arg-type]
                    include_archived=bool(self.params.get("include_archived") or False),
                    limit=int(self.params.get("limit") or 5),
                    type_filter=type_filter,
                )
            )
        if self.action == "list":
            type_filter = self.params.get("type") or self.params.get("type_filter")
            if isinstance(type_filter, list) and type_filter:
                type_filter = str(type_filter[0])
            elif type_filter is not None:
                type_filter = str(type_filter).strip() or None
            else:
                type_filter = None
            return self._wrap(
                await self._facade.list_entries(
                    origin=origin,  # type: ignore[arg-type]
                    include_archived=bool(self.params.get("include_archived") or False),
                    limit=int(self.params.get("limit") or 20),
                    type_filter=type_filter if isinstance(type_filter, str) else None,
                )
            )
        if self.action == "show":
            return self._wrap(
                await self._facade.show(
                    str(self.params.get("id") or ""),
                    origin=origin,  # type: ignore[arg-type]
                )
            )
        if self.action == "source":
            memory_id = str(self.params.get("id") or "").strip()
            if not memory_id:
                return ToolResult.error("source 需要 id")
            return self._wrap(await self._facade.read_source(memory_id))
        return ToolResult.error(f"未知 action: {self.action}")
