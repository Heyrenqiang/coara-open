"""Todo tool — session self-driven checklist (full-list update, Codex-style)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.todos.changes import detect_todo_changes
from src.todos.display import format_todo_markdown, format_todo_write_model_content
from src.todos.registry import get_todo_store, get_todo_store_lock
from src.todos.store import TodoStoreError
from src.todos.types import TodoItem, TodoStatus

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.todos.store import TodoStore


def _session_todo_store(coara: CoaraBase) -> TodoStore:
    return get_todo_store(workspace_dir=coara.workspace_dir, session_id=coara.session_id)


def _normalize_todo_id(raw: str) -> str:
    return raw.strip()[:40]


def _auto_id(existing: set[str]) -> str:
    max_n = 0
    for tid in existing:
        if tid.startswith("t") and tid[1:].isdigit():
            max_n = max(max_n, int(tid[1:]))
    n = max_n + 1
    candidate = f"t{n}"
    while candidate in existing:
        n += 1
        candidate = f"t{n}"
    return candidate


def _build_replace_items(
    raws: list[Any],
    previous: list[TodoItem],
) -> list[TodoItem]:
    """Validate full-list payload and build TodoItems (stable ids when possible)."""
    if not isinstance(raws, list):
        raise ValueError("todos 须为数组（整表覆盖；空数组表示清空）")

    prev_by_id = {todo.id: todo for todo in previous}
    # content → id：同文案续写时尽量保留原 id，便于变更检测
    prev_by_content: dict[str, str] = {}
    for todo in previous:
        key = todo.content.strip()
        if key and key not in prev_by_content:
            prev_by_content[key] = todo.id

    used_ids: set[str] = set()
    items: list[TodoItem] = []

    for raw in raws:
        if not isinstance(raw, dict):
            raise ValueError("每条待办必须是对象")
        content = str(raw.get("content") or "").strip()
        if not content:
            raise ValueError("每条待办必须有 content（一步一句）")

        status_raw = str(raw.get("status") or TodoStatus.PENDING.value).strip() or TodoStatus.PENDING.value
        try:
            status = TodoStatus(status_raw)
        except ValueError as exc:
            raise ValueError(f"非法 status：{status_raw}（pending / in_progress / completed / failed）") from exc

        explicit_id = _normalize_todo_id(str(raw.get("id") or ""))
        if explicit_id:
            todo_id = explicit_id
        elif content in prev_by_content and prev_by_content[content] not in used_ids:
            todo_id = prev_by_content[content]
        else:
            todo_id = _auto_id(used_ids | set(prev_by_id))

        if todo_id in used_ids:
            raise ValueError(f"待办 id 重复：{todo_id}")
        used_ids.add(todo_id)

        prev = prev_by_id.get(todo_id)
        priority = str(raw.get("priority") or (prev.priority if prev else "medium") or "medium")
        notes = str(raw.get("notes") if raw.get("notes") is not None else (prev.notes if prev else ""))

        items.append(
            TodoItem(
                id=todo_id,
                content=content,
                status=status,
                priority=priority,
                notes=notes,
            )
        )

    return items


_TODO_DESCRIPTION = """管理当前会话，驱动自己推进任务的待办工具

在遇到下面这些情况时，可以使用
- 碰到多步、有依赖、需要规划好、记下来
- 执行任务过程用户中又新加了任务、需求、待办等，需要记下来防止遗忘
- 任务、待办等已经完成，需要更新状态时
- 任务收尾时

怎么更新（action=update）
- 每次提交完整 todos 数组（整表覆盖）；空数组 = 清空
- 每步 content 宜短，约五至七个词/十来个汉字，带 status：pending / in_progress / completed / failed
- 可有多项 in_progress，并行任务如实标注；某项做完即标 completed
- 中途改计划：直接提交新全表，并用 explanation 说明为何改
- 可选 notes（卡点/结果）、priority（仅影响展示排序）

