"""Plan Mode Tools — EnterPlanMode, SubmitPlan, and ExitPlanMode.

只读讨论 + 写&提交一体：submit 直接收计划全文，原子写入计划文件并展示给用户。
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


_HERO_NAMES: list[str] = [
    "iron-man",
    "spider-man",
    "captain-america",
    "thor",
    "hulk",
    "black-widow",
    "doctor-strange",
    "scarlet-witch",
    "ant-man",
    "black-panther",
    "captain-marvel",
    "vision",
    "falcon",
    "wolverine",
    "storm",
    "deadpool",
    "punisher",
    "daredevil",
    "batman",
    "superman",
    "wonder-woman",
    "flash",
    "aquaman",
    "green-lantern",
    "cyborg",
    "shazam",
    "martian-manhunter",
    "green-arrow",
    "nightwing",
    "batgirl",
    "supergirl",
    "zatanna",
]


def _generate_plan_slug() -> str:
    a = secrets.choice(_HERO_NAMES)
    b = secrets.choice(_HERO_NAMES)
    while b == a:
        b = secrets.choice(_HERO_NAMES)
    return f"{a}-{b}"


def _get_plan_file_path(workspace_dir: Path, *, max_attempts: int = 20) -> Path | None:
    """Pick a collision-free plan file path; None after ``max_attempts`` collisions."""
    plans_dir = workspace_dir / ".coara" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(max_attempts):
        path = plans_dir / f"{_generate_plan_slug()}.md"
        if not path.exists():
            return path
    return None


# ── EnterPlanMode ──────────────────────────────────────────────
class EnterPlanModeInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara

    def get_description(self) -> str:
        return "开启计划模式"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            return ToolResult.error("开启计划模式工具未绑定到 coara 实例。")

        if self._parent.is_plan_mode:
            return ToolResult.error(
                '已经在计划模式里了。讨论结束需要落地时再 plan_mode(action="submit") '
                '提交审批，或 plan_mode(action="exit") 退出。'
            )

        plan_path = _get_plan_file_path(self._parent.workspace_dir)
        if plan_path is None:
            return ToolResult.error("无法分配唯一的计划文件名（多次碰撞），请重试。")
        self._parent.enter_plan_mode(plan_path)

        from src.core.message_tags import system_reminder

        return ToolResult.success(
            content=system_reminder(
                f"计划模式已开启。计划文件路径（可选，不写也行）：{plan_path}\n"
                f"工具限制：shell/delete/workflow 等工具不可见；不能修改工作空间文件（计划文件除外）。"
            ),
            metadata={"plan_file": str(plan_path)},
        )


# ── 计划审阅展示 ───────────────────────────────────────────────
def _format_plan_review_summary(plan_content: str, plan_path: Path) -> str:
    """计划全文 + 外壳标题。

    标题用一级标题：三端都是 Markdown 渲染（web/手机大字号，CLI 走 rich 基础
    Markdown 的标题样式），与正文标题同级才不显得比正文还小。计划正文的标题
    从二级起（见 SubmitPlan 的参数说明），层次留给外壳。
    """
    body = plan_content.strip()
    return f"# 计划审阅\n\n{body}\n\n---\n\n计划文件：{plan_path}"


async def _publish_plan_review(plan_content: str, plan_path: Path, parent: Any = None) -> None:
    """把计划展示到对话流（端通道正文 chunk / remote send_text / local CliScrollback），不弹窗、不阻塞。

    只展示计划全文加一句结束语，不预设选项——用户在下一条消息里自由回复，
    LLM 结合刚展示的计划自然判断用户决定（批准/调整/放弃）。

    投递次序（与正文同一事实源，避免 attach 端收不到）：
    1. 有注册端通道（attach/web 回合）→ 走 EndRegistry chunk 通路，与助手正文
       同一路由正常上屏（旧 remote send_text 的 "info" 帧 attach 客户端不消费）。
    2. 无端通道的远端回合（matrix 等）→ remote send_text 兜底。
    3. 本地 CLI / 无界面 → CliScrollback 单通道写屏。
    """
    from src.coara.turn_context import get_turn_channel

    body = f"{_format_plan_review_summary(plan_content, plan_path)}\n\n请审阅：批准、要调整的地方，或放弃。"

    deliver = getattr(parent, "_deliver_plan_review_to_end", None)
    if callable(deliver):
        try:
            if await deliver(body):
                return
        except Exception:  # noqa: BLE001
            logger.warning("plan review end-channel delivery failed; falling back", exc_info=True)

    remote_channel = get_turn_channel()
    if remote_channel is not None:
        sent = await remote_channel.send_text(body)
        if sent is False:
            raise RuntimeError("计划内容未能发送到远端，请检查连接后重试。")
        return
    # CLI 端：走 CliScrollback 单通道（与流式块/spinner 同代理队列 FIFO）。
    # 裸 print 会被显示体系旁路——spinner 活跃时被覆盖或乱序，用户看不到。
    from src.cli.scrollback import CliScrollback

    CliScrollback.write("")
    CliScrollback.write("=" * 60)
    for line in body.split("\n"):
        CliScrollback.write(line)
    CliScrollback.write("=" * 60)
    CliScrollback.write("")


# ── SubmitPlan ─────────────────────────────────────────────────
class SubmitPlanInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self.content = params.get("content")
        self._parent = parent_coara

    def get_description(self) -> str:
        return "提交计划给用户审批"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            return ToolResult.error("提交计划工具未绑定到 coara 实例。")

        if not self._parent.is_plan_mode:
            return ToolResult.error('不在计划模式里。plan_mode(action="submit") 只能在计划模式下使用。')

        # 已提交待批准：同一回合内禁止再提交（调整要等用户下一条消息后重新 submit）。
        # 直接抛 CoaraRunCancelledError 收尾回合——拒绝的同时退出，LLM 没有机会反复撞。
        if getattr(self._parent, "_plan_pending_approval", False):
            from src.coara.turn_completion import CoaraRunCancelledError

            raise CoaraRunCancelledError("plan_pending_approval_resubmit")

        plan_path = self._parent.plan_file_path
        if plan_path is None:
            return ToolResult.error("计划模式未分配计划文件，请重新进入计划模式。")

        # content 必填：submit 就是提交计划全文，写&展示一步完成。不给 content 等于
        # 没计划可提交（也堵死「先 write/edit 写文件再 submit」的旁路——写了不展示的缝隙）。
        if self.content is None or not str(self.content).strip():
            return ToolResult.error(
                "submit 必须带 content（计划全文 Markdown）。"
                '直接 plan_mode(action="submit", content="计划全文") 一步写好并展示。'
            )

        from src.core.json_store import write_text_atomic

        write_text_atomic(plan_path, str(self.content))

        plan_content = plan_path.read_text(encoding="utf-8")
        try:
            await _publish_plan_review(plan_content, plan_path, parent=self._parent)
        except RuntimeError as exc:
            return ToolResult.error(str(exc))

        # 对话式审批：展示计划后回合即正常收尾（抛 PlanSubmittedError，编排器
        # 静默收尾、不注入打断文案）。用户下一条消息正常进对话，LLM 结合刚展示的
        # 计划自然判断用户决定，无需系统拦截包装
        # 硬锁：置「待批准」，同回合内 exit/重复 submit 被拒，直到用户新消息清锁
        self._parent._plan_pending_approval = True
        from src.coara.turn_completion import PlanSubmittedError

        raise PlanSubmittedError(plan_file=str(plan_path))


# ── ExitPlanMode ───────────────────────────────────────────────
class ExitPlanModeInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara

    def get_description(self) -> str:
        return "直接退出计划模式"

    async def execute(self, signal=None) -> ToolResult:
        if self._parent is None:
            return ToolResult.error("退出计划模式工具未绑定到 coara 实例。")

        if not self._parent.is_plan_mode:
            return ToolResult.error("当前不在计划模式里，不需要退出。")

        # 待批准锁：计划已提交未获用户回应，禁止自行退出放行——
        # 退出必须等用户下一条消息（新回合/接续输入清锁后）再执行。
        # 直接抛 CoaraRunCancelledError 收尾回合：拒绝的同时退出，LLM 无法反复尝试。
        if getattr(self._parent, "_plan_pending_approval", False):
            from src.coara.turn_completion import CoaraRunCancelledError

            raise CoaraRunCancelledError("plan_pending_approval_exit")

        plan_path = self._parent.plan_file_path
        self._parent.exit_plan_mode()
        return ToolResult.success(
            content="计划模式已退出，所有工具恢复可用。",
            metadata={
                "plan_file": str(plan_path) if plan_path is not None else None,
                "cancelled": True,
            },
        )


# ── PlanModeTool ───────────────────────────────────────────────
class PlanModeTool(BaseTool):
    name = "plan_mode"
    description = """计划模式，本质是进入只读状态，核心是**讨论**——自由对话、查代码、摸结构、和用户把方案聊清楚。

