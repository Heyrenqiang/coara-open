"""Delegate Tool — 委派任务给 SubAgent 处理。

通过 delegate 工具将任务委派给专用子智能体。
每个 SubAgent 在独立上下文中运行，拥有自己的工具集和指令。
这是一个按需创建的进程内委派机制，创建的 SubAgent 是 IN_PROCESS 对象（非进程）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from pathlib import Path
from typing import Any

from src.coara.background_agent import BackgroundAgentManager
from src.coara.base import CoaraBase
from src.coara.builtin_agents import (
    BUILTIN_SUBAGENTS,
    CLI_SILENT_SUBAGENT_TYPES,
    SYSTEM_ONLY_SUBAGENT_TYPES,
    get_enabled_subagents,
    get_subagent,
    removed_subagent_type_message,
)
from src.coara.subagent_store import SubagentRecord, SubagentStore, subagent_store_for_workspace
from src.core.logger import logger
from src.core.time import now_iso
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import CoaraPersona, CoaraStatus, MessageRole, SubagentStatus
from src.tools.builtin.delegate.failure_classifier import classify_subagent_failure

# Subagents inherit unlimited root iterations by default; cap to avoid multi-hour
# foreground delegate runs when prompts are open-ended (e.g. parallel coaras + web_search).
SUBAGENT_MAX_TOOL_ITERATIONS = 1500

# Running subagent registry for the main→sub message channel: subagent_id → CoaraBase.
# Registered at delegate start, removed when the run finishes (any mode).
_RUNNING_SUBAGENTS: dict[str, CoaraBase] = {}

# Foreground delegate asyncio.Tasks: subagent_id → Task. Lets delegate(action=stop)
# hard-cancel foreground delegates (background ones go through BackgroundAgentManager).
_RUNNING_FG_TASKS: dict[str, asyncio.Task[Any]] = {}
_RUNNING_FG_DESCRIPTIONS: dict[str, str] = {}

# All in-flight subagents (fg + bg), for Ctrl+C / /stop hard interrupt of LLM/tools
# before asyncio.Task.cancel(). Channel registry below is a subset (fg coaras only).
_ACTIVE_SUBAGENTS: dict[str, CoaraBase] = {}


def _origin_metadata(subagent: Any) -> dict[str, str]:
    """段归属字段：source + matrix channel（供完成通知 / continuation 直推）。"""
    snap = getattr(subagent, "_subagent_origin", None)
    source = ""
    channel = ""
    if isinstance(snap, (tuple, list)) and len(snap) >= 1:
        source = str(snap[0] or "").strip()
        if len(snap) >= 2 and snap[1]:
            channel = str(snap[1]).strip()
    out: dict[str, str] = {"subagent_origin": source}
    if channel:
        out["subagent_origin_channel"] = channel
    return out


def lookup_running_subagent(task_id: str) -> CoaraBase | None:
    """Return the running channel-enabled subagent for ``task_id``, else None."""
    return _RUNNING_SUBAGENTS.get(task_id)


def delegate_system_prompt(base: str, *, channel: bool) -> str:
    """Subagent system prompt = 类型模板（通道节与「不要中间输出」纪律已直接写在 .md 里）。

    The 主⇄子 channel section lives in ``coaras.md`` and the 不要中间输出
    discipline is written into every subagent ``.md`` directly, so there is no
    runtime placeholder resolution — *channel* is kept for the caller's gating
    (registers the ``interact`` tool) but does not alter the prompt.
    """
    return base


# Minimum interval between incremental breakpoint writes of a running subagent.
# Full single-file rewrites are cheap (atomic write), but a fast tool loop can
# otherwise trigger one write per tool result.
_SUBAGENT_CHECKPOINT_MIN_INTERVAL_SECONDS = 5.0


class SubagentCheckpointer:
    """Incrementally persist a running subagent's breakpoint to SubagentStore.

    The terminal write in ``_run_subagent`` only happens when the run ends;
    a process kill mid-run used to lose all intermediate progress. The agent
    loop yields a chunk after every tool batch, so callers simply invoke
    ``maybe_write()`` per chunk — writes are throttled, and the atomic
    single-file rewrite keeps the on-disk record always loadable.
    """

    def __init__(
        self,
        store: SubagentStore,
        *,
        agent_id: str,
        subagent_type: str,
        description: str,
        background: bool,
        parent_coara_id: str | None,
        min_interval: float = _SUBAGENT_CHECKPOINT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._agent_id = agent_id
        self._subagent_type = subagent_type
        self._description = description
        self._background = background
        self._parent_coara_id = parent_coara_id
        self._min_interval = min_interval
        self._last_write = 0.0

    def maybe_write(self, subagent: CoaraBase) -> bool:
        """Write a checkpoint when the throttle interval has elapsed."""
        now = time.monotonic()
        if self._last_write and now - self._last_write < self._min_interval:
            return False
        self._last_write = now
        self.write(subagent)
        return True

    def write(self, subagent: CoaraBase) -> None:
        """Full rewrite of this agent's single record with running status."""
        try:
            existing = self._store.load(self._agent_id)
            status = (
                SubagentStatus.RUNNING_BACKGROUND.value if self._background else SubagentStatus.RUNNING_FOREGROUND.value
            )
            self._store.save(
                SubagentRecord(
                    agent_id=self._agent_id,
                    subagent_type=self._subagent_type,
                    description=self._description,
                    message_history=SubagentStore.serialize_message_history(subagent.message_history),
                    status=status,
                    created_at=existing.created_at if existing else now_iso(),
                    updated_at=now_iso(),
                    session_id=subagent.session_id,
                    child_coara_id=subagent.identity.coara_id,
                    parent_coara_id=(
                        existing.parent_coara_id if existing and existing.parent_coara_id else self._parent_coara_id
                    ),
                    mode="background" if self._background else "foreground",
                )
            )
        except Exception as exc:
            logger.warning(f"Subagent checkpoint write failed for {self._agent_id}: {exc}")


def _snapshot_cancellable_delegates() -> list[tuple[str, str]]:
    """Capture (task_id, description) for in-flight delegates before hard-cancel.

    Used to enrich the interrupt system message so the next turn can
    ``delegate(action=\"resume\", …)``.
    """
    from src.coara.background_agent import BackgroundAgentManager

    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(task_id: str, description: str = "") -> None:
        if not task_id or task_id in seen:
            return
        seen.add(task_id)
        items.append((task_id, (description or "").strip()))

    for task_id, task in list(_RUNNING_FG_TASKS.items()):
        if task is None or task.done():
            continue
        _add(task_id, _RUNNING_FG_DESCRIPTIONS.get(task_id, ""))

    for task_id in list(_ACTIVE_SUBAGENTS):
        _add(task_id, _RUNNING_FG_DESCRIPTIONS.get(task_id, ""))

    for task_id, bg_task in list(BackgroundAgentManager()._tasks.items()):
        if bg_task is not None and bg_task.done():
            continue
        desc = ""
        try:
            from src.tools.builtin.background.task_support import resolve_task_store

            record = resolve_task_store().load(task_id)
            if record is not None:
                desc = getattr(record, "description", "") or ""
        except Exception:
            logger.debug(f"后台任务描述查询失败（interrupt note 缺描述）: {task_id}")
        _add(task_id, desc)

    return items


def format_turn_interrupt_note(
    cancelled_delegates: list[tuple[str, str]] | None = None,
) -> str:
    """Build the ``当前会话已打断`` system-message body (with optional resume tip)."""
    note = "当前会话已打断。"
    if not cancelled_delegates:
        return note
    lines = [
        note,
        '已硬停的子智能体（继续原任务时用 delegate(action="resume", task_id=…) 从断点接着跑）：',
    ]
    for task_id, description in cancelled_delegates:
        if description:
            lines.append(f"- {task_id}：{description}")
        else:
            lines.append(f"- {task_id}")
    return "\n".join(lines)


def hard_cancel_all_running_delegates(*, reason: str = "user_interrupt") -> dict[str, Any]:
    """Force-stop every in-flight subagent and background task (Ctrl+C / /stop).

    Covers: foreground delegates, background agent delegates, bash ``task`` jobs.
    Order: interrupt each child CoaraBase (abort LLM/tools) → ``task.cancel()`` on
    all fg/bg/bash asyncio tasks. Nested child interrupts pass
    ``cancel_delegates=False`` so this does not recurse.

    Returns counts plus ``cancelled_delegates``: ``[(task_id, description), …]``
    snapshot taken before cancel (for interrupt history injection).
    """
    cancelled_delegates = _snapshot_cancellable_delegates()

    interrupted = 0
    for subagent_id, subagent in list(_ACTIVE_SUBAGENTS.items()):
        try:
            if subagent.interrupt_current_turn(
                reason,
                interrupt_source="hard_cancel_all_delegates",
                cancel_delegates=False,
            ):
                interrupted += 1
            else:
                # Child may not have entered process_message yet — still kill HTTP.
                try:
                    from src.llm.service import llm_service

                    llm_service.abort_active()
                    if subagent.provider is not None:
                        subagent.provider.abort()
                except Exception:
                    pass  # 外层 except 的 debug 已覆盖本子智能体，这里有意静默
        except Exception as exc:
            logger.debug(f"hard_cancel interrupt {subagent_id} failed: {exc}")

    fg_cancelled = 0
    for _task_id, task in list(_RUNNING_FG_TASKS.items()):
        if task.done():
            continue
        task.cancel()
        fg_cancelled += 1

    from src.background.bash_runner import BashBackgroundRunner
    from src.coara.background_agent import BackgroundAgentManager

    bg_cancelled = BackgroundAgentManager().cancel_all()
    bash_cancelled = BashBackgroundRunner().cancel_all()
    total_cancelled = interrupted + fg_cancelled + bg_cancelled + bash_cancelled
    if total_cancelled:
        logger.warning(
            f"Hard-cancelled work: interrupted={interrupted} fg_tasks={fg_cancelled} "
            f"bg_agents={bg_cancelled} bash_tasks={bash_cancelled} reason={reason}"
        )
    else:
        # 正常打断（Ctrl+C / /stop）时通常没有在跑的子智能体/后台任务，不刷屏
        logger.debug(f"No active work to hard-cancel (reason={reason})")
    return {
        "subagents_interrupted": interrupted,
        "fg_tasks": fg_cancelled,
        "bg_tasks": bg_cancelled,
        "bash_tasks": bash_cancelled,
        "cancelled_delegates": cancelled_delegates,
    }


def _cleanup_fg_registries(subagent_id: str) -> None:
    """Drop fg registries even when the task was cancelled before it ever ran."""
    _RUNNING_FG_TASKS.pop(subagent_id, None)
    _RUNNING_FG_DESCRIPTIONS.pop(subagent_id, None)


