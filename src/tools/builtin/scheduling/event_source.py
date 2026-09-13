"""Root tool: manage event-source definitions for user workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.event_sources.ops import (
    EventSourceOpsError,
    delete_definition,
    ensure_workspace_registered,
    list_definitions,
    read_definition,
    reload_manager,
    set_enabled,
    write_definition,
)
from src.event_sources.types import SALIENCE_LEVELS, EventSourceKind, HandleMode

if TYPE_CHECKING:
    from src.coara.base import CoaraBase

EVENT_SOURCE_TOOL_TYPE: type[BaseTool]


class EventSourceTool(BaseTool):
    name = "event_source"
    summary = "为已登记工作空间配置外部事件源，写 YAML 并热重载"
    display_name = "Event Source"
    description = """为**用户已登记的工作空间**配置外部事件源，写 YAML 并热重载

Action
- `list`：列出已配置事件源
- `add`：新建（需 `id` `kind`；`workspace` 默认真前台；file_watch/poll 还需 `watch_path`）
- `update`：按 id 覆盖更新字段
- `toggle`：开/关，需 `enabled`
- `remove`：删除定义
- `reload`：仅热重载，add/update/remove/toggle 已自动 reload

`kind`：`file_watch` / `interval_poll` / `webhook` / `cron`，定时钟，需 `cron` 表达式
`salience`：low / normal / high，默认 normal；high 进前台待处理视图
`ttl_seconds`：消息保质期，超时未读自动勾掉，低价值事件建议配
`handle`：`park`，默认内容挂住等用户 / `janitor`，入库后由用户经 /ws updates 处理；不再派 LLM review