适用
- 用户要求进入计划模式或者用户只想先讨论
- 需要先做计划的复杂任务，包括但不限于：
    - 新增模块、重构核心流程或改动公共接口
    - 操作有破坏性或不可逆性（删文件、清数据、硬重置等）

不适用
- 单文件小修小补（错别字、明显 bug、微调样式等）
- 纯信息查询、代码解释、纯调研

注意事项
- action 三选一：enter=开启计划模式; submit=写&提交一体，把计划/方案全文提交用户审批; exit=直接退出计划模式
- content（必填）：计划/方案全文 Markdown
- 禁止在进入计划模式后的对话流里直接输出计划/方案。凡要输出方案，一律用走submit，在content里写计划/方案全文
- 禁止在未进入计划模式时使用submit工具
- submit 是可选项，不一定要执行这个动作
- 进入计划模式后不允许改动文件
"""
    display_name = "PlanMode"
    category = "system"
    # Side-effectful session state; must not use the global THINK tool cache.
    kind = ToolKind.OTHER
    owner_only = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["enter", "submit", "exit"],
                "description": "enter=开启; submit=提交审批; exit=直接退出计划模式",
            },
            "content": {
                "type": "string",
                "description": (
                    "submit 时的计划全文 Markdown：写入计划文件并展示给用户。"
                    "正文标题从二级（##）起，一级留给外壳的「计划审阅」"
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None):
        super().__init__()
        self._parent = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        action = str(params.get("action") or "").strip()
        if action == "enter":
            return EnterPlanModeInvocation(params, self._parent)
        if action == "submit":
            return SubmitPlanInvocation(params, self._parent)
        if action == "exit":
            return ExitPlanModeInvocation(params, self._parent)
        raise ValueError(f"Unknown plan_mode action: {action}. Use enter, submit, or exit.")

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        return None

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        # 会话级状态切换，串行
        return "plan_mode"


ROOT_PLAN_MODE_TOOL_TYPES = (PlanModeTool,)