_DELEGATE_DESCRIPTION_TEMPLATE = """委派子智能体执行任务

内置可用子智能体列表
${SUBAGENT_LIST}

你可以调用子智能体来完成任务，同一子智能体有两种运行模式，前台 `background=false`，后台 `background=true`
前台模式
- 目的：加速任务运行。同时启动多个子智能体并行干活，**你自己也是并行的一部分**——亲自承担其中一条任务分支
- 调用后：不必等子智能体的结果，可继续推进你自己的分支任务。

后台模式
- 定位：辅助助手。你可以交给他一些辅助任务，它会默默在任务完成后把结果返回给你
- 调用后：你不必等它，本轮次可正常结束，以后的轮次也是正常对话

规划原则
- 遇到某些任务，鼓励并行调用多个 coaras，加快任务完成速度
- 会话过程中，鼓励适当调用aide
- 看到接续输入带来可独立的新任务时，可用调用 `coaras` 分支并行

注意事项
- coaras 必须前台，aide 可前台可后台
- 简单小步任务不要 delegate
- 已委派给子智能体的工作，你绝对不能重复去做，等子智能体的最终结果即可
- 用 `delegate(action="message")` 向子智能体传达信息，包括新任务、新信息、新要求等
- 你自己的活干完且还有子智能体运行的时候调`delegate(action="wait")`等子智能体完成，禁止做任何多余的工具调用来填充轮次——这非常消耗词元，代价巨大
- 有新任务不依赖在跑子智能体结果时，直接推进，不需要等在跑的子智能体任务完成
- 禁止调用`delegate(action="wait")`等待非子智能体任务
- 后台子智能体任务完成时会注入 `<后台结果>`，不要去复述结果
- `description` 只是一句话任务名

子智能体控制
- 途中干预（纠偏/补充/问进展）：`delegate(action="message", task_id=…, prompt=…)`——仅限**运行中**的子智能体；已结束的子智能体不能 message，要追加任务用 resume（误发会自动按 resume 续跑，不会报错，但动作要用对）
- 需要叫停并收回结果时：**优先 message 软收尾**——`delegate(action="message", task_id=…, prompt=…)`，在 prompt 里写清「立刻停止继续干活，把已完成部分汇总并马上回报」；子智能体收消息后会收束并交付，已有进展得以保留
- 硬停 `delegate(action="stop", task_id=…)` 会立刻掐断运行中工作，**进行中的结果全部丢弃**，代价很大；仅当 message 无效、子智能体失控、或必须立即终止时才用，且会弹确认审批——**禁止**作为默认叫停手段
- 等结果：`delegate(action="wait")`等待子智能体完成；用户发新消息后可继续推进，处理完后子智能体还没完成要继续 wait
- 所有子智能体的结果都不会立刻返回，你也看不到它的实时活动，没产出并也不是卡死，要等它结果返回
- 误停用`delegate(action="resume", task_id=…)` 从断点续跑

如何写 prompt，务必重视
子智能体看不到对话上下文，`prompt` 就是它收到的全部任务信息。你要像你自己的规范一样去规范它：交待任务的同时，还要给出方法、技巧与规则等，教它怎么把事做好——这些没有写死的通用模板，而是你为这个任务量身定制的，必须有

prompt 必须自洽完整，包含
1. 任务目标：做什么、为什么做
2. 范围：涉及的绝对路径与模块，以及明确不碰的部分
3. 方法指导：建议的执行顺序、工具用法、技巧与易踩的坑
4. 规则与禁止项：本任务的硬约束，如只读不改、不动无关代码、遵守项目约定
5. 验收标准：怎样算完成
6. 回报格式：最终回报必须包含 结论摘要、涉及的绝对路径、关键数据等
"""


def _format_subagent_list_entry(sa: Any) -> str:
    """Render one subagent for the delegate tool description list."""
    role = (getattr(sa, "role", None) or "").strip() or sa.name
    when = " ".join((getattr(sa, "description", None) or "").split())
    if not when:
        when = "（未配置）"
    # 名字加粗 + role 作描述；何时用单独一行，避免与 role 重复堆砌
    return f"- **{sa.name}** — {role}\n  何时用 {when}"


