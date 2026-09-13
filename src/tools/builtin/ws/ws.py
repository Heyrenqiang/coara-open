"""Root ws tool — minimal workspace directory registry: add, remove, rename, list, switch."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


def resolve_default_workspace_path(coara_home: Path, name: str) -> Path:
    return (coara_home / "workspaces" / name).resolve()


def _retract_switch_ui_transcript(source_coara: Any) -> None:
    """Drop the switch-trigger turn from the source workspace UI transcript.

    ``strip_ws_switch_tail`` 已清掉内存 message_history 的切换尾部；这里立刻
    对账一次 L1 会话事件带——sync_history 检出前缀分叉会写 history/shadow，
    投影（Web hydrate / dashboard）随即不再产出被截掉的尾部。
    """
    turn = getattr(source_coara, "_active_turn", None)
    turn_id = str(getattr(turn, "turn_id", "") or "")
    session_id = str(getattr(source_coara, "session_id", "") or "")
    # 端路由门禁要求端作用域事件必带来源：取触发回合的活跃来源（web/cli/…），
    # 无来源事件会被 _end_source_allows 严格模式吞掉，端永远收不到回撤指令。
    source = str(getattr(source_coara, "_active_turn_source", "") or "")
    if not source:
        # 回退：turn_context 的 EndChannel 与本回合同栈（工具执行期间
        # _active_turn_source 已置空时，ContextVar 仍持有真实来源）。
        try:
            from src.coara.turn_context import get_end_channel

            channel = get_end_channel()
            source = str(getattr(channel, "source", "") or "")
        except Exception:  # noqa: BLE001 — 取不到就留空，门禁按既有契约处理
            source = ""

    recorder = getattr(source_coara, "_session_log", None)
    if recorder is not None and hasattr(source_coara, "message_history"):
        try:
            recorder.sync_history(list(source_coara.message_history))
        except Exception:
            logger.exception("Failed to shadow UI transcript after workspace switch")

    emit = getattr(source_coara, "_emit_trace", None)
    if callable(emit):
        emit(
            "chat_turn_retracted",
            "Workspace switch: retract switch-trigger turn from UI",
            payload={
                "turn_id": turn_id,
                "session_id": session_id,
                "reason": "workspace_switch",
                "source": source,
            },
        )


class WsTool(BaseTool):
    name = "ws"
    summary = "已登记工作空间的 list / add / remove / rename / switch / kind"
    display_name = "Workspace"
    description = """管理已登记工作空间（coara Home 注册表），查询、登记、移出、改名、切换、设置性质

Actions
- `list`：只查询，标出（当前）；用户问「在哪个工作空间」时 list 后直接回答，**不要**自行切换
- `add`：登记；`name` 必填；`path` 省略则 `{coara_home}/workspaces/{name}`；目录不存在默认创建（`create_path`）
- `remove`：取消登记；**须先问用户**是否删磁盘，确认才传 `delete_disk=true`，默认只取消登记；
  不能移当前 active，也不能移回合中的空间
- `rename`：只改登记名（`name`→`new_name`），路径与磁盘不动；勿用 remove+add 代替
- `switch`：**仅**用户明确要求切换/进入某工作空间时调用；勿因列表里看见其它名字而自行切换；
  会结束当前回合，切换后的工作等用户下一条消息再继续
- `kind`：查询或设置空间性质（`managed` / `reference` / `code`）；省略 `kind` 参数则查询

