"""Unified reminder tool — scheduled in the coara main process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.reminders.service import ReminderService

class ReminderTool(BaseTool):
    name = "reminder"
    summary = "管理定时提醒，coara 主进程内调度，到点入工作空间收件箱"
    display_name = "Reminder"
    description = """管理定时提醒，coara 主进程内调度，持久化在 coara home `reminders/store.json`，到点落入工作空间动态收件箱，high 显著挂前台待处理，等用户查看

与**cron 事件源**（写 YAML 配置，到点发 `cron.tick` 事件落工作空间动态）不同，本工具只服务**口头提醒用户**，不绑定工作空间

适用
- 用户说 X 分钟/小时/天后提醒我… 待会提醒我做…
- 用户要求 每 X 分钟/小时/天提醒一次…，周期性 nag
- 用户要求 每天早上 8 点 每周一 9 点… 等**个人**日历规则，不绑定工作空间
- 查看或取消已有提醒；创建后用户要求验证是否设上

示例 5 分钟后叫我 每天早上 8 点提醒我开会，不绑定工作空间、只要 Root 说一声

不适用
- 只是聊天提到未来计划、**没有**要求设提醒
- 需要 coara 离线也能触达（本工具依赖进程在线；关闭后重启，一次性/间隔任务会补触发已过期的，cron 任务直接排下一次）

注意事项
用户提出**定时 / 提醒 / 闹钟 / 待会叫我**类需求且**不涉及工作空间** 时，**必须**调用本工具，**禁止**只口头答应——不调用则不会定时

| action | 何时用 |
|--------|--------|
| `add_once` | 只响一次；简单延迟（如 5 分钟后） |
| `add_interval` | 固定间隔重复（如每隔 30 分钟） |
| `add_cron` | Cron 表达式（如每周一 8 点） |
| `list` | 查看已设提醒、查 `job_id` |
| `remove` | 按 `job_id` 取消 |

- `add_once` / `add_interval`：`message` 必填；`minutes` / `hours` / `days` 至少一个 > 0
- `add_cron`：五字段 cron；`message` 必填；时区默认 `Asia/Shanghai`
- 创建后向用户确认触发时间；取消时先 `list` 查 `job_id`，若用户未提供
- 到点提醒不自动跑回合，作为高显著内容落在当前活跃工作空间的动态里，用户查看后自行吩咐后续动作"""
    kind = ToolKind.OTHER
    category = "scheduling"
    owner_only = True
    should_defer = True  # 低频工具：schema 不常驻 prompt，经 tool(action=activate) 按需装载
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add_once", "add_interval", "add_cron", "list", "remove"],
                "description": (
                    "add_once=一次性延迟; add_interval=固定间隔重复; add_cron=Cron 表达式; "
                    "list=列出; remove=按 job_id 取消"
                ),
            },
            "message": {"type": "string", "description": "提醒内容，add_* 时必填"},
            "minutes": {"type": "integer", "description": "分钟（add_once/add_interval）", "default": 0},
            "hours": {"type": "integer", "description": "小时（add_once/add_interval）", "default": 0},
            "days": {"type": "integer", "description": "天（add_once/add_interval）", "default": 0},
            "cron": {
                "type": "string",
                "description": 'Cron 表达式，格式 分 时 日 月 星期，如 "0 8 * * 1"，add_cron 时必填',
            },
            "job_id": {"type": "string", "description": "提醒任务 ID，remove 时必填"},
        },
        "required": ["action"],
    }

    def __init__(self, reminder_service: ReminderService | None = None):
        super().__init__()
        self._service = reminder_service

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return ReminderInvocation(params, self._service)


class ReminderInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], service: ReminderService | None):
        super().__init__(params)
        self._service = service
        self.action = str(params.get("action") or "").strip()
        self.message = str(params.get("message") or "").strip()
        self.minutes = int(params.get("minutes") or 0)
        self.hours = int(params.get("hours") or 0)
        self.days = int(params.get("days") or 0)
        self.cron = str(params.get("cron") or "")
        self.job_id = str(params.get("job_id") or "")

    def get_description(self) -> str:
        if self.action == "list":
            return "List reminders"
        if self.action == "remove":
            return f"Remove reminder {self.job_id}"
        return f"Reminder {self.action}: {self.message[:60]}"

    async def execute(self, signal=None) -> ToolResult:
        if self._service is None:
            return ToolResult.error("ReminderService is not available")

        try:
            if self.action in ("add_once", "add_interval", "add_cron") and not self.message:
                return ToolResult.error("add_* 需要 message 参数（提醒内容）")
            if self.action == "add_once":
                text = await self._service.add_one_time_reminder(
                    self.message,
                    minutes=self.minutes,
                    hours=self.hours,
                    days=self.days,
                )
            elif self.action == "add_interval":
                text = await self._service.add_interval_reminder(
                    self.message,
                    minutes=self.minutes,
                    hours=self.hours,
                    days=self.days,
                )
            elif self.action == "add_cron":
                if not self.cron:
                    return ToolResult.error("add_cron requires cron expression")
                text = await self._service.add_cron_reminder(self.cron, self.message)
            elif self.action == "list":
                rows = await self._service.list_reminders()
                if not rows:
                    return ToolResult.success("当前没有定时提醒。")
                lines = [f"- {row['id']} [{row['kind']}] next={row['next_run_at']} — {row['message']}" for row in rows]
                return ToolResult.success("\n".join(lines), metadata={"reminders": rows})
            elif self.action == "remove":
                if not self.job_id:
                    return ToolResult.error("remove requires job_id")
                text = await self._service.remove_reminder(self.job_id)
            else:
                return ToolResult.error(
                    f"Unknown action: {self.action}. Use add_once, add_interval, add_cron, list, or remove."
                )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        else:
            return ToolResult.success(text)


REMINDER_TOOL_TYPE = ReminderTool