注意
- 查看 开关 删除 重载**直接调用本工具** 无需加载技能
- 仅需**新建或改设计**（kind / 路径 / 模板 / 分流）时先激活 `event-source` 技能读 EVENT_SOURCE_SPEC 后 `add`/`update`
- 这是用户本机事件源，与端用户 `/report`（软件内置开发者通道）无关"""
    kind = ToolKind.OTHER
    category = "scheduling"
    owner_only = True
    should_defer = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "toggle", "remove", "reload"],
                "description": "list / add / update / toggle / remove / reload",
            },
            "id": {"type": "string", "description": "事件源 id，add/update/toggle/remove 必填"},
            "kind": {
                "type": "string",
                "enum": ["file_watch", "interval_poll", "webhook", "cron"],
                "description": "事件源类型，add 必填",
            },
            "workspace": {
                "type": "string",
                "description": "归属工作空间名，add 默认当前前台空间",
            },
            "enabled": {"type": "boolean", "description": "是否启用，toggle 时必填；add/update 可选"},
            "watch_path": {"type": "string", "description": "相对工作空间根的监听目录（file_watch/poll）"},
            "watch_pattern": {"type": "string", "description": "文件名过滤，默认 *"},
            "watch_events": {
                "type": "array",
                "items": {"type": "string"},
                "description": "created / modified（file_watch）",
            },
            "interval_seconds": {"type": "number", "description": "poll 间隔秒数"},
            "poll_min_count": {"type": "integer", "description": "poll 最少新文件数"},
            "cron": {"type": "string", "description": "5 字段 cron 表达式，kind=cron 必填，Asia/Shanghai"},
            "webhook_secret": {"type": "string", "description": "webhook 鉴权令牌"},
            "salience": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": "显著性，默认 normal；high 上浮前台待处理视图",
            },
            "handle": {
                "type": "string",
                "enum": ["park", "janitor"],
                "description": "处理模式，默认 park，挂住等用户；janitor 叫醒管家过目处置",
            },
            "cooldown_seconds": {"type": "number", "description": "去重冷却秒数"},
            "ttl_seconds": {
                "type": "number",
                "description": "消息保质期秒数，落箱后超时未读将被纯规则过目自动勾掉，留轨迹",
            },
            "routing_domain": {"type": "string"},
            "suggested_delegate": {"type": "string"},
            "message_template": {"type": "string"},
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase):
        super().__init__()
        self._parent = parent_coara

    @staticmethod
    def requires_approval(args: dict[str, Any] | None = None) -> bool:
        action = str((args or {}).get("action") or "").strip().lower()
        return action in {"add", "update", "toggle", "remove"}

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _EventSourceInvocation(params, self)


class _EventSourceInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], tool: EventSourceTool):
        super().__init__(params)
        self._tool = tool

    def get_description(self) -> str:
        action = str(self.params.get("action") or "")
        eid = str(self.params.get("id") or "")
        if eid:
            return f"event_source {action} {eid}"
        return f"event_source {action}"

    def _coara_home(self) -> Any:
        root = self._tool._parent
        wm = getattr(root, "workspace_manager", None)
        if wm is None:
            raise EventSourceOpsError("工作空间未启用（无 coara_home）")
        return wm.coara_home, wm, root

    def _es_manager(self, root: Any) -> Any:
        mgr = getattr(root, "event_source_manager", None)
        if mgr is None:
            raise EventSourceOpsError("事件源管理器未启动")
        return mgr

    def _foreground_workspace(self, root: Any) -> str:
        if hasattr(root, "foreground_active_name"):
            try:
                return str(root.foreground_active_name() or "")
            except Exception:
                return ""
        return ""

    async def execute(self, signal: Any = None) -> ToolResult:
        action = str(self.params.get("action") or "").strip().lower()
        try:
            coara_home, wm, root = self._coara_home()
            if action == "list":
                rows = list_definitions(coara_home, registry=wm.registry)
                if not rows:
                    return ToolResult.success("暂无事件源")
                lines = [
                    f"- {d.id}  [{d.kind.value}]  @{d.workspace}  "
                    f"{'开' if d.enabled else '关'}  salience={d.salience}  handle={d.handle.value}"
                    for d in rows
                ]
                return ToolResult.success("事件源：\n" + "\n".join(lines))

            if action == "reload":
                await reload_manager(self._es_manager(root))
                return ToolResult.success("事件源已重新加载")

            if action == "remove":
                eid = str(self.params.get("id") or "").strip()
                delete_definition(coara_home, eid, registry=wm.registry)
                await reload_manager(self._es_manager(root))
                return ToolResult.success(f"已删除事件源 {eid}")

            if action == "toggle":
                eid = str(self.params.get("id") or "").strip()
                if "enabled" not in self.params:
                    return ToolResult.error("toggle 需要 enabled=true|false")
                defn = set_enabled(coara_home, eid, enabled=bool(self.params.get("enabled")), registry=wm.registry)
                await reload_manager(self._es_manager(root))
                return ToolResult.success(f"事件源 {defn.id} 已{'开启' if defn.enabled else '关闭'}")

            if action in {"add", "update"}:
                return await self._upsert(coara_home, wm, root, overwrite=(action == "update"))

            return ToolResult.error(f"未知 action：{action}")
        except EventSourceOpsError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:
            return ToolResult.error(f"事件源操作失败：{exc}")

    async def _upsert(self, coara_home: Any, wm: Any, root: Any, *, overwrite: bool) -> ToolResult:
        eid = str(self.params.get("id") or "").strip()
        if not eid:
            return ToolResult.error("需要 id")

        if overwrite:
            base = read_definition(coara_home, eid, registry=wm.registry).model_dump(mode="json")
        else:
            kind = str(self.params.get("kind") or "").strip()
            if not kind:
                return ToolResult.error("add 需要 kind")
            workspace = str(self.params.get("workspace") or "").strip() or self._foreground_workspace(root)
            ensure_workspace_registered(wm, workspace)
            base = {
                "id": eid,
                "kind": kind,
                "workspace": workspace,
                "enabled": True,
            }

        # Overlay provided fields.
        for key in (
            "kind",
            "workspace",
            "enabled",
            "watch_path",
            "watch_pattern",
            "watch_events",
            "interval_seconds",
            "poll_min_count",
            "cron",
            "webhook_secret",
            "salience",
            "handle",
            "cooldown_seconds",
            "ttl_seconds",
            "routing_domain",
            "suggested_delegate",
            "message_template",
        ):
            if key in self.params and self.params.get(key) is not None:
                base[key] = self.params[key]
        base["id"] = eid

        ensure_workspace_registered(wm, str(base.get("workspace") or ""))
        # Validate enums early for clearer errors.
        try:
            EventSourceKind(str(base.get("kind")))
            if base.get("handle"):
                HandleMode(str(base.get("handle")))
            if base.get("salience") and str(base.get("salience")) not in SALIENCE_LEVELS:
                raise ValueError(f"salience 必须是 {'/'.join(SALIENCE_LEVELS)}")
        except Exception as exc:
            return ToolResult.error(f"参数无效：{exc}")

        kind = str(base.get("kind"))
        if kind in {"file_watch", "interval_poll"} and not str(base.get("watch_path") or "").strip():
            return ToolResult.error(f"{kind} 需要 watch_path")
        if kind == "cron" and not str(base.get("cron") or "").strip():
            return ToolResult.error("cron 事件源需要 cron 表达式（5 字段，Asia/Shanghai）")

        defn = write_definition(coara_home, base, overwrite=overwrite, registry=wm.registry)
        await reload_manager(self._es_manager(root))
        verb = "已更新" if overwrite else "已创建"
        return ToolResult.success(
            f"{verb}事件源 {defn.id}（{defn.kind.value} → @{defn.workspace}，"
            f"salience={defn.salience}，handle={defn.handle.value}）"
        )


EVENT_SOURCE_TOOL_TYPE = EventSourceTool