注意
- 切换必须用 `action=switch`，勿用 shell
- 工作空间动态（收件箱）的查看与处置用独立工具 `review`，janitor 专属，不在本工具
- 本工具为挂起工具，schema 不常驻 prompt，先 `tool(action="activate", name="ws")` 再调用"""
    # Workspace-scoped; must not use the global THINK tool cache (same params across workspaces).
    kind = ToolKind.OTHER
    category = "ws"
    owner_only = True
    should_defer = True  # 低频：schema 不常驻，经 tool(activate) 按需装载
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list",
                    "add",
                    "remove",
                    "rename",
                    "switch",
                    "kind",
                ],
                "description": (
                    "list=查询，不切换；switch=仅用户明确要求切换时；"
                    "add/remove/rename；kind=设置空间性质，如 code"
                ),
            },
            "name": {
                "type": "string",
                "description": "工作空间名；add/remove/rename/switch 必填",
            },
            "new_name": {
                "type": "string",
                "description": "rename 时的新名称",
            },
            "path": {
                "type": "string",
                "description": "add 时根路径；省略则 {coara_home}/workspaces/{name}",
            },
            "summary": {
                "type": "string",
                "description": "add 时可选摘要",
            },
            "kind": {
                "type": "string",
                "description": (
                    "kind 时设置的空间性质，managed=普通 / reference=只读引用 / "
                    "code=代码编程空间；省略时查询当前性质"
                ),
            },
            "create_path": {
                "type": "boolean",
                "description": "add 时目录不存在则创建，默认 true",
                "default": True,
            },
            "delete_disk": {
                "type": "boolean",
                "description": "remove 时删磁盘，默认 false",
                "default": False,
            },
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None):
        super().__init__()
        self._coara = parent_coara

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        """Prompt for destructive ws actions."""
        action = str(args.get("action", "") or "").strip() if isinstance(args, dict) else ""
        return action == "remove"

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return WsInvocation(params, self._coara)


class WsInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], coara: CoaraBase | None):
        super().__init__(params)
        self._coara = coara
        self.action = str(params.get("action") or "").strip()
        self.path_raw = str(params.get("path") or "").strip()
        self.workspace_name = str(params.get("name") or params.get("workspace") or "").strip()
        self.new_name = str(params.get("new_name") or "").strip()
        self.summary = str(params.get("summary") or "").strip()
        self.kind = str(params.get("kind") or "").strip().lower()
        self.create_path = bool(params.get("create_path", True))
        self.delete_disk = bool(params.get("delete_disk", False))

    def get_description(self) -> str:
        if self.action == "add":
            return f"Register workspace {self.workspace_name or '?'}"
        if self.action == "remove":
            mode = "并删除磁盘" if self.delete_disk else "仅取消登记"
            return f"Remove workspace {self.workspace_name or '?'}（{mode}）"
        if self.action == "rename":
            return f"Rename workspace {self.workspace_name or '?'} → {self.new_name or '?'}"
        if self.action == "kind":
            return f"Set workspace {self.workspace_name or '?'} kind={self.kind or '（查询）'}"
        if self.action == "switch":
            return f"Switch to workspace {self.workspace_name or '?'}"
        return "List registered workspaces"

    async def execute(self, signal=None) -> ToolResult:
        if self._coara is None:
            return ToolResult.error("ws requires root coara context")

        manager = getattr(self._coara, "workspace_manager", None)
        if manager is None:
            return ToolResult.error("Workspace manager is not available on this coara instance.")

        if self.action == "list":
            return await self._execute_list(manager)

        if self.action == "add":
            return await self._execute_add(manager)

        if self.action == "remove":
            return await self._execute_remove(manager)

        if self.action == "rename":
            return await self._execute_rename(manager)

        if self.action == "kind":
            return await self._execute_kind(manager)

        if self.action == "switch":
            return await self._execute_switch(manager)

        return ToolResult.error(f"Unknown ws action: {self.action}")

    def _invoking_session_coara(self) -> Any | None:
        """Locate the WorkspaceSession coara that owns this tool call.

        Prefer ``session_id`` injected by the executor; fall back to scanning
        ``_inside_turn`` only when unbound (tests / legacy).
        """
        root = self._coara
        sid = str(getattr(self, "session_id", "") or "").strip()
        sessions = getattr(root, "_sessions", None) or {}
        if sid:
            for session in sessions.values():
                coara = getattr(session, "coara", None)
                if coara is not None and str(getattr(coara, "session_id", "") or "") == sid:
                    return coara
        for session in sessions.values():
            coara = getattr(session, "coara", None)
            if coara is not None and bool(getattr(coara, "_inside_turn", False)):
                return coara
        return None

    def _invoking_active_name(self, manager: Any) -> str | None:
        """Active name for the invoking session's workspace（非全局 cli view）。"""
        from src.workspace.catalog import resolve_foreground_active_name

        source = self._invoking_session_coara()
        if source is not None:
            wd = Path(getattr(source, "workspace_dir", "") or "").expanduser()
            if str(wd):
                try:
                    resolved = wd.resolve()
                except OSError:
                    resolved = wd
                workspace_id = manager.match_path_to_workspace_id(resolved)
                if workspace_id is not None:
                    entry = manager.registry.get_by_id(workspace_id)
                    if entry is not None:
                        return entry.name
        return resolve_foreground_active_name(self._coara, manager)

    async def _execute_list(self, manager: Any) -> ToolResult:
        from src.workspace.catalog import format_workspace_catalog

        root = self._coara
        root.sync_workspace_manager_to_foreground()
        active_name = self._invoking_active_name(manager)
        return ToolResult.success(format_workspace_catalog(manager, active_name=active_name))

    async def _execute_add(self, manager: Any) -> ToolResult:
        if not self.workspace_name:
            return ToolResult.error("add 需要 name（工作空间名，如 my-workspace）")

        if self.path_raw:
            workspace_path = Path(self.path_raw).expanduser().resolve()
        else:
            workspace_path = resolve_default_workspace_path(manager.coara_home, self.workspace_name)

        if workspace_path.exists() and not workspace_path.is_dir():
            return ToolResult.error(f"路径不是目录: {workspace_path}")

        if not workspace_path.exists():
            if not self.create_path:
                return ToolResult.error(f"目录不存在: {workspace_path}（可设 create_path=true）")
            workspace_path.mkdir(parents=True, exist_ok=True)

        entry = manager.add_workspace(
            workspace_path,
            name=self.workspace_name,
            summary=self.summary or None,
        )

        return ToolResult.success(
            "\n".join(
                [
                    f"已登记工作空间 {entry.name}",
                    f"path: {entry.resolved_path()}",
                    f"summary: {entry.summary or '（无）'}",
                ]
            )
        )

    async def _execute_remove(self, manager: Any) -> ToolResult:
        if not self.workspace_name:
            return ToolResult.error("remove 需要 name")

        entry = manager.registry.resolve_name_or_id(self.workspace_name)
        if entry is None:
            return ToolResult.error(f"未找到工作空间 {self.workspace_name}")

        active_name = self._invoking_active_name(manager)
        if active_name == entry.name:
            return ToolResult.error(
                f"不能移除当前 active 工作空间 {entry.name}；"
                f'请先用 ws(action="switch", name="<其他工作空间>") 切换后再 remove'
            )

        # Guard: a cached session with an in-flight turn must not lose its
        # registry entry (and especially not its disk directory) mid-turn.
        root = self._coara
        cached = root._sessions.get(entry.id)
        cached_coara = getattr(cached, "coara", None) if cached is not None else None
        session_busy = bool(cached_coara is not None and cached_coara.is_turn_busy())
        if session_busy:
            return ToolResult.error(
                f"工作空间 {entry.name} 有正在进行的会话，不能移除；请先在该空间 /stop 或等其回合结束"
            )

        disk_path = entry.resolved_path()
        display_name = entry.name

        if self.delete_disk and cached is not None:
            return ToolResult.error(
                f"工作空间 {display_name} 仍有缓存会话，拒绝删除磁盘目录；"
                "可仅取消登记（delete_disk=false），或切换过去 /new 后再删"
            )

        if not manager.remove_workspace(entry.id):
            return ToolResult.error(f"未找到工作空间 {self.workspace_name}")

        if not self.delete_disk:
            return ToolResult.success(f"已取消登记 {display_name}（磁盘目录仍保留：{disk_path}）")

        import shutil

        if not disk_path.exists():
            return ToolResult.success(f"已取消登记 {display_name}；磁盘路径已不存在：{disk_path}")

        try:
            resolved = disk_path.resolve()
            home = Path(manager.coara_home).resolve()
        except OSError as exc:
            return ToolResult.error(f"已取消登记 {display_name}，但无法解析路径：{disk_path}（{exc}）")

        if resolved == home:
            return ToolResult.success(f"已取消登记 {display_name}，但拒绝删除 coara_home：{disk_path}")
        if not disk_path.is_dir():
            return ToolResult.success(f"已取消登记 {display_name}；路径不是目录，未删盘：{disk_path}")

        try:
            shutil.rmtree(disk_path)
        except OSError as exc:
            return ToolResult.error(f"已取消登记 {display_name}，但删除磁盘失败：{disk_path}（{exc}）")

        return ToolResult.success(f"已取消登记 {display_name}，并删除磁盘目录：{disk_path}")

    async def _execute_rename(self, manager: Any) -> ToolResult:
        if not self.workspace_name:
            return ToolResult.error("rename 需要 name（当前名称）")
        if not self.new_name:
            return ToolResult.error("rename 需要 new_name（新名称）")

        entry = manager.registry.resolve_name_or_id(self.workspace_name)
        if entry is None:
            return ToolResult.error(f"未找到工作空间 {self.workspace_name}")

        old_name = entry.name
        try:
            renamed = manager.rename_workspace(entry.id, self.new_name)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if renamed is None:
            return ToolResult.error(f"未找到工作空间 {self.workspace_name}")

        return ToolResult.success(
            "\n".join(
                [
                    f"已重命名工作空间：{old_name} → {renamed.name}",
                    f"path: {renamed.resolved_path()}（未改动）",
                ]
            )
        )

    async def _execute_kind(self, manager: Any) -> ToolResult:
        """查询或设置工作空间性质（kind）。用户手动设置的长期属性，janitor 不写。"""
        if not self.workspace_name:
            return ToolResult.error("kind 需要 name")

        entry = manager.registry.resolve_name_or_id(self.workspace_name)
        if entry is None:
            return ToolResult.error(f"未找到工作空间 {self.workspace_name}")

        from src.workspace.types import WorkspaceKind

        if not self.kind:
            return ToolResult.success(f"工作空间 {entry.name} 当前性质：{entry.kind.value}")
        try:
            new_kind = WorkspaceKind(self.kind)
        except ValueError:
            known = ", ".join(k.value for k in WorkspaceKind)
            return ToolResult.error(f"未知性质 '{self.kind}'，支持：{known}")

        if entry.kind == new_kind:
            return ToolResult.success(f"工作空间 {entry.name} 性质已是 {new_kind.value}，未改动")
        entry.kind = new_kind
        manager.registry.save()
        return ToolResult.success(
            f"已设置工作空间 {entry.name} 性质：{new_kind.value}\n"
            "（长期属性：janitor 据此维护「空间内目录文件」模块）"
        )

    async def _execute_switch(self, manager: Any) -> ToolResult:
        if not self.workspace_name:
            return ToolResult.error("switch 需要 name")

        root = self._coara
        # 源 session = 发起本工具调用的会话（executor 注入 session_id），非「扫第一个忙会话」。
        source_coara = self._invoking_session_coara()
        if source_coara is None:
            resolver = getattr(root, "resolve_view_coara", None)
            if callable(resolver):
                try:
                    source_coara = resolver("cli")
                except Exception:
                    source_coara = None
            if source_coara is None:
                source_coara = getattr(root, "foreground_coara", root)
        is_mid_turn = bool(getattr(source_coara, "_inside_turn", False))

        # 切换发起端的 view（matrix/web/cli），不拖其它端。
        # 优先用 executor 注入的 origin_source（与回合来源一致）。
        turn_source = str(
            getattr(self, "origin_source", None)
            or getattr(source_coara, "_active_turn_source", "")
            or "cli"
        )
        normalize = getattr(root, "normalize_view_end", None)
        try:
            end_key = normalize(turn_source) if callable(normalize) else "cli"
        except ValueError:
            end_key = "cli"
        set_view = getattr(root, "set_view_workspace", None)
        if callable(set_view):
            success = await set_view(end_key, self.workspace_name)
        else:
            success = await root.switch_workspace(self.workspace_name)

        if not success:
            return ToolResult.error(f"切换到 {self.workspace_name} 失败（未找到或不可用）")

        view_coara = None
        resolve = getattr(root, "resolve_view_coara", None)
        if callable(resolve):
            try:
                view_coara = resolve(end_key)
            except Exception:
                view_coara = None
        if view_coara is None:
            view_coara = getattr(root, "foreground_coara", None)
        active_path = getattr(view_coara, "workspace_dir", None) or manager.active_path
        session_renewed = bool(getattr(root, "last_switch_session_renewed", False)) if end_key == "cli" else False
        if is_mid_turn:
            from src.coara.turn_completion import CoaraRunCancelledError
            from src.coara.workspace_switch_history import strip_ws_switch_tail

            if hasattr(source_coara, "message_history"):
                strip_ws_switch_tail(source_coara.message_history)
            _retract_switch_ui_transcript(source_coara)
            cancel = CoaraRunCancelledError(f"switch_workspace:{self.workspace_name}")
            cancel.session_renewed = session_renewed  # type: ignore[attr-defined]
            cancel.last_active = getattr(root, "last_switch_last_active", None)  # type: ignore[attr-defined]
            raise cancel
        from src.coara.workspace_state import format_workspace_switch_message

        note = format_workspace_switch_message(
            self.workspace_name,
            session_renewed=session_renewed,
            last_active=getattr(root, "last_switch_last_active", None) if end_key == "cli" else None,
        )
        return ToolResult.success(
            f"{note}\npath: {active_path}",
            metadata={"control_only": True},
        )


WS_TOOL_TYPE = WsTool