class DelegateTool(BaseTool):
    """委派工具：将任务委派给专用 SubAgent 处理。

    动态注入可用 SubAgent 列表到工具描述中，
    LLM 通过 subagent_type 参数选择使用哪个 SubAgent。

    注意：delegate 是常驻的三大执行层之一，按需创建进程内 SubAgent（轻量级，同进程协程）。
    """

    name = "delegate"
    description = "委派任务给专用子代理处理"  # _update_description() 覆盖为动态版本
    display_name = "Delegate"
    category = "agent"
    kind = ToolKind.EXECUTE
    owner_only = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["spawn", "message", "resume", "wait", "stop"],
                "description": (
                    "spawn=委派新任务; message=向运行中的子智能体发途中消息，"
                    "纠偏/补充/问进展/软叫停并要求立刻汇总回报；需带 task_id；"
                    "已结束的子智能体不能 message，误发会自动按 resume 续跑; "
                    "resume=恢复子智能体并带上 prompt，"
                    "被打断的从断点续跑；已完成的在其上下文之上追加新任务，prompt 必填——"
                    "优先用于「让刚审计/调研完它的那位继续修」，避免新开冷启动重复摸底，需带 task_id; "
                    "wait=阻塞等齐全部在跑的前台子智能体，task_id 指定时只等它; "
                    "stop=硬停，丢弃进行中结果，代价大；仅 message 无效或必须立刻掐断时用；"
                    "需带 task_id，弹确认审批——禁止作默认叫停"
                ),
                "default": "spawn",
            },
            "task_id": {
                "type": "string",
                "description": (
                    "action=message/resume/stop 时必填、action=wait 时可选，目标子智能体任务 ID（如 sa-coaras-xxxxxxxx）。"
                    "resume 已完成任务时 prompt 为追加的新任务指令"
                ),
            },
            "description": {
                "type": "string",
                "description": "一句话任务名，简短概括做什么，action=spawn 时必填",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "action=spawn 为任务的完整指令。子智能体看不到对话上下文，这是它收到的全部任务信息，"
                    "写清目标、范围（绝对路径）、方法指导、规则与禁止项、验收标准、回报格式。"
                    "action=message 为要发给运行中子智能体的消息正文；"
                    "软叫停时写明立刻停手、汇总已完成部分并马上回报，优先于 action=stop"
                ),
            },
            "subagent_type": {
                "type": "string",
                "enum": [sa.name for sa in BUILTIN_SUBAGENTS if sa.name not in SYSTEM_ONLY_SUBAGENT_TYPES],
                "description": "要使用的 SubAgent 类型，action=spawn 时必填",
            },
            "background": {
                "type": "boolean",
                "description": ("是否在后台异步执行"),
                "default": False,
            },
            "workspace": {
                "type": "string",
                "description": (
                    "可选。锁定子代理的工作空间名，即登记表中的别名，使用登记表中的工作空间根路径。"
                    "不填则使用父代理当前激活的工作目录。"
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None):
        super().__init__()
        self._parent = parent_coara
        # Ensure instance-level copy so mutations don't affect the class attribute.
        self.parameters_schema = dict(self.parameters_schema)
        self._update_description()

    def _update_description(self) -> None:
        """动态更新工具描述，注入可用子代理列表。"""
        enabled_subagents = self._get_enabled_subagents()
        enabled_names = [sa.name for sa in enabled_subagents]
        subagent_desc = "\n".join(_format_subagent_list_entry(sa) for sa in enabled_subagents)

        self.parameters_schema = {
            **self.parameters_schema,
            "properties": {
                **self.parameters_schema["properties"],
                "subagent_type": {
                    **self.parameters_schema["properties"]["subagent_type"],
                    "enum": enabled_names,
                },
            },
        }
        self.description = _DELEGATE_DESCRIPTION_TEMPLATE.replace("${SUBAGENT_LIST}", subagent_desc)

    def _get_enabled_subagents(self) -> list[Any]:
        if self._parent is None:
            return get_enabled_subagents(None)
        return get_enabled_subagents(None)

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return DelegateToolInvocation(params, self._parent)

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        """Hard-stop terminates in-flight work — same gate as former task(action=stop) for agents."""
        action = str(args.get("action", "") or "").strip() if isinstance(args, dict) else ""
        return action == "stop"

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        # Delegate is a full child-agent run, so it should end when the child
        # finishes, fails, or is interrupted, not when the generic tool timeout elapses.
        return None


class DelegateToolInvocation(ToolInvocation):
    """委派工具调用实例。"""

    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara
        # 瞬时失败自动续跑防循环标志：一次 delegate 调用最多自动续跑一次
        self._auto_resumed_once = False
        # 自动续跑会重建 CoaraBase。外层 finally 必须持久化重建后的实例，
        # 否则会用首次失败实例覆盖续跑后的完整历史和会话标识。
        self._auto_resume_persist_subagent: CoaraBase | None = None
        self.action = str(params.get("action") or "spawn").strip().lower() or "spawn"
        self.task_id = str(params.get("task_id") or "").strip()

        if self.action == "message":
            if not self.task_id:
                raise ValueError("action=message 缺少必填参数: task_id（目标子智能体任务 ID）")
            if not str(params.get("prompt") or "").strip():
                raise ValueError("action=message 缺少必填参数: prompt（要发送的消息正文）")
            self.description = str(params.get("description") or f"向 {self.task_id} 发送途中消息")
            self.prompt = str(params["prompt"])
            self._raw_subagent_type = str(params.get("subagent_type") or "")
            self.subagent_type = self._raw_subagent_type.strip().lower()
            self.background = False
            self.workspace = None
            self.provider = None
            self.model = None
            self.system_dispatch = bool(params.get("system_dispatch", False))
            return

        if self.action == "resume":
            if not self.task_id:
                raise ValueError("action=resume 缺少必填参数: task_id（要恢复的子智能体任务 ID）")
            self.description = str(params.get("description") or f"恢复 {self.task_id}")
            # 已完成（idle）任务的追加指令经 prompt 传入 _execute_resume 包装注入；
            # 可空=纯断点续跑。之前这里硬置空 追加任务被静默丢弃（只重报旧结果的根因）
            self.prompt = str(params.get("prompt") or "")
            self._raw_subagent_type = ""
            self.subagent_type = ""
            self.background = False  # resolved from the stored record
            self.workspace = None
            self.provider = None
            self.model = None
            self.system_dispatch = False
            return

        if self.action == "wait":
            self.description = str(
                params.get("description") or (f"等待 {self.task_id}" if self.task_id else "等待子智能体结果")
            )
            self.prompt = ""
            self._raw_subagent_type = ""
            self.subagent_type = ""
            self.background = False
            self.workspace = None
            self.provider = None
            self.model = None
            self.system_dispatch = False
            return

        if self.action == "stop":
            if not self.task_id:
                raise ValueError("action=stop 缺少必填参数: task_id（目标子智能体任务 ID）")
            self.description = str(params.get("description") or f"停止 {self.task_id}")
            self.prompt = ""
            self._raw_subagent_type = ""
            self.subagent_type = ""
            self.background = False
            self.workspace = None
            self.provider = None
            self.model = None
            self.system_dispatch = False
            return

        if self.action != "spawn":
            raise ValueError(f"未知 action: {self.action}（支持 spawn / message / resume / wait / stop）")

        for key in ("description", "prompt", "subagent_type"):
            if key not in params:
                raise ValueError(f"Missing required parameter: {key}")

        self.description = params["description"]
        self.prompt = params["prompt"]
        self._raw_subagent_type = str(params["subagent_type"])
        self.subagent_type = self._raw_subagent_type.strip().lower()
        self.background = bool(params.get("background", False))
        # aide 与 coaras 同一套机制：前台/后台由调用方决定（唯一差异：
        # aide 可后台，coaras 只能前台）。
        if self.subagent_type == "coaras" and self.background:
            raise ValueError("coaras 只能前台运行（background 必须 false）；需要后台默默执行的场景请派 aide")
        self.workspace = (params.get("workspace") or "").strip() or None
        self.system_dispatch = bool(params.get("system_dispatch", False))
        # provider/model 是系统内部调度通道（janitor 等），不在 LLM 面 schema 内。
        # 仅系统调度才接受：LLM 可能发出 schema 外参数（实测把 media 的
        # provider 默认值 'agnes' 抄进 delegate 调用，把子智能体钉死到错误 provider），
        # 非系统调度一律忽略，强制继承父会话。
        if self.system_dispatch:
            self.provider = str(params.get("provider") or "").strip() or None
            self.model = str(params.get("model") or "").strip() or None
        else:
            self.provider = None
            self.model = None

    def get_description(self) -> str:
        if self.action == "message":
            return f"Delegate→[{self.task_id}]: 途中消息"
        if self.action == "stop":
            return f"Delegate stop [{self.task_id}]"
        if self.action == "wait":
            return f"Delegate wait [{self.task_id}]" if self.task_id else "Delegate wait"
        if self.action == "resume":
            return f"Delegate resume [{self.task_id}]"
        mode = "[bg]" if self.background else ""
        return f"Delegate{mode} [{self.subagent_type}]: {self.description}"

    def _channel_enabled(self) -> bool:
        """主⇄子双向通道挂在任务型前台子智能体（coaras/aide）上
        （interact + 途中消息 + 提示词通道节）。

        后台运行按后台语义走（结果回投，无中途通道）；
        janitor/daily 是系统派发（无父会话续接概念），一律无通道。
        """
        return (
            self.action == "spawn"
            and self.subagent_type in ("coaras", "aide")
            and not self.background
            and not self.system_dispatch
        )

    def _resolve_workspace_dir(self) -> tuple[Any, str | None]:
        """Resolve delegate workspace alias to a workspace root path."""
        from pathlib import Path

        if self._parent is None:
            return Path.cwd(), None
        if not self.workspace:
            return Path(self._parent.workspace_dir).resolve(), None
        manager = getattr(self._parent, "workspace_manager", None)
        if manager is None:
            return (
                None,
                f"Workspace manager unavailable; cannot resolve workspace '{self.workspace}'.",
            )
        entry = manager.registry.resolve_name_or_id(self.workspace)
        if entry is None:
            known = ", ".join(sorted(e.name for e in manager.registry.document.workspaces.values())) or "(none)"
            return None, f"Unknown workspace '{self.workspace}'. Registered: {known}"
        return entry.resolved_path(), None

    def _coara_home(self) -> Path | None:
        if self._parent is None:
            return None
        wm = getattr(self._parent, "workspace_manager", None)
        return wm.coara_home if wm is not None else None

    def _subagent_store_for_dir(self, workspace_dir: Path) -> SubagentStore:
        return subagent_store_for_workspace(workspace_dir, coara_home=self._coara_home())

    def _resolve_persona_parts(self, subagent_config: Any) -> tuple[Any, Any]:
        """按子智能体配置生成 persona（spawn/resume 共用）。janitor 已机制化，不经此路径。"""
        return (
            delegate_system_prompt(subagent_config.system_prompt, channel=self._channel_enabled()),
            subagent_config.yaml_config,
        )

    def _build_subagent(
        self,
        subagent_id: str,
        subagent_config: Any,
        workspace_dir: Any,
        *,
        provider_name: Any,
        model: Any,
    ) -> CoaraBase:
        """构建子智能体 CoaraBase（spawn 与 resume 共用）。

        差异只在输入：spawn 允许本次调用覆盖 provider/model 并以全新历史启动；
        resume 固定沿用父级 provider/model，断点历史随后由调用方恢复。
        """
        assert self._parent is not None
        persona_template, persona_yaml_config = self._resolve_persona_parts(subagent_config)
        subagent = CoaraBase(
            name=subagent_id,
            persona=CoaraPersona(
                name=subagent_config.name,
                role=subagent_config.role,
                system_prompt_template=persona_template,
                yaml_config=persona_yaml_config,
            ),
            workspace_dir=workspace_dir,
            provider_name=provider_name,
            model=model,
            delegate_depth=self._parent.delegate_depth + 1,
            audit_session_id=self._parent.audit_session_id,
            max_tool_iterations=SUBAGENT_MAX_TOOL_ITERATIONS,
            # janitor / daily need owner_only tools; still non-user-facing.
            user_facing=False,
            is_owner_context=self.subagent_type in ("janitor", "daily"),
            # is_owner_context 会让 base 默认 agent_kind=main（拿 owner_only 工具
            # 的副作用），导致 janitor/daily 的对话事件被当主会话 hydrate 进 web
            # 聊天区。它们是后台维护子智能体，事件身份必须是 subagent（被投影层
            # 默认排除），工具权限仍由 is_owner_context 保证。resume 断点续跑同此。
            session_agent_kind="subagent",
        )
        subagent._delegate_background = bool(self.background)
        # 归属：子智能体的正文流要折叠回发起它的 delegate 工具行（端寻址只投 web），
        # 端侧靠这两个键把 chunk 归位——父会话 id（靠它找到在飞的 web 回合流）
        # 与那条工具行的 call_id。
        subagent._delegate_parent_session_id = str(getattr(self._parent, "session_id", "") or "")
        subagent._delegate_parent_workspace_dir = str(getattr(self._parent, "workspace_dir", "") or "")
        subagent._delegate_parent_tool_call_id = str(getattr(self, "tool_call_id", "") or "")
        subagent._delegate_subagent_id = subagent_id
        # 派发端归属（段来源 + 端内连接）：子智能体的正文 / 工具行 / 最终结果都只投
        # 派发它的那一端（web 折叠区），段来源在派发时快照固定，不随运行中用户换端
        # 漂移。spawn / resume / 断点自动续跑三条路都经这里构建——漏设会让 resume
        # 的折叠输出被 origin 守卫整条丢掉（工具行、chunk、最终结果全丢，实际缺陷）。
        subagent._subagent_parent = self._parent
        try:
            _seg_src = str(getattr(getattr(self._parent, "_segments", None), "source", "") or "").strip()
            if not _seg_src:
                from src.coara.turn_source import current_turn_source

                _seg_src = current_turn_source(self._parent)
            if not _seg_src:
                _seg_src = str(getattr(self._parent, "_last_user_input_source", "") or "").strip() or "cli"
            _origin_channel: str | None = None
            _origin = getattr(self._parent, "session_origin", None)
            if isinstance(_origin, dict):
                _origin_channel = _origin.get("channel_id")
            if _seg_src == "matrix" and not _origin_channel:
                from src.coara.turn_context import get_turn_channel_id

                _origin_channel = get_turn_channel_id()
            subagent._subagent_origin = (_seg_src, _origin_channel)
        except Exception:  # noqa: BLE001 — 来源登记失败不阻断派发
            subagent._subagent_origin = ("cli", None)
        # 子智能体 diff 路由依赖 end_registry（经 _root_ref 取）：继承父会话的
        # root 引用，否则 _route_tool_diff 第一步就查不到 registry、diff 全丢。
        subagent._root_ref = getattr(self._parent, "_root_ref", None)
        # 系统静默子智能体（janitor/daily）：CLI 不渲染其工具 diff/摘要
        # （resume 断点续跑同样恢复该标记）
        subagent._cli_silent = self.subagent_type in CLI_SILENT_SUBAGENT_TYPES
        if self.subagent_type == "daily":
            # daily 统筹全部工作空间：不注入任何单个工作空间的环境上下文/概况
            subagent.inject_environment_seed = False
        return subagent

    def _wire_subagent(self, subagent: CoaraBase, subagent_config: Any) -> set[str]:
        """计算并应用子代理工具白名单，并把子智能体 trace 事件接入父级 trace sink。"""
        assert self._parent is not None
        tool_whitelist = self._resolve_tool_whitelist(subagent_config.tools)
        subagent._tool_manager.set_whitelist(tool_whitelist)
        # Wire subagent trace events into parent's trace sink so CLI/Dashboard see them
        if self._parent._trace_sink:
            subagent.set_trace_sink(self._parent._trace_sink)
        return tool_whitelist

    def _save_running_record(
        self,
        store: SubagentStore,
        subagent: CoaraBase,
        subagent_id: str,
        description: str,
        *,
        created_at: str,
    ) -> None:
        """保存/更新 SubagentStore 状态为运行中（spawn 新建 / resume 覆盖断点共用）。"""
        status = SubagentStatus.RUNNING_BACKGROUND.value if self.background else SubagentStatus.RUNNING_FOREGROUND.value
        store.save(
            SubagentRecord(
                agent_id=subagent_id,
                subagent_type=self.subagent_type,
                description=description,
                message_history=SubagentStore.serialize_message_history(subagent.message_history),
                status=status,
                created_at=created_at,
                updated_at=now_iso(),
                session_id=subagent.session_id,
                child_coara_id=subagent.identity.coara_id,
                parent_coara_id=self._parent.identity.coara_id if self._parent else None,
                mode="background" if self.background else "foreground",
            )
        )

    async def _dispatch_subagent(
        self,
        subagent: CoaraBase,
        subagent_id: str,
        subagent_config: Any,
        tool_whitelist: set[str],
        signal,
        *,
        description: str,
        resumed: bool,
    ) -> ToolResult:
        """前后台分发（spawn 与 resume 共用）。

        resumed=False 为全新 spawn：前台额外发 subagent_start 生命周期事件；
        resumed=True 为断点恢复：返回文案/元数据带恢复语义。
        """
        # 后台模式：将子代理执行放到 BackgroundAgentManager，立即返回
        assert self._parent is not None

        if self.background:

            async def _bg_coro() -> ToolResult:
                return await self._run_subagent(subagent, subagent_id, subagent_config, tool_whitelist, signal)

            task_id = await BackgroundAgentManager().start(
                parent_coara=self._parent,
                coro=_bg_coro,
                task_id=subagent_id,
                subagent_type=self.subagent_type,
                description=description,
                child_coara_id=subagent.identity.coara_id,
                parent_tool_call_id=getattr(self, "tool_call_id", None),
            )
            if resumed:
                return ToolResult.success(
                    content=f"已从断点恢复后台任务 [{subagent_id}]，{self.subagent_type} 继续处理中，完成后会通知你。",
                    metadata={
                        "task_id": subagent_id,
                        "subagent_type": self.subagent_type,
                        "mode": "background",
                        "resumed": True,
                    },
                )
            return ToolResult.success(
                content=f"后台任务已启动 [{task_id}]，{self.subagent_type} 正在处理中，完成后会通知你。",
                metadata={
                    "task_id": task_id,
                    "subagent_type": self.subagent_type,
                    "description": description,
                    "mode": "background",
                },
            )

        # 前台异步模式：启动子代理为独立 asyncio.Task，立即返回占位符。
        # 主 LLM 可以继续推进其他工作；轮次退出守卫会阻止退出直到结果返回。
        # 结果通过 continuation_input 注入，下一次 ReAct 迭代自然看到。
        async def _fg_coro() -> ToolResult:
            return await self._run_subagent(subagent, subagent_id, subagent_config, tool_whitelist, signal)

        fg_task = asyncio.create_task(_fg_coro(), name=subagent_id)
        _RUNNING_FG_TASKS[subagent_id] = fg_task
        _RUNNING_FG_DESCRIPTIONS[subagent_id] = description
        self._parent.register_foreground_delegate(subagent_id, fg_task, description)

        def _on_fg_done(t: asyncio.Task[Any], sid: str = subagent_id, desc: str = description) -> None:
            assert self._parent is not None
            self._parent.on_foreground_delegate_done(sid, desc, t)

        def _forget_fg(sid: str = subagent_id) -> None:
            _cleanup_fg_registries(sid)

        fg_task.add_done_callback(_on_fg_done)

        def _forget_fg_done(t: asyncio.Task[Any], sid: str = subagent_id) -> None:
            _forget_fg(sid)

        fg_task.add_done_callback(_forget_fg_done)

        if resumed:
            return ToolResult.success(
                content=(
                    f"[前台子智能体已恢复] [{subagent_id}]\n"
                    f"类型：{self.subagent_type}\n"
                    f"任务：{description}\n"
                    f"状态：从断点继续运行中"
                ),
                metadata={
                    "task_id": subagent_id,
                    "subagent_type": self.subagent_type,
                    "mode": "foreground_async",
                    "resumed": True,
                },
            )

        # Emit start trace event for foreground async delegates
        self._emit_subagent_lifecycle_event(
            "subagent_start",
            f"Foreground async delegate started: {self.subagent_type}",
            subagent_id=subagent_id,
            child_session_id=subagent.session_id,
            child_coara_id=subagent.identity.coara_id,
        )
        logger.info(f"Foreground async delegate [{self.subagent_type}] started: {description} ({subagent_id})")

        return ToolResult.success(
            content=(
                f"[前台子智能体已启动] [{subagent_id}]\n"
                f"类型：{self.subagent_type}\n"
                f"任务：{description}\n"
                f"状态：运行中"
            ),
            metadata={
                "task_id": subagent_id,
                "subagent_type": self.subagent_type,
                "description": description,
                "mode": "foreground_async",
            },
        )

    async def execute(self, signal=None) -> ToolResult:
        if self.action == "message":
            return await self._execute_message(signal)
        if self.action == "resume":
            return await self._execute_resume(signal)
        if self.action == "wait":
            return await self._execute_wait(signal)
        if self.action == "stop":
            return await self._execute_stop(signal)

        logger.info(f"Delegate [{self.subagent_type}] background={self.background}")
        removed_msg = removed_subagent_type_message(self._raw_subagent_type)
        if removed_msg:
            return ToolResult.error(removed_msg)

        # janitor 已改为内核维护管道（janitor_maintenance），禁止再经 delegate 派发。
        if self.subagent_type == "janitor":
            return ToolResult.error("janitor 已改为内核维护管道，不再经 delegate 派发")

        # 1. 验证 SubAgent 类型
        subagent_config = get_subagent(self.subagent_type)
        if not subagent_config:
            available = ", ".join(sa.name for sa in BUILTIN_SUBAGENTS)
            return ToolResult.error(f"Unknown SubAgent type '{self._raw_subagent_type}'. Available types: {available}")

        if self._parent is None:
            return ToolResult.error("Delegate tool called without parent Coara context")

        if self._parent.delegate_depth >= 1:
            return ToolResult.error(
                f"Delegate depth limit reached (current depth={self._parent.delegate_depth}). "
                f"Subagents cannot create further subagents. Please complete the task directly."
            )

        enabled_subagents = get_enabled_subagents(None)
        enabled_names = [sa.name for sa in enabled_subagents]
        if self.subagent_type not in enabled_names:
            if (
                self.subagent_type in SYSTEM_ONLY_SUBAGENT_TYPES
                and self.system_dispatch
                and get_subagent(self.subagent_type) is not None
            ):
                pass  # system-only (daily): allowed only via system_dispatch
            else:
                if self.subagent_type in SYSTEM_ONLY_SUBAGENT_TYPES:
                    return ToolResult.error(
                        f"SubAgent '{self.subagent_type}' 仅限系统内部调度，不能通过 delegate 主动调用"
                    )
                available = ", ".join(enabled_names) or "(none)"
                return ToolResult.error(
                    f"SubAgent type '{self.subagent_type}' is not enabled for this Coara. Available types: {available}"
                )

        workspace_dir, workspace_error = self._resolve_workspace_dir()
        if workspace_error:
            return ToolResult.error(workspace_error)
        subagent_store = self._subagent_store_for_dir(Path(workspace_dir))

        subagent_id = f"sa-{self.subagent_type}-{uuid.uuid4().hex[:8]}"

        subagent = self._build_subagent(
            subagent_id,
            subagent_config,
            workspace_dir,
            provider_name=self.provider or getattr(self._parent, "provider_name", None),
            model=self.model or getattr(self._parent, "model_name", None),
        )

        # 子智能体段归属登记（段来源 + 端内连接）已随 `_build_subagent` 统一完成
        # ——spawn 与 resume 同口径，见那里的注释。

        if self.workspace:
            manager = getattr(self._parent, "workspace_manager", None)
            if manager is not None:
                entry = manager.registry.resolve_name_or_id(self.workspace)
                if entry is not None:
                    from src.workspace.vfs import VfsResolver

                    scoped_vfs = VfsResolver(strict=True)
                    scoped_vfs.set_mounts([entry], active_id=entry.id)
                    subagent._delegate_vfs = scoped_vfs

        # 3. 计算并应用子代理工具白名单 + 接入父级 trace sink
        tool_whitelist = self._wire_subagent(subagent, subagent_config)

        # 保存 SubagentStore 状态为运行中
        self._save_running_record(subagent_store, subagent, subagent_id, self.description, created_at=now_iso())

        # 同时写入 TaskStore（后台任务统一查询入口）
        if self.background:
            from src.background.task_store import TaskRecord as BgTaskRecord
            from src.background.task_store import TaskStatus as BgTaskStatus
            from src.background.task_store_paths import task_store_for_coara
            from src.coara.turn_source import current_turn_source

            task_store_for_coara(self._parent).save(
                BgTaskRecord(
                    task_id=subagent_id,
                    kind="agent",
                    description=self.description,
                    status=BgTaskStatus.RUNNING.value,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                    subagent_type=self.subagent_type,
                    agent_id=subagent.identity.coara_id,
                    origin_source=current_turn_source(self._parent),
                )
            )

        # 4./5. 前后台分发（后台入 BackgroundAgentManager / 前台独立 asyncio.Task）
        return await self._dispatch_subagent(
            subagent,
            subagent_id,
            subagent_config,
            tool_whitelist,
            signal,
            description=self.description,
            resumed=False,
        )

    async def _execute_message(self, signal=None) -> ToolResult:
        """Send a mid-run message to a running subagent (主→子 channel).

        The message lands in the subagent's continuation queue and is consumed
        at its next iteration boundary. Stop intent is cooperative by prompt
        convention; hard cancel stays with ``delegate(action="stop")``.

        已收官/被打断的子智能体不在本动作范围内：那属于「追加任务」，请显式用
        ``resume``——本动作绝不静默替换成续跑（会多花一轮 LLM，且与调用方意图
        不符），查不到活实例即明确报错。
        """
        from src.core.message_tags import midrun_message

        subagent = lookup_running_subagent(self.task_id) or _ACTIVE_SUBAGENTS.get(self.task_id)
        if subagent is None or not hasattr(subagent, "submit_continuation_input"):
            # 动作不静默替换：message 是「给运行中的它补一句话」，resume 是「追加
            # 任务再跑一轮」——两者代价不同，落空时直接报错让调用方显式选
            return ToolResult.error(
                f"子智能体 {self.task_id} 不在运行中，途中消息未投递。"
                "若它已结束、要追加任务并在原上下文继续，请用 resume。"
            )
        subagent.submit_continuation_input(midrun_message(self.prompt))
        return ToolResult.success(
            content=f"消息已送达 {self.task_id} 的输入队列，将在其下一迭代边界生效。",
            metadata={"task_id": self.task_id, "mode": "message"},
        )

    async def _execute_stop(self, signal=None) -> ToolResult:
        """Hard-stop a running subagent by task_id (foreground or background)."""
        if not self.task_id:
            return ToolResult.error("action=stop 缺少必填参数: task_id")

        subagent = _ACTIVE_SUBAGENTS.get(self.task_id) or lookup_running_subagent(self.task_id)
        if subagent is not None:
            with contextlib.suppress(Exception):
                subagent.interrupt_current_turn(
                    "delegate_stop",
                    interrupt_source="delegate_action_stop",
                    cancel_delegates=False,
                )

        fg_task = _RUNNING_FG_TASKS.get(self.task_id)
        if fg_task is not None and not fg_task.done():
            fg_task.cancel()
            _cleanup_fg_registries(self.task_id)
            self._mark_subagent_killed(self.task_id, "stopped")
            return ToolResult.success(
                content=f"已停止子智能体 {self.task_id}。",
                metadata={"task_id": self.task_id, "scope": "foreground"},
            )

        cancelled = await BackgroundAgentManager().cancel(self.task_id)
        if not cancelled:
            return ToolResult.error(f"未找到运行中的子智能体 '{self.task_id}'。")
        self._mark_subagent_killed(self.task_id, "stopped")
        return ToolResult.success(
            content=f"已停止子智能体 {self.task_id}。",
            metadata={"task_id": self.task_id, "scope": "background"},
        )

    def _mark_subagent_killed(self, task_id: str, reason: str) -> None:
        from src.background.task_store import TaskStatus
        from src.tools.builtin.background.task_support import resolve_task_store

        with contextlib.suppress(Exception):
            resolve_task_store().update(
                task_id,
                status=TaskStatus.KILLED.value,
                error=reason,
                interrupted=True,
            )

    async def _execute_wait(self, signal=None) -> ToolResult:
        """Block until ALL running foreground subagents complete (or one, when task_id given).

        只等事件不搬结果：完成回调（on_foreground_delegate_done）已把结果注入
        接续队列，下一迭代自然入历史。wait 醒在回调之后，返回完成花名册即可，
        不存在双份送达的竞态。用户接续输入会提前唤醒（事件由输入与完成回调共享）；
        每次 wait 都阻塞到真实事件，不存在空转。
        """
        if self._parent is None:
            return ToolResult.error("Delegate wait 需要父 Coara 上下文")
        from src.coara.turn_completion import _await_interruptible

        parent = self._parent
        pending = {tid: t for tid, t in parent._pending_foreground_delegates.items() if not t.done()}
        if self.task_id:
            task = pending.get(self.task_id)
            if task is None:
                return ToolResult.error(f"{self.task_id} 不在在跑的前台子智能体中（可能已完成，结果已在输入队列）")
            pending = {self.task_id: task}
        if not pending:
            return ToolResult.success(content="当前没有在跑的前台子智能体")

        # 先清事件再挂起：clear 之后到达的完成回调/用户输入会重新置位。
        # ALL_COMPLETED 语义：聚合任务等齐快照里的全部子智能体；
        # 与唤醒事件之间用 FIRST_COMPLETED——只有用户新输入才提前唤醒。
        parent._continuation_event.clear()
        # 输入先于 wait 到达（上次 drain 后、LLM 生成期间进队）时事件已被清掉，
        # 空等会让它拖到子智能体收官才被处理——视同被提前唤醒，直接走早退路径
        pre_queued = bool(parent._continuation_inputs)
        all_done = asyncio.ensure_future(asyncio.gather(*pending.values(), return_exceptions=True))
        wake_task = asyncio.create_task(parent._continuation_event.wait())
        try:
            if not pre_queued:
                if signal is not None:
                    await _await_interruptible(
                        asyncio.wait({all_done, wake_task}, return_when=asyncio.FIRST_COMPLETED),
                        signal,
                    )
                else:
                    await asyncio.wait({all_done, wake_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wake_task.cancel()
            # all_done 不取消：gather 取消会传播给子任务，提前唤醒必须放过在跑的子智能体

        done_lines: list[str] = []
        running_ids: list[str] = []
        for tid, task in pending.items():
            if task.done():
                desc = parent._foreground_delegate_descriptions.get(tid, "")
                status = "已取消" if task.cancelled() else "已完成"
                done_lines.append(f"- [{tid}] {desc}：{status}")
            else:
                running_ids.append(f"[{tid}]")
        if running_ids:
            # 被用户新输入提前唤醒：先处理新输入，仍需结果再 wait
            done_text = "\n".join(done_lines) if done_lines else "（暂无完成）"
            return ToolResult.success(
                content=(
                    f"被新的接续输入提前唤醒，先处理新输入。仍在运行：{'、'.join(running_ids)}\n"
                    f"已完成：\n{done_text}\n（结果已送达，下一步自然看到；仍需等待时再调 wait）"
                )
            )
        lines = done_lines
        return ToolResult.success(content="全部前台子智能体已收官（结果已送达，下一步自然看到）：\n" + "\n".join(lines))

    async def _execute_resume(self, signal=None) -> ToolResult:
        """Resume a cancelled/failed subagent from its persisted breakpoint.

        Loads the SubagentRecord (full message_history + type + mode), rebuilds
        the CoaraBase, sanitizes the interrupted tool tail, and re-runs without
        re-sending the original prompt — the history already carries it.
        """
        if self._parent is None:
            return ToolResult.error("Delegate resume 需要父 Coara 上下文")
        workspace_dir, workspace_error = self._resolve_workspace_dir()
        if workspace_error:
            return ToolResult.error(workspace_error)
        store = self._subagent_store_for_dir(Path(workspace_dir))
        record = store.load(self.task_id)
        if record is None:
            return ToolResult.error(f"找不到子智能体断点 {self.task_id}（可能已清理或从未持久化）")
        # Interrupted runs resume the breakpoint; idle (completed) runs accept an
        # appended task on top of their persisted context (audit→fix reuse).
        if record.status not in (
            SubagentStatus.CANCELLED.value,
            SubagentStatus.FAILED.value,
            SubagentStatus.IDLE.value,
        ):
            return ToolResult.error(
                f"子智能体 {self.task_id} 状态为 {record.status}，只有被打断（cancelled/failed）或已完成（idle，可追加任务）的才能恢复"
            )
        subagent_config = get_subagent(record.subagent_type)
        if not subagent_config:
            return ToolResult.error(f"未知子智能体类型 {record.subagent_type}，无法恢复")
        if record.subagent_type == "janitor":
            return ToolResult.error("janitor 已改为内核维护管道，不再经 delegate 恢复")

        self.subagent_type = record.subagent_type
        self.description = record.description
        self.background = record.mode == "background"

        subagent_id = record.agent_id
        subagent = self._build_subagent(
            subagent_id,
            subagent_config,
            workspace_dir,
            provider_name=getattr(self._parent, "provider_name", None),
            model=getattr(self._parent, "model_name", None),
        )

        # Restore the breakpoint history and close any dangling tool calls left
        # by the interruption (same treatment as a Ctrl+C'd main turn).
        subagent.message_history = SubagentStore.deserialize_message_history(record.message_history)
        from src.coara.workspace_switch_history import finalize_interrupted_turn_history

        finalize_interrupted_turn_history(subagent.message_history)

        # 恢复中断时未消化的接续输入（主会话途中消息等），随本次运行重新入队
        for pending_input in record.pending_inputs or []:
            subagent.submit_continuation_input(pending_input)

        tool_whitelist = self._wire_subagent(subagent, subagent_config)

        # Mark running again (upsert over the cancelled/failed record).
        self._save_running_record(store, subagent, subagent_id, record.description, created_at=record.created_at)

        # Resume continuation: do NOT re-send the original prompt (it is already
        # in history). Inject a resume hint so the model re-orients instead of
        # assuming stale state. Completed runs carry an explicit follow-up task.
        from src.core.message_tags import system_reminder

        if record.status == SubagentStatus.IDLE.value and self.prompt and self.prompt.strip():
            self.prompt = system_reminder(
                "你此前已完成上一阶段任务，完整上下文保留在上方。现在在此基础上追加新任务，"
                "直接复用你已掌握的文件位置与结论，不要重新摸底：\n\n" + self.prompt
            )
        else:
            self.prompt = system_reminder(
                "你从断点恢复继续这个任务。上方是你被打断前的完整上下文。"
                "环境（文件/工作区/其他任务）在你中断期间可能已变化，关键信息必要时先重新确认再继续，不要假设中断前的状态仍然有效。"
            )

        return await self._dispatch_subagent(
            subagent,
            subagent_id,
            subagent_config,
            tool_whitelist,
            signal,
            description=record.description,
            resumed=True,
        )

    async def _run_subagent(
        self,
        subagent: CoaraBase,
        subagent_id: str,
        subagent_config: Any,
        tool_whitelist: set[str],
        signal=None,
    ) -> ToolResult:
        """运行子代理到完成，返回结果。供阻塞模式和后台模式共用。"""
        self._auto_resume_persist_subagent = None
        # 如果父代理提供了取消信号，注册监听器把父级取消传播给子代理
        listener = None
        if signal is not None:

            def _propagate_cancel():
                subagent.interrupt_current_turn(
                    "parent_cancelled",
                    interrupt_source="delegate_parent_cancelled",
                    cancel_delegates=False,
                )

            listener = _propagate_cancel
            signal.add_listener(listener)

        final_status = SubagentStatus.FAILED
        error_msg: str | None = None
        result_preview = ""
        _ACTIVE_SUBAGENTS[subagent_id] = subagent
        # 迭代边界增量断点：每个工具批次结束（流式 chunk）后把中间进展落盘，
        # 进程被杀也能从最近进展 resume，而不是丢回任务开头
        checkpointer = SubagentCheckpointer(
            self._subagent_store_for_dir(Path(subagent.workspace_dir)),
            agent_id=subagent_id,
            subagent_type=self.subagent_type,
            description=self.description,
            background=self.background,
            parent_coara_id=self._parent.identity.coara_id if self._parent else None,
        )
        if self._channel_enabled():
            _RUNNING_SUBAGENTS[subagent_id] = subagent
        try:
            # 初始化子代理工具与能力
            await self._bootstrap_subagent(subagent, subagent_config, tool_whitelist)

            # 注入任务指令并执行
            self._emit_subagent_lifecycle_event(
                "subagent_start",
                f"Subagent started: {self.subagent_type}",
                subagent_id=subagent_id,
                child_session_id=subagent.session_id,
                child_coara_id=subagent.identity.coara_id,
            )
            if not self.background:
                logger.info(
                    f"Foreground delegate [{self.subagent_type}] running "
                    f"(max {SUBAGENT_MAX_TOOL_ITERATIONS} tool rounds; Ctrl+C to cancel): "
                    f"{self.description}"
                )
            last_chunk = ""
            # 系统静默子智能体不产出工具摘要/改动 diff（turn_orchestrator 渲染入口关闭）
            silent_turn = self.subagent_type in CLI_SILENT_SUBAGENT_TYPES
            run_prompt = self.prompt
            if self.action == "spawn" and not self.system_dispatch:
                # 主会话派发的初始任务统一用 <任务指令> 包裹（中途来信另有 <途中消息>）
                from src.core.message_tags import task_instruction

                run_prompt = task_instruction(run_prompt)
                # 落盘主对话 web 视图：这条指令实时只走 trace user_message 广播，
                # 不进 web_views 视图文件——刷新后气泡丢失、被后续用户输入挤占位置。
                # 与 /compact 同款独立 turn_start + user_message 帧，让刷新回放把
                # 指令气泡建回正确时间线位置（紧跟发起它的回合之后）。
                self._persist_delegate_task_view(run_prompt)
            elif self.action == "resume" and not self.system_dispatch:
                # resume 行的折叠区与 spawn 行同口径：也带一份原始任务指令。
                # resume 不重发指令（断点历史里已有），只能回溯取；本次 resume
                # 自身的提示语是系统提醒，不是任务指令，不作为 brief。
                self._persist_resume_brief_view(subagent, subagent_id)
            from src.coara.turn_source import current_turn_source

            async for chunk in subagent.process_message(
                run_prompt,
                show_tool_summary=not silent_turn,
                # 继承父回合发起端：子智能体 tool_* / 活动树按端过滤依赖 payload.source
                source=current_turn_source(self._parent),
            ):
                if isinstance(chunk, str):
                    last_chunk = chunk
                checkpointer.maybe_write(subagent)

            final_result = getattr(subagent, "_final_deliver_message", None) or self._extract_final_response(
                subagent, fallback=last_chunk
            )
            result_preview = final_result
            # 最终答复折叠回 web 的 delegate 工具行（只 web，不落带）。
            self._route_subagent_result(subagent, final_result)
            if subagent.status == CoaraStatus.FAILED:
                error_msg = final_result or "SubAgent execution failed"
                # 瞬时失败且未自动续跑过：从最近 checkpoint 自动续跑一次
                if self._should_auto_resume(subagent, error_msg):
                    result = await self._auto_resume_from_checkpoint(
                        subagent, subagent_id, subagent_config, tool_whitelist, signal, checkpointer, error_msg
                    )
                    # finally 默认 final_status=FAILED；续跑成功已写 IDLE，须同步
                    # 状态以免 finally 把 store 覆盖回 FAILED。
                    if not result.is_error:
                        final_status = SubagentStatus.IDLE
                        result_preview = str(result.content or "")
                        error_msg = None
                    else:
                        result_preview = str(result.content or "")
                        error_msg = result_preview or error_msg
                    return result
                self._emit_subagent_lifecycle_event(
                    "subagent_failed",
                    f"Subagent failed: {self.subagent_type}",
                    subagent_id=subagent_id,
                    child_session_id=subagent.session_id,
                    error=error_msg,
                )
                logger.warning(f"Delegate [{self.subagent_type}] failed: {self.description} -> {final_result}")
                return ToolResult.error(
                    self._dead_error_text(subagent_id, subagent, error_msg),
                    metadata={
                        "task_id": subagent_id,
                        "subagent_id": subagent_id,
                        "subagent_type": self.subagent_type,
                        "description": self.description,
                        "outcome": "failed",
                    },
                )

            self._emit_subagent_lifecycle_event(
                "subagent_complete",
                f"Subagent completed: {self.subagent_type}",
                subagent_id=subagent_id,
                child_session_id=subagent.session_id,
            )
            logger.debug(f"Delegate [{self.subagent_type}] completed: {self.description} ({len(final_result)} chars)")
            final_status = SubagentStatus.IDLE

            return ToolResult.success(
                content=final_result,
                metadata={
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "subagent_id": subagent_id,
                    "task_id": subagent_id,
                    "child_session_id": subagent.session_id,
                    "outcome": "ok",
                    # 段归属：delegate 调用时父会话当前注入段的来源 + matrix 房间
                    # （子智能体活动全部固定送该端显示），完成时随 metadata 带给注入端。
                    **_origin_metadata(subagent),
                },
            )
        except asyncio.CancelledError:
            error_msg = "用户取消了操作"
            final_status = SubagentStatus.CANCELLED
            self._emit_subagent_lifecycle_event(
                "subagent_failed",
                f"Subagent cancelled: {self.subagent_type}",
                subagent_id=subagent_id,
                error=error_msg,
            )
            logger.warning(f"Delegate [{self.subagent_type}] cancelled by user")
            # 终态也必须送一次结果帧：它是端上折叠区「最终结果」组的唯一来源，
            # 取消/异常若绕过正常路径，那一组就永远是空的（用户只看到一行摘要）。
            self._route_subagent_result(
                subagent,
                str(getattr(subagent, "_final_deliver_message", "") or "").strip()
                or "（已取消：本次运行被中断，未产出最终报告）",
            )
            return ToolResult.cancelled(
                "子代理执行已被用户取消。",
                metadata={
                    "task_id": subagent_id,
                    "subagent_id": subagent_id,
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "outcome": "cancelled",
                },
            )
        except Exception as exc:
            error_msg = str(exc)
            # 瞬时异常（provider 抖动等）且未自动续跑过：从最近 checkpoint 自动续跑一次
            if self._should_auto_resume(subagent, error_msg, exc=exc):
                result = await self._auto_resume_from_checkpoint(
                    subagent, subagent_id, subagent_config, tool_whitelist, signal, checkpointer, error_msg
                )
                if not result.is_error:
                    final_status = SubagentStatus.IDLE
                    result_preview = str(result.content or "")
                    error_msg = None
                else:
                    result_preview = str(result.content or "")
                    error_msg = result_preview or error_msg
                return result
            self._emit_subagent_lifecycle_event(
                "subagent_failed",
                f"Subagent failed: {self.subagent_type}",
                subagent_id=subagent_id,
                error=error_msg,
            )
            logger.error(f"Delegate [{self.subagent_type}] failed: {exc}")
            # 同上：异常终止也要把终态发给端上（否则折叠区只有摘要、没有结果）。
            self._route_subagent_result(
                subagent,
                str(getattr(subagent, "_final_deliver_message", "") or "").strip()
                or f"（失败：{str(error_msg)[:200]}）",
            )
            return ToolResult.error(
                self._dead_error_text(subagent_id, subagent, f"SubAgent execution failed: {exc}"),
                metadata={
                    "task_id": subagent_id,
                    "subagent_id": subagent_id,
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "outcome": "failed",
                },
            )
        finally:
            _ACTIVE_SUBAGENTS.pop(subagent_id, None)
            _RUNNING_SUBAGENTS.pop(subagent_id, None)
            _RUNNING_FG_TASKS.pop(subagent_id, None)
            _RUNNING_FG_DESCRIPTIONS.pop(subagent_id, None)
            if signal is not None and listener is not None:
                signal.remove_listener(listener)
            # Persist subagent state before shutdown — update existing record if possible
            persisted_subagent = self._auto_resume_persist_subagent or subagent
            try:
                # 硬取消（Ctrl+C）等打断会把悬空 tool_call 封进历史，resume 后
                # provider 400 连环。封存前先跑恢复回合同款自愈（context_prep
                # 里 sanitize_dangling_tool_tail + close_unmatched_tool_calls），
                # 确保落盘的历史是闭合的。
                from src.coara.workspace_switch_history import (
                    close_unmatched_tool_calls,
                    sanitize_dangling_tool_tail,
                )

                sanitize_dangling_tool_tail(persisted_subagent.message_history)
                closed, _ = close_unmatched_tool_calls(persisted_subagent.message_history)
                if closed:
                    logger.info(f"Closed {closed} dangling tool_call(s) before persisting subagent {subagent_id}")
                store = self._subagent_store_for_dir(Path(persisted_subagent.workspace_dir))
                existing = store.load(subagent_id)
                now = now_iso()
                common_fields = {
                    "agent_id": subagent_id,
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "message_history": SubagentStore.serialize_message_history(persisted_subagent.message_history),
                    "status": final_status.value,
                    "updated_at": now,
                    "session_id": persisted_subagent.session_id,
                    "child_coara_id": persisted_subagent.identity.coara_id,
                    "result_preview": result_preview[:2000] if result_preview else "",
                    "error": error_msg,
                    # 中断时把未消化的接续输入（主会话途中消息等）一并封存，resume 时恢复。
                    # 封存只保留文本（pending_inputs 为 list[str]）；子智能体途中消息均为文本。
                    "pending_inputs": (
                        [ci.text for ci in persisted_subagent.drain_continuation_inputs()]
                        if final_status in (SubagentStatus.CANCELLED, SubagentStatus.FAILED)
                        else []
                    ),
                }
                if existing:
                    record = SubagentRecord(
                        **common_fields,  # type: ignore[arg-type]  # 公共字段是联合类型字典，字段名与类型由赋值保证
                        created_at=existing.created_at,
                        parent_coara_id=existing.parent_coara_id,
                    )
                else:
                    record = SubagentRecord(
                        **common_fields,  # type: ignore[arg-type]  # 同上
                        created_at=now,
                        parent_coara_id=self._parent.identity.coara_id if self._parent else None,
                    )
                store.save(record)
            except Exception as store_exc:
                logger.warning(f"Failed to persist subagent state for {subagent_id}: {store_exc}")
            # TaskStore status update for background delegates is handled by
            # BackgroundAgentManager's completion callback; foreground delegates
            # are never written to TaskStore. Nothing to do here.
            await persisted_subagent.shutdown()
            if persisted_subagent is not subagent:
                await subagent.shutdown()

    def _should_auto_resume(
        self,
        subagent: CoaraBase,
        error_msg: str,
        *,
        exc: BaseException | None = None,
    ) -> bool:
        """判定失败后是否自动从 checkpoint 续跑一次。

        三个硬条件：① 本次任务尚未自动续跑过（防循环）；② 失败性质为瞬时
        （provider 抖动/空响应/超时/可重试 HTTP）；③ 非用户取消（CANCELLED
        分支在 except CancelledError 提前 return，不会走到这里）。
        """
        if self._auto_resumed_once:
            return False
        # 优先用记录在子智能体上的原始 LLM 异常（FAILED 分支由
        # turn_orchestrator 写入 _turn_failure），其次用传入的 exc，
        # 都没有才退回错误文案兜底。
        failure_exc = exc if exc is not None else getattr(subagent, "_turn_failure", None)
        return classify_subagent_failure(failure_exc, error_msg) == "transient"

    def _dead_error_text(self, subagent_id: str, subagent: CoaraBase, error_msg: str) -> str:
        """判死后返回父 LLM 的错误文案：附断点续跑引导或确定性失败说明。"""
        failure_exc = getattr(subagent, "_turn_failure", None)
        kind = classify_subagent_failure(failure_exc, error_msg)
        if kind == "transient":
            return (
                f"{error_msg}\n\n"
                f'（断点已封存，可用 delegate(action="resume", task_id="{subagent_id}") 续跑）'
            )
        # 配额/额度：同模型自动续无意义，但断点仍在——换模型或额度恢复后可 resume
        lowered = f"{error_msg} {failure_exc or ''}".lower()
        if any(
            k in lowered
            for k in (
                "usage limit",
                "quota",
                "access_terminated",
                "5-hour",
                "余额不足",
                "欠费",
                "额度",
                "402",
            )
        ):
            return (
                f"{error_msg}\n\n"
                f"（当前模型额度不可用，同模型自动续跑无意义；断点已封存为 {subagent_id}。"
                f'换模型或额度恢复后可用 delegate(action="resume", task_id="{subagent_id}") '
                f"从断点接着跑，不必整批重派摸底）"
            )
        return f"{error_msg}\n\n（确定性失败，resume 无意义，需换方法或检查参数/权限/配额）"

    async def _auto_resume_from_checkpoint(
        self,
        failed_subagent: CoaraBase,
        subagent_id: str,
        subagent_config: Any,
        tool_whitelist: set[str],
        signal,
        checkpointer: SubagentCheckpointer,
        first_error: str,
    ) -> ToolResult:
        """瞬时失败后从最近 checkpoint 自动续跑一次（复用 resume 恢复链路）。

        与 ``_execute_resume`` 同一套恢复动作：重建子智能体 → 反序列化断点历史
        → 闭合悬空 tool_call → 恢复未消化接续输入 → 注入续跑提示 → 重跑。
        只自动续一次（``_auto_resumed_once`` 防循环）；再失败走正常判死。
        """
        self._auto_resumed_once = True
        self._emit_subagent_lifecycle_event(
            "subagent_retrying",
            f"Subagent auto-resuming after transient failure: {self.subagent_type}",
            subagent_id=subagent_id,
            child_session_id=failed_subagent.session_id,
            error=first_error,
        )
        logger.info(
            f"Delegate [{self.subagent_type}] transient failure, auto-resuming from checkpoint: "
            f"{self.description} ({subagent_id}); first error: {first_error[:160]}"
        )
        # 先封存当前（失败）状态，再从断点重建——保证断点是已闭合的最新历史
        store = self._subagent_store_for_dir(Path(failed_subagent.workspace_dir))
        try:
            from src.coara.workspace_switch_history import (
                close_unmatched_tool_calls,
                finalize_interrupted_turn_history,
                sanitize_dangling_tool_tail,
            )

            sanitize_dangling_tool_tail(failed_subagent.message_history)
            close_unmatched_tool_calls(failed_subagent.message_history)
            pending_inputs = [ci.text for ci in failed_subagent.drain_continuation_inputs()]
            await failed_subagent.shutdown()

            # 重建子智能体，沿用父级 provider/model（与 resume 一致）
            new_subagent = self._build_subagent(
                subagent_id,
                subagent_config,
                failed_subagent.workspace_dir,
                provider_name=getattr(self._parent, "provider_name", None),
                model=getattr(self._parent, "model_name", None),
            )
            self._auto_resume_persist_subagent = new_subagent
            new_subagent.message_history = failed_subagent.message_history
            finalize_interrupted_turn_history(new_subagent.message_history)
            for text in pending_inputs:
                new_subagent.submit_continuation_input(text)
            self._wire_subagent(new_subagent, subagent_config)
            await self._bootstrap_subagent(new_subagent, subagent_config, tool_whitelist)

            # 登记为运行中（覆盖断点记录），发重试开始事件
            _ACTIVE_SUBAGENTS[subagent_id] = new_subagent
            if self._channel_enabled():
                _RUNNING_SUBAGENTS[subagent_id] = new_subagent
            self._save_running_record(
                store, new_subagent, subagent_id, self.description, created_at=now_iso()
            )
            checkpointer._agent_id = subagent_id  # 复用同一 checkpointer 续写断点

            from src.core.message_tags import system_reminder

            resume_prompt = system_reminder(
                "你从断点自动恢复继续这个任务（此前因模型/网络瞬时抖动中断）。上方是你中断前的完整上下文。"
                "环境在你中断期间可能已变化，关键信息必要时先重新确认再继续，不要假设中断前的状态仍然有效。"
            )
            from src.coara.turn_source import current_turn_source

            silent_turn = self.subagent_type in CLI_SILENT_SUBAGENT_TYPES
            last_chunk = ""
            async for chunk in new_subagent.process_message(
                resume_prompt,
                show_tool_summary=not silent_turn,
                source=current_turn_source(self._parent),
            ):
                if isinstance(chunk, str):
                    last_chunk = chunk
                checkpointer.maybe_write(new_subagent)

            final_result = getattr(new_subagent, "_final_deliver_message", None) or self._extract_final_response(
                new_subagent, fallback=last_chunk
            )
            if new_subagent.status == CoaraStatus.FAILED:
                error_msg = final_result or "SubAgent execution failed"
                self._emit_subagent_lifecycle_event(
                    "subagent_failed",
                    f"Subagent failed after auto-resume: {self.subagent_type}",
                    subagent_id=subagent_id,
                    child_session_id=new_subagent.session_id,
                    error=error_msg,
                )
                logger.warning(
                    f"Delegate [{self.subagent_type}] auto-resume also failed: {self.description} -> {final_result}"
                )
                # 复用 finally 封存路径：更新断点为续跑后的历史
                _ACTIVE_SUBAGENTS.pop(subagent_id, None)
                _RUNNING_SUBAGENTS.pop(subagent_id, None)
                return ToolResult.error(
                    self._dead_error_text(subagent_id, new_subagent, error_msg),
                    metadata={
                        "task_id": subagent_id,
                        "subagent_id": subagent_id,
                        "subagent_type": self.subagent_type,
                        "description": self.description,
                        "outcome": "failed",
                    },
                )

            self._emit_subagent_lifecycle_event(
                "subagent_complete",
                f"Subagent completed after auto-resume: {self.subagent_type}",
                subagent_id=subagent_id,
                child_session_id=new_subagent.session_id,
            )
            logger.info(f"Delegate [{self.subagent_type}] auto-resume succeeded: {self.description}")
            _ACTIVE_SUBAGENTS.pop(subagent_id, None)
            _RUNNING_SUBAGENTS.pop(subagent_id, None)
            # 更新断点记录为完成态
            try:
                record = store.load(subagent_id)
                if record:
                    store.save(
                        SubagentRecord(
                            **{**record.__dict__, "status": SubagentStatus.IDLE.value, "updated_at": now_iso(),
                               "result_preview": final_result[:2000], "error": None}
                        )
                    )
            except Exception:  # noqa: BLE001
                logger.debug("auto-resume: update record to idle failed", exc_info=True)
            return ToolResult.success(
                content=final_result,
                metadata={
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "subagent_id": subagent_id,
                    "task_id": subagent_id,
                    "child_session_id": new_subagent.session_id,
                    "auto_resumed": True,
                    "outcome": "ok",
                    **_origin_metadata(new_subagent),
                },
            )
        except asyncio.CancelledError:
            raise  # 用户取消：原样上抛，走外层 CancelledError 分支
        except Exception as resume_exc:  # noqa: BLE001
            logger.error(f"Delegate auto-resume failed: {resume_exc}")
            _ACTIVE_SUBAGENTS.pop(subagent_id, None)
            _RUNNING_SUBAGENTS.pop(subagent_id, None)
            return ToolResult.error(
                self._dead_error_text(subagent_id, failed_subagent, f"{first_error}（自动续跑也失败：{resume_exc}）"),
                metadata={
                    "task_id": subagent_id,
                    "subagent_id": subagent_id,
                    "subagent_type": self.subagent_type,
                    "description": self.description,
                    "outcome": "failed",
                },
            )

    async def _bootstrap_subagent(
        self,
        subagent: CoaraBase,
        subagent_config: Any,
        tool_whitelist: set[str],
    ) -> None:
        if self._channel_enabled():
            # coaras include=["*"] resolves from parent's visible tools, which
            # lack ``interact`` — allow it explicitly before set_whitelist.
            tool_whitelist = tool_whitelist | {"interact"}
        await subagent.bootstrap_tools(with_skill_tool=False)

        if self._channel_enabled():
            # Sub→主 explicit channel: register the ``interact`` tool（直接绑定父会话，
            # 无需全局注册表查找）。
            from src.tools.builtin.communication.interact import InteractTool

            subagent.register_tool(InteractTool(parent_coara=subagent, parent_session=self._parent))

        if self.subagent_type == "daily" and self._parent is not None:
            store = getattr(self._parent, "records_store", None)
            if store is not None:
                from src.tools.builtin.records.local_search import LocalSearchTool
                from src.tools.builtin.records.record import RecordTool

                subagent.register_tool(
                    RecordTool(store=store, parent_coara=subagent),
                    replace=True,
                )
                subagent.register_tool(
                    LocalSearchTool(store=store, parent_coara=subagent, defer=False),
                    replace=True,
                )

        # 子智能体无技能模块：需要技能时主会话把技能内容写进任务指令（见 coaras.md）
        await subagent.initialize()

    def _normalize_tool_whitelist(self, tool_names: list[str] | None) -> set[str]:
        if not tool_names:
            return set()
        return set(tool_names)

    def _resolve_tool_whitelist(self, tool_names: list[str] | None) -> set[str]:
        tool_whitelist = self._normalize_tool_whitelist(tool_names)
        if not tool_whitelist and self._parent is not None:
            parent_bound = self._parent._tool_manager.get_bound_tool_names()
            if parent_bound:
                tool_whitelist = set(parent_bound)
            else:
                visible = self._parent._tool_manager.get_visible_tool_names(self._parent.identity.is_owner_context)
                tool_whitelist = set(visible)
            # 子智能体不做二级装载（deferred/tool 工具）：
            # 挂起机制是主会话的上下文治理手段；子代理 spawn 时工具面一次定死，
            # 继承父白名单时排除装载器与未揭示的挂起工具，防未来注册逻辑漂移。
            parent_tm = self._parent._tool_manager
            tool_whitelist.discard("tool")
            for deferred_name in getattr(parent_tm, "_deferred", set()) or set():
                if deferred_name not in getattr(parent_tm, "_revealed", set()) or set():
                    tool_whitelist.discard(deferred_name)
        if (
            self._parent is not None
            and getattr(self._parent, "is_plan_mode", False)
            # 系统维护管家（janitor/daily，SYSTEM_ONLY）是写 record/ws.md/inbox 的
            # 维护者，不是来改用户代码的——plan_mode 的只读契约针对 计划阶段不动工作
            # 空间代码，不该锁死它们。否则 janitor 在计划模式下派发会被砍成
            # read/grep/glob，想 record 却调不到写工具，陷 412 轮空转退不出（烧 token）。
            and self.subagent_type not in SYSTEM_ONLY_SUBAGENT_TYPES
        ):
            # Plan mode is a hard read-only contract. Delegated children get a
            # fresh ToolManager with is_plan_mode=False, so neither the
            # executor's plan-file-only write/edit check nor the plan-mode
            # visibility filter would apply to them — enforce it here:
            # intersect with the plan-mode-safe set, then drop the write
            # tools (children must not write the plan file either).
            # The result propagates to grandchildren via the parent_bound
            # branch above.
            safe = set(self._parent._tool_manager._PLAN_MODE_ALLOWED)
            tool_whitelist = (tool_whitelist & safe) - {"write", "edit", "plan_mode"}
            if not tool_whitelist:
                # An empty whitelist means "allow all" downstream — never let
                # the strip produce that under plan mode.
                tool_whitelist = {"read", "grep", "glob"}
        return tool_whitelist

    @staticmethod
    def _original_task_instruction(subagent: CoaraBase) -> str:
        """从子智能体历史里取回原始任务指令（resume 行折叠区的 brief 内容）。

        resume 不重发指令（断点历史里已有），要显示同一份指令只能回溯取第一条
        以 ``<任务指令>`` 开头的消息。取不到返回空串——调用方跳过并记 warning，
        不造空帧。
        """
        from src.core.message_tags import TASK_INSTRUCTION_OPEN

        for message in list(getattr(subagent, "message_history", None) or []):
            content = getattr(message, "content", "")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            text = str(content or "")
            if text.lstrip().startswith(TASK_INSTRUCTION_OPEN):
                return text
        return ""

    def _persist_resume_brief_view(self, subagent: CoaraBase, subagent_id: str) -> None:
        """resume 行也落一条 brief 帧（内容＝该子智能体的原始任务指令）。

        与 spawn 行同口径：``parent_tool_call_id`` 是**本次 resume 调用**的
        tool_call_id（`_build_subagent` 已把同一 id 写进子智能体的
        ``_delegate_parent_tool_call_id``），所以同一子智能体被多次 resume 时，
        每条 resume 行各自带同一份指令、互不覆盖。取不到指令就跳过（记 warning）。

        落盘之外还要发一条实时帧：resume 不走 spawn 那条「任务指令 trace」路径
        （它发的是系统提醒，不是指令），只落盘会让折叠区在刷新前后不一致。
        """
        instruction = self._original_task_instruction(subagent)
        if not instruction:
            logger.warning(
                f"delegate resume: 未在子智能体 {subagent_id} 历史里找到 <任务指令>，跳过 brief 落盘"
                f"（call_id={getattr(self, 'tool_call_id', '')}）"
            )
            return
        self._persist_delegate_task_view(instruction)
        self._emit_resume_brief_trace(subagent, instruction)

    def _emit_resume_brief_trace(self, subagent: CoaraBase, instruction: str) -> None:
        """resume 的 brief 实时帧（与 spawn 的 trace user_message 同形状同字段）。

        由子智能体发出（``origin_scope=subagent_loop`` + 子会话 id），端上按
        ``parent_tool_call_id`` 折进本次 resume 的工具行——不新建 role=user 气泡。
        """
        from src.coara.base import _subagent_origin_source

        parent_tool_call_id = str(getattr(self, "tool_call_id", "") or "")
        if not parent_tool_call_id:
            return
        try:
            subagent._emit_trace(
                "user_message",
                instruction[:200],
                payload={
                    "content": instruction,
                    "source": _subagent_origin_source(subagent),
                    "turn_id": "",
                    "delegate_brief": True,
                    "parent_tool_call_id": parent_tool_call_id,
                },
            )
        except Exception:  # noqa: BLE001 — 实时帧失败不影响 resume 执行
            logger.debug("emit resume brief trace failed", exc_info=True)

    def _persist_delegate_task_view(self, run_prompt: str) -> None:
        """把 delegate 任务指令作为主对话 web 视图帧落盘（刷新后可回放）。

        实时：trace ``user_message``（子智能体 process_message 发出）广播到浏览器，
        带 ``delegate_brief`` 标记时端上折进发起那条 delegate 工具行。它从不进
        web_views 视图文件，刷新后 hydrate 重建不出这条——此处补落盘为父会话当前
        活跃回合的一条 user_message 跟话帧，让刷新回放与实时一致。

        调用方：spawn 传本次 ``<任务指令>`` 包裹文本；resume 传从断点历史取回的
        原始指令（``_persist_resume_brief_view``，与实时 trace 内容不同——resume
        的实时帧是系统提醒不是指令，故这条只服务折叠区）。

        只在 web 端有活跃视图时落盘；任何失败只记日志，绝不阻断派发。
        来源端门控：三端视图文件按端隔离——CLI/matrix 发起的 delegate 指令
        不写 web 视图（否则刷新后会把非 web 的指令渲染成假 web 输入气泡）。
        只落 web 源回合，且写入真实来源（不再硬编码 "web"）。
        """
        if self._parent is None:
            return
        if self.background:
            return
        # 门控：非 web 来源回合（cli-attached / matrix / event / background）
        # 的委派指令不落 web 视图。read 端另有按 source 过滤兜底。
        from src.coara.turn_source import current_turn_source, web_shows_source

        seg_source = current_turn_source(self._parent)
        if not web_shows_source(seg_source, unknown=False):
            return
        try:
            # 存完整 <任务指令> 包裹文本（与实时 trace user_message 的 content
            # 逐字一致——同一 run_prompt，不 strip）：前端 hydrate 的 liveTail
            # 文本去重据此命中，刷新时丢弃实时残留版、落盘版在原位重建。
            body = run_prompt
            if not body or not body.strip():
                return
            root = getattr(self._parent, "_root_ref", None)
            web_server = getattr(root, "_web_server", None) if root is not None else None
            view_store = getattr(web_server, "_view_store", None) if web_server is not None else None
            coara_home = getattr(web_server, "coara_home", None) if web_server is not None else None
            if view_store is None:
                return
            from src.ui.web_views import resolve_web_view_path

            sess_id = str(getattr(self._parent, "session_id", "") or "")
            # 归属父会话当前活跃回合：作为该回合的一条 user_message 跟话帧，
            # 按 view_seq 与父回合的 chunk 交织——与实时 append 在「当时消息流
            # 末尾」的位置一致。建独立 turn 会被 build_messages 按 turn 分组排
            # 到父回合全部内容之后，位置反而错。
            turn_id = str(getattr(getattr(self._parent, "_active_turn", None), "turn_id", "") or "")
            if not sess_id or not turn_id:
                return
            path = resolve_web_view_path(
                self._parent.workspace_dir, coara_home=coara_home, subject="root", session_id=sess_id
            )
            view_store.append_event(
                path,
                kind="user_message",
                turn_id=turn_id,
                source=seg_source,
                subject="root",
                session_id=sess_id,
                payload={
                    "content": body,
                    # delegate_task：历史标记（前端曾据它区分指令气泡）
                    "delegate_task": True,
                    # delegate_brief + 父行 call_id：指令不进主会话正文流，
                    # 端上折进发起它的那条 delegate 工具行的展开区。与实时
                    # trace user_message（base.process_message）同源同字段。
                    "delegate_brief": True,
                    "parent_tool_call_id": str(getattr(self, "tool_call_id", "") or ""),
                },
            )
        except Exception:  # noqa: BLE001 — 视图落盘故障不影响派发
            # 兜底不阻断，但落带失败会让刷新回放少一条指令帧——记 WARNING 可查
            logger.warning("persist delegate task view failed", exc_info=True)

    def _route_subagent_result(self, subagent: CoaraBase, text: str) -> None:
        """子智能体的最终答复：带父标识投给**发起端**，折叠在它的 delegate 工具行里。

        命中即随该回合的 TurnStream 落视图带（kind=subagent_result，带
        tool_call_id + view_seq），刷新后由快照的 ``subagent_results`` 映射回到
        折叠输出；端上此刻无任何可命中通道（浏览器没开、回合已收尾）时才就地
        补落一份，帧不丢。两条路互斥——命中即不补落，绝不制造第二落点。

        端寻址按 ``_subagent_origin_source``（谁发起的 delegate，就投给谁）——
        web 落视图带、matrix 进房间、attach 走连接，各端自行按父标识折进那条行。
        """
        body = str(text or "").strip()
        parent = self._parent
        if not body or parent is None:
            return
        from src.coara.base import _subagent_origin_source

        source = _subagent_origin_source(subagent)
        if not source:
            return
        root = getattr(parent, "_root_ref", None)
        registry = getattr(root, "end_registry", None) if root is not None else None
        if registry is None:
            return
        session_id = str(getattr(parent, "session_id", "") or "")
        frame = {
            "kind": "subagent_result",
            "text": body,
            # tool_call_id 给 CLI 折叠块认父；parent_tool_call_id 给 matrix/web 通道
            # 判定「这是子智能体帧，折进父行」——缺了它结果帧会退化成普通正文发进房间，
            # 手机端就会把子智能体的最终报告当主会话消息铺在对话流里
            "tool_call_id": str(getattr(self, "tool_call_id", "") or ""),
            "parent_tool_call_id": str(getattr(self, "tool_call_id", "") or ""),
            "coara_id": str(getattr(getattr(subagent, "identity", None), "coara_id", "") or ""),
            "session_id": session_id,
            "workspace_dir": str(getattr(parent, "workspace_dir", "") or ""),
        }
        origin = getattr(subagent, "_subagent_origin", None)
        channel_id = str(origin[1] or "") if isinstance(origin, tuple) and len(origin) > 1 else ""
        try:
            outcome = registry.deliver(source, session_id, frame, channel_id=channel_id)
            if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
                asyncio.ensure_future(outcome.value)
            if not outcome.hit:
                self._persist_subagent_result_view(subagent, body)
        except Exception:  # noqa: BLE001 — 展示层失败不影响子智能体结果交付
            # 投递/兜底落带异常：不阻断交付，但会在端上表现为结果看不见——WARNING
            logger.warning("route subagent result failed", exc_info=True)

    def _push_matrix_subagent_result(self, subagent: CoaraBase, body: str) -> None:
        """matrix 无在线通道时的兜底：发子智能体信封，端上折进 delegate 行。

        形态与 sender 通道完全一致（同 kind、同父标识）——兜底只换传输，不换形状，
        端上不会因为走了兜底就把它铺成主会话正文。
        """
        origin = getattr(subagent, "_subagent_origin", None)
        room_id = str(origin[1] or "") if isinstance(origin, tuple) and len(origin) > 1 else ""
        if not room_id:
            return
        try:
            import json as _json

            from src.matrix_client.diff_bridge import push_matrix_text

            payload = {
                "kind": "subagent_result",
                "text": body,
                "parent_tool_call_id": str(getattr(self, "tool_call_id", "") or ""),
            }
            envelope = f"[COARA_SUBAGENT]{_json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
            asyncio.ensure_future(push_matrix_text(room_id=room_id, body=envelope))
        except Exception:  # noqa: BLE001 — 兜底失败只记日志，不阻断结果交付
            logger.warning("matrix subagent result fallback push failed", exc_info=True)

    def _persist_subagent_result_view(self, subagent: CoaraBase, text: str) -> None:
        """没有端通道时的兜底落地：按发起端各落各的，结果不丢。

        与 ``_route_subagent_result`` 是一对：那条走通道投递，命中即不补落；这里
        只在未命中时执行，绝不制造第二落点。web 落视图带（刷新后由快照的
        ``subagent_results`` 并回折叠），matrix 发子智能体信封（与 sender 同形态）。
        任何失败只记日志，绝不阻断交付。
        """
        body = str(text or "").strip()
        parent = self._parent
        if not body or parent is None:
            return
        from src.coara.base import _subagent_origin_source

        if _subagent_origin_source(subagent) == "matrix":
            self._push_matrix_subagent_result(subagent, body)
            return
        try:
            from src.coara.turn_source import current_turn_source, web_shows_source

            seg_source = current_turn_source(parent)
            if not web_shows_source(seg_source, unknown=False):
                return
            root = getattr(parent, "_root_ref", None)
            web_server = getattr(root, "_web_server", None) if root is not None else None
            view_store = getattr(web_server, "_view_store", None) if web_server is not None else None
            if view_store is None:
                return
            from src.ui.web_views import resolve_web_view_path

            sess_id = str(getattr(parent, "session_id", "") or "")
            if not sess_id:
                return
            turn_id = str(getattr(getattr(parent, "_active_turn", None), "turn_id", "") or "")
            path = resolve_web_view_path(
                parent.workspace_dir,
                coara_home=getattr(web_server, "coara_home", None),
                subject="root",
                session_id=sess_id,
            )
            view_store.append_event(
                path,
                kind="subagent_result",
                turn_id=turn_id,
                source=seg_source or "web",
                subject="root",
                session_id=sess_id,
                payload={
                    "text": body,
                    "tool_call_id": str(getattr(self, "tool_call_id", "") or ""),
                    "coara_id": str(getattr(getattr(subagent, "identity", None), "coara_id", "") or ""),
                },
            )
        except Exception:  # noqa: BLE001 — 视图落盘故障不影响结果交付
            # 同前：兜底不阻断，静默会让「结果没出现在折叠区」无从定位——WARNING
            logger.warning("persist subagent result view failed", exc_info=True)

    def _emit_subagent_lifecycle_event(
        self,
        event_type: str,
        message: str,
        *,
        subagent_id: str,
        child_session_id: str | None = None,
        child_coara_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._parent is None:
            return

        payload = {
            "subagent_type": self.subagent_type,
            "subagent_id": subagent_id,
            "description": self.description,
            # 发起空间归属：切走视图后的迟到关行事件按「其它空间」丢弃，
            # 不被广播层标 detached 复活已清的活动树行（09-09 审计 P1-3）。
            "workspace_dir": str(getattr(self._parent, "workspace_dir", "") or ""),
        }
        parent_tool_call_id = getattr(self, "tool_call_id", None)
        if parent_tool_call_id:
            payload["parent_tool_call_id"] = parent_tool_call_id
            payload["parent_activity_id"] = parent_tool_call_id
        if child_session_id:
            payload["child_session_id"] = child_session_id
        if child_coara_id:
            payload["child_coara_id"] = child_coara_id
        if error:
            payload["error"] = error
        self._parent._emit_trace(event_type, message, payload=payload)

    @staticmethod
    def _extract_final_response(subagent: CoaraBase, *, fallback: str = "") -> str:
        last_user_index = max(
            (index for index, message in enumerate(subagent.message_history) if message.role == MessageRole.USER),
            default=-1,
        )
        messages_after_prompt = subagent.message_history[last_user_index + 1 :]

        for message in reversed(messages_after_prompt):
            if message.role != MessageRole.ASSISTANT:
                continue
            if message.tool_calls:
                continue
            if not isinstance(message.content, str):
                continue
            if not message.content.strip():
                continue
            return message.content
        return fallback