主动收尾（action=park）
- 剩余待办暂时无法推进时一步收尾，需要依赖后台任务运行、等待外部条件、需要用户定夺
- 调用即结束本轮，不再进行下一轮。要向用户交代的话全部写在 message 参数里，它会被直接交付给用户
- message 就是本轮对用户说的那句话：本轮正文里不要再把同样的话说一遍。要么不写正文，
  要么只写与它不同的补充，例如尚未交代的细节。同文重复会被投递两次，界面上叠成两个气泡
- 可同时带 todos 整表更新（已出结果的项标 completed/failed），一次调用全部了结
- 待办保持未完成、不伪装完结；后台任务完成或用户再发话时自然接续
- description 写明卡点原因

注意事项
- 简单一问一答、单步就能做完的事，不要调用本工具
- 每次必填 description，一句话说明本次操作
- 工具回执里已有清单正文，不要在对话里再复述整表"""


class TodoReadToolInvocation(ToolInvocation):
    """Read current todos for this session."""

    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara
        if parent_coara is None:
            raise ValueError("todo_read requires a bound Coara context")
        self.description = str(params.get("description") or "").strip()

    def get_description(self) -> str:
        return f"TodoRead: {self.description}" if self.description else "TodoRead"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            raise RuntimeError("TodoTool not bound to parent coara")

        lock = get_todo_store_lock(self._parent.workspace_dir, self._parent.session_id)
        async with lock:
            serialized = [todo.to_dict() for todo in _session_todo_store(self._parent).get_all()]
        summary = "已读取当前待办清单" if serialized else "当前没有待办清单"
        metadata = {"action": "read", "todos": serialized, "description": self.description}
        return ToolResult.success(
            content=format_todo_markdown(summary, serialized) if serialized else summary,
            metadata=metadata,
        )


class TodoUpdateInvocation(ToolInvocation):
    """Replace the entire session todo list."""

    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara
        if parent_coara is None:
            raise ValueError("todo update requires a bound Coara context")
        self.description = str(params.get("description") or "").strip()
        self.explanation = str(params.get("explanation") or "").strip()
        self.summary = str(params.get("summary") or "").strip()

    def get_description(self) -> str:
        return f"Todo update: {self.description}" if self.description else "Todo update"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            raise RuntimeError("TodoTool not bound to parent coara")

        lock = get_todo_store_lock(self._parent.workspace_dir, self._parent.session_id)
        async with lock:
            try:
                store = _session_todo_store(self._parent)
                previous = store.get_all()
                previous_dicts = [todo.to_dict() for todo in previous]
                raws = self.params.get("todos")
                if raws is None:
                    raise ValueError("action=update 需要 todos 数组（整表；空数组表示清空）")
                items = _build_replace_items(raws, previous)
                saved = store.replace_all(items)
                serialized = [todo.to_dict() for todo in saved]
            except (TodoStoreError, ValueError) as exc:
                return ToolResult.error(str(exc))

        changes = detect_todo_changes(previous_dicts, serialized)
        summary_parts = [p for p in (self.summary, self.explanation) if p]
        summary = "；".join(summary_parts)

        metadata: dict[str, Any] = {
            "action": "update",
            "todos": serialized,
            "description": self.description,
            "changes": {
                "created": changes.created,
                "completed": changes.completed,
                "updated": changes.updated,
                "failed": changes.failed,
            },
        }
        if self.explanation:
            metadata["explanation"] = self.explanation

        return ToolResult.success(
            content=format_todo_write_model_content(
                summary=summary,
                todos=serialized,
                changes=changes,
                previous=previous_dicts,
            ),
            metadata=metadata,
        )


class TodoParkInvocation(ToolInvocation):
    """One-shot turn close: optional full-list update + closing message, turn ends immediately."""

    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara
        if parent_coara is None:
            raise ValueError("todo park requires a bound Coara context")
        self.description = str(params.get("description") or "").strip()
        self.message = str(params.get("message") or "").strip()

    def get_description(self) -> str:
        return f"Todo park: {self.description}" if self.description else "Todo park"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            raise RuntimeError("TodoTool not bound to parent coara")

        lock = get_todo_store_lock(self._parent.workspace_dir, self._parent.session_id)
        async with lock:
            try:
                store = _session_todo_store(self._parent)
                raws = self.params.get("todos")
                if raws is not None:
                    items = _build_replace_items(raws, store.get_all())
                    store.replace_all(items)
            except (TodoStoreError, ValueError) as exc:
                return ToolResult.error(str(exc))

        # 一步收尾：结束语挂上实例，编排器在本工具批次后立即结束回合并交付。
        # open_count 以调用方提交的整表为准（store 的终态清理只影响持久化显示）。
        if raws is not None:
            open_count = sum(
                1
                for raw in raws
                if isinstance(raw, dict) and str(raw.get("status") or "pending").strip() in ("pending", "in_progress")
            )
        else:
            open_items = [
                todo for todo in store.get_all() if todo.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
            ]
            open_count = len(open_items)
        self._parent._todo_park_message = self.message
        content = (
            f"本轮已结束：结束语已交付用户，{open_count} 项未完成待办保留原状（{self.description}）。"
            "后台任务完成或用户继续发话时会自然接续，届时再推进。"
        )
        metadata: dict[str, Any] = {
            "action": "park",
            "description": self.description,
            "open_count": open_count,
        }
        return ToolResult.success(content=content, metadata=metadata)


class TodoTool(BaseTool):
    """Read or fully replace the current session todo list."""

    name = "todo"
    description = _TODO_DESCRIPTION
    display_name = "Todo"
    category = "planning"
    kind = ToolKind.EDIT
    owner_only = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "update", "park"],
                "description": "read=读取清单；update=整表覆盖（空数组清空）；park=一步结束本轮（结束语写 message）",
            },
            "todos": {
                "type": "array",
                "description": "update 时必填、park 时可选：完整步骤列表（整表覆盖）",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "步骤正文（短句，必填）",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "failed"],
                            "description": "默认 pending；in_progress 表示进行中，可多项并行",
                        },
                        "id": {
                            "type": "string",
                            "description": "可选；省略时按同文案续用旧 id 或自动编号",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "可选，仅影响展示排序，默认 medium",
                        },
                        "notes": {"type": "string", "description": "可选：结果或卡点"},
                    },
                    "required": ["content"],
                },
            },
            "explanation": {
                "type": "string",
                "description": "可选：改计划时的理由，中途推翻步骤时建议填写",
            },
            "message": {
                "type": "string",
                "description": "park 时必填：向用户交代的结束语，调用即结束本轮，直接交付用户。"
                "它就是本轮对用户说的话——正文不要再复述同一段，否则会投递两次、叠成两个气泡",
            },
            "summary": {"type": "string", "description": "可选进度摘要（写入工具回执开头）"},
            "description": {
                "type": "string",
                "description": "必填：本次操作一句话说明（CLI ✓ 行展示）",
            },
        },
        "required": ["action", "description"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None):
        super().__init__()
        self._parent = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        if not str(params.get("description") or "").strip():
            raise ValueError("todo 调用缺少必填参数：description（一句话说明本次操作，显示在 CLI 端）")
        action = str(params.get("action") or "").strip()
        if action == "read":
            return TodoReadToolInvocation(params, self._parent)
        if action == "update":
            return TodoUpdateInvocation(params, self._parent)
        if action == "park":
            if not str(params.get("message") or "").strip():
                raise ValueError('todo(action="park") 缺少必填参数：message（向用户交代的结束语，调用即结束本轮）')
            return TodoParkInvocation(params, self._parent)
        raise ValueError(f"未知 todo action: {action}（支持 read / update / park）")
