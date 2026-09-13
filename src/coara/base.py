"""Base coara implementation."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent.executor import ToolExecutor
from src.agent.hooks import (
    AccessPolicyHook,
    CompactHookRunner,
    LoopDetectionHook,
    ShellFailureStreakBeforeHook,
    ShellFailureStreakPostHook,
    ToolHookRunner,
)
from src.agent.loop import LoopDetector, ShellFailureStreakGuard
from src.agent.output_truncation import settings_from_config
from src.coara.display import format_tool_call_label
from src.coara.injections import wrap_tool_result
from src.coara.llmlog import (
    DAILY_WORKSPACE_KEY,
    FLOW_WORKSPACE_KEY,
    Meta,
    build_agent_overview,
    log_llm_call,
)
from src.coara.tool_manager import ToolManager
from src.coara.trace_emitter import TraceEmitter, serialize_messages_for_trace
from src.coara.turn_completion import CoaraRunCancelledError
from src.coara.turn_phase import TurnPhase
from src.coara.turn_timing import TurnTimingRecorder
from src.context.window import LlmUsageSnapshot, context_window_manager, payload_fingerprint_cached
from src.core.abort import AbortController
from src.core.coara_home import resolve_coara_home
from src.core.logger import logger
from src.core.message_tags import TASK_INSTRUCTION_OPEN
from src.core.time import now_iso
from src.core.tool_base import BaseTool, ToolResult
from src.core.types import (
    CoaraIdentity,
    CoaraPersona,
    CoaraStatus,
    ContinuationInput,
    Message,
    MessageRole,
    SkillDefinition,
)
from src.llm.profiles import Profile
from src.llm.provider import LLMProvider, LLMResponse
from src.llm.request import LLMRequest
from src.llm.service import llm_service
from src.prompt.builder import PromptBuilder
from src.skills.manager import SkillManager
from src.skills.session import SkillSessionState
from src.todos.turn_control import TurnController
from src.tools.builtin.file_io.file_support import invalidate_read_caches_for_paths
from src.workspace.llm_binding import workspace_llm_chrome_fields


@dataclass(slots=True)
class TurnRuntime:
    """Runtime state for a single in-flight turn."""

    turn_id: str
    controller: AbortController
    reason: str = "interrupted"
    # 出口软提醒每回合只发一次：提醒后 LLM 仍不收子智能体结果 → 放行
    fg_release_reminded: bool = False

    @property
    def signal(self):
        """Expose the AbortSignal for listener-based propagation."""
        return self.controller.signal


def _subagent_origin_source(coara: Any) -> str:
    """子智能体的派发端来源（dispatch 快照优先，回退当前回合来源）。

    子智能体产出的帧只服务**派发它的那一端**（web 端折叠区）：非 web 派发的帧
    硬投 web 会污染 web 视图带，且在纯 CLI 进程里每一帧刷一条 no-sender WARNING。
    """
    origin = getattr(coara, "_subagent_origin", None)
    if isinstance(origin, tuple) and origin:
        source = str(origin[0] or "")
        if source:
            return source
    return str(getattr(coara, "_active_turn_source", "") or "")


# provider 流式分帧符（如 ]<]minimax[>[ ）：驱动层不认识的线协议标记，
# 一旦漏进正文会原样上屏。非我们源码里的常量，是 provider 服务端下发的帧分隔符。
_PROVIDER_FRAME_MARKER_RE = re.compile(r"\]<][A-Za-z0-9_.-]+\[>\[")
# ReAct 协议残留：模型把 <tool_call>/<invoke> 写进正文（协议退化或模型模仿）。
_REACT_TOOLCALL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
_REACT_INVOKE_BLOCK_RE = re.compile(r"<invoke\b[^>]*>.*?</invoke>", re.DOTALL)


def _sanitize_outbound_text(text: str) -> str:
    """正文下发前的协议残渣净化（三端共用入口：`_route_chunk_to_current_end`）。

    两类残渣：
    - provider 流式分帧符（`]<]minimax[>[` 这类）：剥掉。
    - ReAct 协议块（`<tool_call>…</tool_call>` / `<invoke>…</invoke>`）：剥掉整段
      ——它们是工具协议的线文本，不是给用户看的内容；漏出意味着协议退化或模型
      误把协议写成了正文。

    保守取向：只剥**整块**协议片段与分帧符，不动正文其余文本（模型在正文里
    逐字讨论这些标签的情况不剥——不整块出现不算残渣）。清理后为空返回空串，
    由调用方整条丢弃。
    """
    if not text:
        return ""
    cleaned = _PROVIDER_FRAME_MARKER_RE.sub("", text)
    cleaned = _REACT_TOOLCALL_BLOCK_RE.sub("", cleaned)
    cleaned = _REACT_INVOKE_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


def _delegated_parent_tool_call_id(coara: Any) -> str:
    """发起该子智能体的 delegate 工具行 call_id；不是用户可见子智能体时返回空串。
    与 ``CoaraBase._is_delegated_subagent`` 同一判据（单一来源）：只有 delegate 建
    出来、且不静默的子智能体才有父行——主会话自己的帧一律不盖这个标识。
    """
    if getattr(coara, "_session_agent_kind", "") != "subagent":
        return ""
    if getattr(coara, "_cli_silent", False):
        return ""
    return str(getattr(coara, "_delegate_parent_tool_call_id", "") or "")


def _route_tool_frame(coara: Any, frame: dict[str, Any], *, payload: dict[str, Any], what: str) -> None:
    """工具帧（diff / 工具行）统一路由与两级兜底（与正文 chunk 同款）。

    模块级而非方法：调用方既有真实 CoaraBase，也有测试桩（SimpleNamespace），
    逻辑不得依赖实例上除下列属性之外的任何东西。

    归属 = payload 的 subagent_origin / source（回合注入段）；端通道缺失时
    逐级回退到回合发起端与会话归属端。命中判据取 ``RouteResult.hit``：
    sender 返回 None（同步 void）也是命中。

    background / event 与正文 chunk 同款：有注册通道就投（例如 matrix 发起的
    后台唤醒回合挂了 background→房间 sender），无通道再走兜底；不再在路由层
    一律丢弃，否则手机端唤醒回合永远看不到工具行。

    子智能体产生的帧带 ``parent_tool_call_id``（发起它的 delegate 工具行）：
    落带后不进主会话正文流，端上把它折进那条行。主会话自己的帧无此字段。
    """
    source = (
        str(payload.get("subagent_origin") or "").strip()
        or str(payload.get("source") or "").strip()
        or str(getattr(coara, "_active_turn_source", "") or "")
    )
    if not source:
        return
    root = getattr(coara, "_root_ref", None)
    if root is None:
        # 防御：子智能体 coara 未回填 _root_ref 时经父会话取 root
        parent = getattr(coara, "_subagent_parent", None)
        root = getattr(parent, "_root_ref", None) if parent is not None else None
    registry = getattr(root, "end_registry", None) if root is not None else None
    if registry is None:
        return
    segments = getattr(coara, "_segments", None)
    session_id = (
        coara._route_session_id()
        if hasattr(coara, "_route_session_id")
        else str(getattr(coara, "session_id", "") or "")
    )
    # 帧带归属（会话 / 回合）：一个浏览器连接可能同时挂着多个空间的回合流，
    # 端通道只有靠归属才投得对——这是「哪个空间的消息去哪端」的唯一凭据。
    frame.setdefault("session_id", session_id)
    frame.setdefault("workspace_dir", str(getattr(coara, "workspace_dir", "") or ""))
    turn_id = str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or "")
    if turn_id:
        frame.setdefault("turn_id", turn_id)
    # 子智能体产的帧带父标识：端上按它折进发起那条 delegate 工具行，而不是
    # 当主会话正文/独立 diff 块渲染。
    parent_tool_call_id = _delegated_parent_tool_call_id(coara)
    if parent_tool_call_id:
        frame.setdefault("parent_tool_call_id", parent_tool_call_id)
    # 产生本帧的那次工具调用 id（＝同一工具 tool 帧上的 tool_call_id）：端上据此
    # 把 diff 精确挂到对应工具行之后，不再靠相邻关系猜（中间夹正文/多工具并发时
    # 相邻关系并不可靠）。取值来自 executor 的 tool_complete payload，不猜。
    tool_call_id = str(payload.get("tool_call_id") or "")
    if tool_call_id:
        frame.setdefault("tool_call_id", tool_call_id)
    # 端内连接标识：子智能体自身的段往往是父会话段的快照（channel 可能恒空），
    # 派发时快照的连接才是它能投对的那条——优先 _subagent_origin[1]，回退段
    # channel_id（同空间多条 attach 并存时按连接精确归位）。
    origin = getattr(coara, "_subagent_origin", None)
    origin_channel = str(origin[1] or "") if isinstance(origin, tuple) and len(origin) > 1 else ""
    channel_id = origin_channel or str(getattr(segments, "channel_id", "") or "")
    try:
        outcome = registry.deliver(
            source,
            session_id,
            frame,
            channel_id=channel_id,
        )
        if not outcome.hit:
            # 与正文 chunk 同款两级兜底：归属端无通道 → 回合发起端 → 会话
            # 归属端（session_origin，background 唤醒回合的帧靠这层上屏）。
            session_origin = getattr(coara, "session_origin", None) or {}
            origin_ch = str(session_origin.get("channel_id") or "")
            candidates: list[tuple[str, str]] = []
            launch_source = str(getattr(coara, "_active_turn_source", "") or "")
            if launch_source and launch_source != source:
                candidates.append((launch_source, origin_ch if launch_source == session_origin.get("source") else ""))
            origin_source = str(session_origin.get("source") or "")
            if origin_source and origin_source not in (source, launch_source):
                candidates.append((origin_source, origin_ch))
            for cand_source, cand_ch in candidates:
                outcome = registry.deliver(cand_source, session_id, frame, channel_id=cand_ch)
                if outcome.hit:
                    break
        if not outcome.hit:
            logger.warning(
                f"route {what}: no sender for end '{source}' "
                f"(session={getattr(coara, 'session_id', '')}, tool={payload.get('tool_name', '')})"
            )
        if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
            # fire-and-forget：帧投递不阻塞工具执行（sender 内部 self-throttle）
            asyncio.ensure_future(outcome.value)
    except Exception:
        logger.debug(f"route tool frame ({what}) failed", exc_info=True)


def _route_subagent_tool_frame(coara: Any, payload: dict[str, Any], frame: dict[str, Any]) -> None:
    """子智能体工具帧（工具行 / diff）：带父标识投给**发起端**，端上折进发起它的 delegate 工具行。

    端寻址按 ``_subagent_origin_source``（谁发起的 delegate，就投给谁）：
    web 落视图带、matrix 进房间、attach 走连接——各端自行按 ``parent_tool_call_id``
    折叠，主会话正文流不受刷屏。凡该端没有对应的折叠父行（非发起端），投递自然
    落空（无注册通道），不会凭空多出内容。

    子智能体的**工具行与 diff 都走这条路**（此前只有工具行有，diff 走主路由进
    主列表）——你要的是「子智能体的任务、过程、结果、改动全部进折叠，不铺主会话」。

    模块级而非方法：调用方既有真实 CoaraBase，也有测试桩（SimpleNamespace）——
    与 ``_route_tool_frame`` 同款理由。
    """
    parent_tool_call_id = _delegated_parent_tool_call_id(coara)
    if not parent_tool_call_id:
        return
    source = _subagent_origin_source(coara)
    if not source:
        return
    root = getattr(coara, "_root_ref", None)
    if root is None:
        parent_coara = getattr(coara, "_subagent_parent", None)
        root = getattr(parent_coara, "_root_ref", None) if parent_coara is not None else None
    registry = getattr(root, "end_registry", None) if root is not None else None
    if registry is None:
        return
    parent_session = str(getattr(coara, "_delegate_parent_session_id", "") or "")
    frame.setdefault("parent_tool_call_id", parent_tool_call_id)
    frame.setdefault("session_id", parent_session)
    frame.setdefault("workspace_dir", str(getattr(coara, "_delegate_parent_workspace_dir", "") or ""))
    # 端内连接：子智能体自身的段多是父会话段快照（channel 可能恒空），派发时
    # 快照的连接才是它能投对的那条——与 _route_tool_frame 同款取值。
    origin = getattr(coara, "_subagent_origin", None)
    channel_id = str(origin[1] or "") if isinstance(origin, tuple) and len(origin) > 1 else ""
    try:
        outcome = registry.deliver(source, parent_session, frame, channel_id=channel_id)
        if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
            asyncio.ensure_future(outcome.value)
    except Exception:  # noqa: BLE001 — 子智能体工具帧投递失败不影响它干活
        logger.debug("route subagent tool frame failed", exc_info=True)


def _route_subagent_tool_line(coara: Any, payload: dict[str, Any], label: str) -> None:
    """子智能体工具行：带父标识投给**发起端**，端上折进发起它的 delegate 工具行。

    端寻址按 ``_subagent_origin_source``（谁发起的 delegate，就投给谁）：
    web 落视图带、matrix 进房间、attach 走连接——各端自行按 ``parent_tool_call_id``
    折叠，主会话正文流不受刷屏。凡该端没有对应的折叠父行（非发起端），投递自然
    落空（无注册通道），不会凭空多出内容。

    历史注释「手机走房间消息」在此实现补齐：此前硬编码只投 web，手机端永远收不到
    子智能体工具行，折叠无从谈起。

    模块级而非方法：调用方既有真实 CoaraBase，也有测试桩（SimpleNamespace）——
    与 ``_route_tool_frame`` 同款理由。
    """
    _route_subagent_tool_frame(
        coara,
        payload,
        {
            "kind": "tool",
            "text": label,
            "tool_name": payload.get("tool_name", ""),
            "tool_call_id": payload.get("tool_call_id", ""),
            "is_error": bool(payload.get("is_error", False)),
            "duration_ms": payload.get("duration_ms"),
        },
    )


class CoaraBase:
    """A single coara unit with tools, skills, and an LLM backend."""

    def __init__(
        self,
        name: str,
        persona: CoaraPersona,
        workspace_dir: Path | None = None,
        provider: LLMProvider | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        coara_id: str | None = None,
        created_at: str | None = None,
        user_facing: bool = False,
        is_owner_context: bool = False,
        max_tool_iterations: int | None = None,
        delegate_depth: int = 0,
        audit_session_id: str | None = None,
        session_agent_kind: str | None = None,
        session_tape: str | None = None,
    ):
        # Always Path — callers (esp. FlowRoot workspace sync) may pass str.
        workspace_dir = Path(workspace_dir or Path.cwd()).expanduser().resolve()

        self.identity = CoaraIdentity(
            coara_id=coara_id or str(uuid.uuid4()),
            name=name,
            user_facing=user_facing,
            is_owner_context=is_owner_context,
            persona=persona,
            workspace_dir=workspace_dir,
            created_at=created_at or now_iso(),
        )

        self.status = CoaraStatus.CREATED
        self.message_history: list[Message] = []
        self.session_id = str(uuid.uuid4())
        self.audit_session_id = audit_session_id or self.session_id
        # 会话事件 agent_kind：main / subagent / flow（Flow 第二主体）
        if session_agent_kind:
            self._session_agent_kind = session_agent_kind
        elif user_facing or is_owner_context:
            self._session_agent_kind = "main"
        else:
            self._session_agent_kind = "subagent"
        # 录像带路由标记：flow（工作流系统带）/ engine（引擎系统带）/ 缺省（空间带）
        self._session_tape = session_tape or ""
        self._inside_turn = False
        # /model 回合中执行时的延迟切换标记（回合结束后生效）
        self._pending_llm_switch: tuple[str, str | None] | None = None
        # /compact 单飞锁：防并发压缩覆盖历史
        self._compress_inflight: bool = False
        # 历史改写世代：压缩等整表替换时 +1；回滚用 floor 而非陈旧绝对下标
        self._history_epoch: int = 0
        self._rollback_floor: int | None = None
        # 反向引用：RootCoara 建会话时回填（压缩成功后据此派 janitor 沉淀，
        # 覆盖「用户连续工作不 /new 导致 janitor 不触发」的盲区）
        self._root_ref: Any | None = None
        # 本会话累计 LLM API 调用次数（跨 turn；/new 清会话时归零）
        self._session_llm_call_count = 0
        # 本会话累计工具调用次数（一轮 LLM 可带多个 tool_calls；/new 归零）
        self._session_tool_call_count = 0

        self.llm_profile = Profile.AGENT_MAIN
        if provider is not None:
            self.provider = provider
            self.provider_name = provider_name or provider.name
            self.model_name = model or provider.default_model
            llm_service.register_injected_provider(provider, model=self.model_name)
        else:
            resolved = llm_service.resolve(
                Profile.AGENT_MAIN,
                provider_name=provider_name,
                model=model,
            )
            self.provider = resolved.provider
            self.provider_name = resolved.provider_name
            self.model_name = resolved.model
            self.llm_profile = resolved.profile

        self._tool_manager = ToolManager(owner=self)
        # Per-instance skill pool: workspace-scoped discovery must not leak
        # across CoaraBase instances (a shared global pool lets a later
        # discover() wipe another workspace's skills mid-turn).
        self.skill_manager = SkillManager()
        self._skills: list[SkillDefinition] = []
        self._skill_session = SkillSessionState()
        self.workspace_dir = workspace_dir
        self._file_read_states: dict[str, Any] = {}

        # Back-reference to the embedded WebServer (set by WebServer.__init__
        # when running in web UI mode). Tools use this to push WS navigation
        # messages (e.g. open_workflow_editor) to the browser.
        self._web_server: Any = None

        # 会话内 flow 图协调器：实例级（主体隔离——主会话与 FlowRoot 各持一份）。
        self._flow_coordinator: Any = None

        self._max_tool_iterations = max_tool_iterations
        self.delegate_depth = delegate_depth
        self._static_prompt_cache: dict[str, str] = {}
        # 顶层 tools 定义覆盖（可选）：非 None 时 _get_tool_definitions_for_llm 直接
        # 返回该快照，不走 tool_manager——用于 janitor 之类复用父会话前缀的场景，
        # 保证发送给 provider 的 tools 与父会话逐字节一致（prompt cache 命中前提）。
        self._tool_definitions_override: list[dict[str, Any]] | None = None
        self._llm_usage_snapshot = LlmUsageSnapshot()
        self._active_turn: TurnRuntime | None = None
        # 最近一回合 LLM 失败的原始异常（turn_orchestrator 判死时记录）；
        # delegate 失败分类据此判定瞬时/确定，主会话不消费。None 表示无 LLM 失败。
        self._turn_failure: BaseException | None = None
        # turn 内 start_new_session 的延迟收尾任务（等旧回合退出后清状态）
        self._deferred_new_session_task: asyncio.Task | None = None
        # 迟到子智能体结果等异步落盘任务（持引用防 GC 回收协程，完成即丢弃）
        self._session_persist_tasks: set[asyncio.Task] = set()
        # 回合相位（等 API / 收 API / 跑工具 / 本地处理），/status 与 watchdog 读取
        self._turn_phase = TurnPhase()
        self._recent_progress_signatures: deque[str] = deque(maxlen=200)
        self._current_trust_level: str = "owner"
        self._continuation_inputs: list[ContinuationInput] = []
        # 注入分段追踪：输出路由/渲染/录像带的统一数据源（见 src/coara/segment.py）。
        # 首段在 process_message 入口开启，后续段在 turn loop 迭代头注入 continuation 时开启。
        from src.coara.segment import SegmentTracker

        self._segments = SegmentTracker()
        # 有新接续输入时置位：等待前台子智能体的回合暂停点监听它，
        # 用户输入可以立即唤醒等待并进入下一轮 ReAct 迭代
        self._continuation_event = asyncio.Event()
        # deliver 交付内容（flow 节点）：设置后本回合在工具执行完立即正常结束，
        # 其结果作为节点输出
        self._final_deliver_message: str | None = None
        # todo(action="park") 的一步收尾结束语：park 执行后本回合立即结束并交付，
        # 编排器在工具批次后消费（消费即清空；新回合开头兜底重置，防中断残留）
        self._todo_park_message: str | None = None
        # mid-turn 远端接续输入的 remote 上下文：(room_id, send_text, interaction_channel)。
        # Matrix ingress 在回合忙时 defer 消息进接续队列（不经 turn），
        # turn loop 消费远端输入时用它恢复远端上下文，审批/询问/plan_review 才能走 Matrix
        self._deferred_remote_ctx: tuple | None = None
        # Pending stamp consumed by the next submit_continuation_input (per-item ownership).
        self._pending_deferred_remote_ctx: tuple | None = None
        # turn loop 为远端接续输入恢复 turn 的 ContextVar token 链，回合收尾统一清理
        self._turn_context_tokens: list[tuple] = []
        # Frontend that started the active turn ("cli" / "web" / "matrix" / "").
        # Used for interrupt origin / observability.
        self._active_turn_source: str = ""
        # 最近一次「真实用户输入」来源（cli/web/matrix）。后台唤醒回合的回复
        # 要回投到这个端——规则：输出端（CLI）常显，输入端=上一条用户输入所在端。
        self._last_user_input_source: str = ""
        # 会话归属端：最近一次真实用户输入的来源 + 其远端通道（审批回 origin 用）。
        self.session_origin: dict[str, Any] | None = None
        self._origin_remote_channel: Any = None
        # plan_mode submit 后的「待批准」锁：置位期间 exit/重复 submit 被工具层拒绝，
        # 用户下一条消息（新回合或带来源的接续输入）才清——submit 后 LLM 无法自己放行。
        self._plan_pending_approval: bool = False
        # turn 队列：等待 per-session 串行锁的待处理回合（D5 显式化 FIFO 记账）。
        from src.coara.turn_queue import TurnQueue

        self._turn_queue = TurnQueue()
        # Foreground delegate tracking — async-launched subagents whose
        # results must be collected before the current turn can exit.
        # Keyed by task_id (= subagent_id); value is the asyncio.Task.
        self._pending_foreground_delegates: dict[str, Any] = {}
        # 回合出口放行的前台子智能体：继续跑，完成结果按迟到语义路由
        self._released_foreground_delegates: dict[str, Any] = {}
        self._foreground_delegate_descriptions: dict[str, str] = {}

        # 会话事件溯源（已转正）：回合事件记录器，唯一事实源，无开关。
        # 任何初始化失败都静默降级为 None（不阻塞回合主流程）。
        self._rebuild_session_log()

        self._trace_emitter = TraceEmitter(
            sink=None,
            coara_id=self.identity.coara_id,
            coara_name=self.identity.name,
            session_id=self.session_id,
            # scope 只认 user_facing：主会话 True→main_loop，子智能体 False→subagent_loop。
            # is_owner_context 是工具权限上下文（janitor/daily 也为 True 以用 owner_only
            # 工具），不是主会话判据——误并入会把系统管家判成 main_loop 泄漏到 Web 对话页。
            origin_scope=("main_loop" if self.identity.user_facing else "subagent_loop"),
            workspace_dir=str(Path(self.workspace_dir).expanduser().resolve()),
        )

        # Concurrency guard
        self._process_lock = asyncio.Lock()

        self.loop_detector = LoopDetector()
        self.shell_failure_streak_guard = ShellFailureStreakGuard()
        self.tool_hook_runner = ToolHookRunner()
        self.tool_hook_runner.register_before(AccessPolicyHook())
        self.tool_hook_runner.register_before(LoopDetectionHook())
        self.tool_hook_runner.register_before(ShellFailureStreakBeforeHook())
        self.tool_hook_runner.register_post(ShellFailureStreakPostHook(), pattern="shell")
        self.compact_hook_runner = CompactHookRunner()
        self.tool_executor = ToolExecutor()
        self.turn_controller = TurnController()
        # 系统级子智能体（如 daily 统筹全部工作空间）可关闭首回合环境种子注入
        self.inject_environment_seed = True

        # Plan mode state lives in _tool_manager

        logger.info(f"coara created: {name} (user_facing={user_facing}, id={self.identity.coara_id})")

    async def initialize(self) -> None:
        self.status = CoaraStatus.IDLE

        from src.core.config import get_config

        await get_config(force_reload=False)

        from src.coara.runtime_tools import register_runtime_tools

        register_runtime_tools(self)

        # Register deferred-tool gateway (always available, not deferred)
        try:
            from src.tools.builtin.integration.tool import ToolGatewayTool

            self.register_tool(ToolGatewayTool(parent_coara=self), replace=True)
        except Exception as exc:
            logger.warning(f"Tool gateway registration failed: {exc}")

        # 主会话工具开关：config.tools.disabled（不注入 prompt + 执行被拒）
        try:
            from src.core.config import config_manager

            tools_cfg = getattr(getattr(config_manager, "config", None), "tools", None)
            if tools_cfg is not None:
                self._tool_manager.set_disabled(set(tools_cfg.disabled or []))
                # 禁用清单会拼进 system prompt 动态段且静态段缓存 tool 列表：
                # 应用后立即失效缓存，避免首回合后开关变更不生效
                self._invalidate_prompt_cache()
        except Exception as exc:
            logger.warning(f"Failed to apply tools.disabled for {self.identity.name}: {exc}")

        self._emit_trace("initialized", "coara initialized")
        logger.info(f"coara initialized: {self.identity.name}")

    def _emit_final_turn_traces(
        self,
        final_content: str,
        *,
        completed_message: str = "Message processing completed",
    ) -> None:
        """Emit assistant + completed trace events so Dashboard marks the turn OK."""
        text = (final_content or "").strip()
        if text:
            active_turn = getattr(self, "_active_turn", None)
            turn_id = str(getattr(active_turn, "turn_id", "") or "")
            source = str(getattr(self, "_active_turn_source", "") or "")
            self._emit_trace(
                "conversation_message",
                text,
                payload={
                    "role": "assistant",
                    "content": text,
                    "turn_id": turn_id,
                    "source": source,
                },
            )
            self._emit_trace(
                "final_response",
                "Produced final response",
                # 全文刚由 conversation_message 记录 这里只留预览
                payload={"content_preview": text[:200]},
            )
        self._emit_trace("completed", completed_message)

    def _emit_turn_timing(self, timing: TurnTimingRecorder) -> None:
        if timing.finished:
            return
        timing.finished = True
        payload = timing.to_payload()
        self._emit_trace(
            "turn_timing",
            "Turn timing summary",
            payload=payload,
        )
        logger.debug(
            "Turn timing turn_id={} total={:.0f}ms llm={:.0f}ms tools={:.0f}ms overhead={:.0f}ms",
            timing.turn_id,
            payload["total_ms"],
            sum(item["llm_ms"] for item in payload["iterations"]),
            sum(item["tools_ms"] for item in payload["iterations"]),
            payload["overhead_ms"],
        )

    def _format_tool_summary(self, tool_name: str, arguments: Any) -> str:
        """Full single-line tool call label for CLI scrollback (``✓ tool(...)``)."""
        return format_tool_call_label(tool_name, arguments, max_len=None)

    def _wrap_tool_result(self, tool_name: str, result: ToolResult) -> list[dict[str, Any]]:
        """Wrap tool result for message history.

        Delegates to the lightweight injections module. <结果> tag has been
        removed in favor of <系统消息> (informational) and direct content.
        """
        return wrap_tool_result(tool_name, result)

    def note_history_rewrite(self) -> None:
        """Stamp rollback floor after ``message_history`` was replaced (compress).

        Absolute ``turn_history_start`` indices become stale when compression
        swaps in a shorter list; subsequent rollback must keep the rewritten
        prefix and only strip messages appended after this stamp.
        """
        self._history_epoch = int(getattr(self, "_history_epoch", 0) or 0) + 1
        self._rollback_floor = len(self.message_history)

    def clear_rollback_floor(self) -> None:
        """Clear mid-turn rewrite floor (call from turn finally)."""
        self._rollback_floor = None

    def _rollback_partial_turn_history(self, turn_history_start: int) -> None:
        floor = getattr(self, "_rollback_floor", None)
        start = int(floor) if floor is not None else int(turn_history_start)
        if start < 0:
            start = 0
        n = len(self.message_history)
        if start > n:
            start = n
        del self.message_history[start:]

    def _unsent_assistant_text(self, full_text: str) -> str:
        """Return assistant text not yet pushed through the CLI stream hook."""
        if not full_text:
            return ""
        streamed = getattr(self, "_streamed_assistant_chars", 0)
        if streamed >= len(full_text):
            return ""
        return full_text[streamed:]

    async def process_message(
        self,
        content: str,
        *,
        trust_level: str = "owner",
        show_tool_summary: bool = True,
        image_blocks: list[dict[str, Any]] | None = None,
        source: str = "",
        turn_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Process a user or parent message with intermediate progress yields.

        Args:
            content: The message content.
            trust_level: "owner" for trusted local users, "untrusted" for external
                callers (e.g., Matrix strangers). The sandbox is only active when
                trust_level is "untrusted".
            show_tool_summary: Whether to yield tool execution summary lines
                (e.g., "✓ docx(...)"). Matrix clients stream each yield as a message.
            source: Frontend that initiated this turn ("web", "matrix", "cli", "").
                Emitted in trace events so other frontends can sync display.
            turn_id: Optional turn ID. If provided, used for trace events so the
                originating frontend can deduplicate with its direct WS messages.
        """
        # 输入入口统一清洗 UTF-16 代理项（Windows 控制台/粘贴常见），
        # 避免污染历史与事件流（UTF-8 编码失败会引发显示订阅者连环警告）
        from src.utils.text_utils import sanitize_surrogates

        content = sanitize_surrogates(content)

        # 账户门禁——可用性优先的离线宽限：门禁评估本身抛异常（多为网络/文件
        # 异常）时不锁死用户，按本地凭证与试用状态放行；仅当拿到明确判定结果
        # （试用期满/凭证被吊销/服务端判超配额/离线宽限期过）才拦截。
        gate = None
        gate_home = None
        try:
            from src.core.coara_home import resolve_coara_home
            from src.ext import turn_gate

            gate_home = resolve_coara_home(self.workspace_dir)
            gate = await turn_gate(gate_home) if gate_home else None
        except Exception:
            gate = None
        if gate is None:
            # 评估异常（gate 为 None）：构造本地放行结果。有凭证 → 离线放行；
            # 无凭证 → 按本地试用状态。判定逻辑在扩展点实现内，内核不碰。
            try:
                from src.ext import turn_gate_offline_fallback

                gate = turn_gate_offline_fallback(gate_home)
            except Exception:
                gate = None
        if gate is not None and not gate.ok:
            # 后台唤醒回合被门禁拦截时，完成通知不能随一句「请登录」丢弃——
            # 它是已完成的（可能已付费的）工作的交付。降级落工作空间收件箱，
            # 登录后经正常收件箱流程可见，不静默消失。
            if str(source or "").strip().lower() == "background":
                try:
                    from src.tools.builtin.ws.updates_ops import resolve_updates_store

                    store = resolve_updates_store(self)
                    if store is not None:
                        wm = getattr(self, "workspace_manager", None)
                        workspace = ""
                        if wm is not None and getattr(self, "workspace_dir", None):
                            workspace = str(wm.name_for_path(Path(self.workspace_dir)) or "")
                        if workspace:
                            text = content if isinstance(content, str) else str(content)
                            store.append(
                                workspace=workspace,
                                source_id="background:gate_blocked",
                                event_type="background_task_complete",
                                dedupe_key=f"background_gate_blocked:{hash(text) & 0xFFFFFFFF:x}",
                                text=text,
                                payload={
                                    "title": "后台任务完成（账户锁定期间）",
                                    "status": "blocked_by_gate",
                                    "message": text,
                                },
                                type="notification",
                                salience="high",
                                source_kind="internal_report",
                            )
                except Exception:
                    pass
            yield f"[系统] {gate.reason or '账户状态无法确认，请登录后使用'}"
            return

        self._current_trust_level = trust_level
        # 回合内 /new 的延迟清理任务存活期间拒绝新回合：延迟任务正等旧回合
        # 释放 _process_lock，此时放行新消息会让清理排在新回合之后，把它的
        # 历史连同新会话一起抹掉（延迟任务完成或超时放弃后自动恢复受理）
        deferred_new_session = self._deferred_new_session_task
        if deferred_new_session is not None and not deferred_new_session.done():
            yield "[系统] 正在开始新会话，请稍候几秒再发送消息。"
            return
        _queued = self._turn_queue.enqueue(turn_id or "", source or "")
        if self._process_lock.locked():
            logger.info(f"process_message queued for {self.identity.name}, waiting for previous turn...")

        wm = getattr(self, "workspace_manager", None)
        if wm is not None:
            wm.reload_if_stale()

        async with self._process_lock:
            self._turn_queue.remove(_queued)
            if getattr(_queued, "cancelled", False):
                # 中断时已被取消的排队回合：拿到锁直接收尾，不发回合。
                # 取消不等于丢弃：落一条系统注记进历史，原文与用户「以为已送达」的
                # 指令有凭据（后台完成通知同理——此前连入队就被蒸发，无任何记录）。
                preview = content.replace("\n", " ").strip()
                if len(preview) > 200:
                    preview = preview[:197] + "…"
                self.message_history.append(
                    Message(
                        role=MessageRole.SYSTEM,
                        content=f"该消息随打断被丢弃（未执行）：{preview or '[空]'}",
                    )
                )
                yield "[系统] 当前会话已打断。"
                return
            self._inside_turn = True
            self._active_turn_source = source or ""
            # 交互前端发起的回合：记下输入端，后台结果唤醒后的回复回投到这里。
            if (source or "") in ("cli", "web", "matrix", "cli-attached"):
                self._last_user_input_source = source
                # 用户新回合 = 用户已介入：解除 plan_mode submit 的待批准锁
                self._plan_pending_approval = False
                from src.coara.turn_context import get_turn_channel, get_turn_channel_id

                self.session_origin = {
                    "source": source,
                    "channel_id": get_turn_channel_id(),
                }
                self._origin_remote_channel = get_turn_channel()
            self.status = CoaraStatus.RUNNING
            resolved_turn_id = turn_id or str(uuid.uuid4())
            # 首次输入开第 0 段：本回合后续输出归属此端，直到 mid-turn continuation
            # 注入开新段（见 turn_orchestrator drain 处）。channel_id 使同端多连接
            # （多条 attach）并存时输出精确归位发起连接。
            from src.coara.turn_context import get_turn_channel_id as _get_ch_id

            _seg0 = self._segments.open(
                source or "",
                turn_id=resolved_turn_id,
                mid_turn=False,
                channel_id=str(_get_ch_id() or ""),
            )
            self._stamp_segment_model(_seg0)
            _rec_seg = getattr(getattr(self, "_session_log", None), "record_segment_open", None)
            if _rec_seg is not None:
                _rec_seg(seq=_seg0.seq, source=_seg0.source, turn_id=resolved_turn_id, mid_turn=False)
            turn_runtime = TurnRuntime(turn_id=resolved_turn_id, controller=AbortController())
            self._active_turn = turn_runtime
            turn_history_start = len(self.message_history)
            timing = TurnTimingRecorder(turn_id=turn_runtime.turn_id)
            timing.mark("turn_start")
            self._turn_timing = timing
            self._streamed_assistant_chars = 0
            from src.coara.turn_orchestrator import TurnRunContext, run_turn_loop

            ctx = TurnRunContext(
                turn_runtime=turn_runtime,
                turn_history_start=turn_history_start,
                timing=timing,
                show_tool_summary=show_tool_summary,
            )

            # Emit trace events so other frontends (Web UI, Matrix, CLI) can
            # observe this turn in real time — including the user's message
            # and streamed assistant chunks.
            #
            # Emit user_message BEFORE turn_start so the chronological trace
            # order matches the conversation order. This keeps Web UI clients
            # (which append user_message and then turn_start messages to the
            # chat list) from displaying the assistant bubble above the user
            # bubble for turns that originated on CLI/Matrix.
            #
            # 子智能体的任务指令（<任务指令>）不是主会话正文：标记 delegate_brief +
            # parent_tool_call_id，端上折进发起它的 delegate 工具行，而不是新建
            # 一条 role=user 气泡。与落盘路径（delegate._persist_delegate_task_view）
            # 同源同字段——两条路都靠这两个键识别。
            brief_fields: dict[str, Any] = {}
            if str(content or "").lstrip().startswith(TASK_INSTRUCTION_OPEN):
                parent_tool_call_id = _delegated_parent_tool_call_id(self)
                if parent_tool_call_id:
                    brief_fields = {"delegate_brief": True, "parent_tool_call_id": parent_tool_call_id}
            self._emit_trace(
                "user_message",
                content[:200],
                payload={"content": content, "source": source, "turn_id": resolved_turn_id, **brief_fields},
            )
            self._emit_trace(
                "turn_start",
                f"Turn started (source={source or 'unknown'})",
                payload={"turn_id": resolved_turn_id, "source": source},
            )
            self._turn_phase.set("processing")
            phase_watchdog = asyncio.create_task(self._turn_phase_watchdog(resolved_turn_id))

            try:
                async for chunk in run_turn_loop(
                    self,
                    content=content,
                    image_blocks=image_blocks,
                    ctx=ctx,
                ):
                    # Emit chat_chunk for non-tool-summary chunks so other
                    # frontends can display the assistant's streamed text.
                    stripped = chunk.lstrip()
                    if stripped and not stripped.startswith("✓") and not stripped.startswith("✗"):
                        if self._is_delegated_subagent():
                            # 子智能体正文：只投 web（折叠在 delegate 工具行里），
                            # 不发全局 chat_chunk、也不进主会话视图带——它是过程
                            # 信息，另两端的对话流不该被它占据。
                            await self._route_subagent_chunk(chunk)
                        else:
                            # chunk 归属当前注入段（输出跟随最新注入端），而非回合启动端——
                            # mid-turn continuation 注入开新段后，后续 chunk 的 source 随段切换。
                            seg_source = self._segments.source or source
                            self._emit_trace(
                                "chat_chunk",
                                chunk[:200],
                                payload={
                                    "text": chunk,
                                    "turn_id": resolved_turn_id,
                                    "source": seg_source,
                                    "seg": self._segments.current.seq if self._segments.current else 0,
                                },
                            )
                            # 正文动态路由：按当前注入段把 chunk 投递到归属端的当前活跃通道
                            # （EndRegistry）。段切换后 x+3 起流进新注入端。发起端循环对正文
                            # chunk 不再自行渲染（改由本统一路由），避免双显。
                            await self._route_chunk_to_current_end(chunk, seg_source)
                    yield chunk
            except asyncio.CancelledError:
                self._emit_trace(
                    "turn_end",
                    "Turn interrupted",
                    payload={
                        "turn_id": resolved_turn_id,
                        "reason": "interrupted",
                        "source": source,
                    },
                    level="warning",
                )
                raise
            except Exception as exc:
                self._emit_trace(
                    "turn_end",
                    f"Turn ended with error: {exc}",
                    payload={
                        "turn_id": resolved_turn_id,
                        "reason": "error",
                        "source": source,
                    },
                    level="error",
                )
                raise
            else:
                self._emit_trace(
                    "turn_end",
                    "Turn completed",
                    payload={
                        "turn_id": resolved_turn_id,
                        "reason": "complete",
                        "source": source,
                    },
                )
            finally:
                phase_watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await phase_watchdog
                self._turn_phase.clear()
                self._inside_turn = False
                self._active_turn = None
                self._active_turn_source = ""
                # Clear per-turn debug/stream scratch so it never leaks into
                # the next turn or session (mirrors _reset_transient_state).
                self._llm_debug_turn_meta = None
                self._llm_debug_user_input = ""
                self._streamed_assistant_chars = 0
                # 回合结束：应用延迟的 LLM 切换（/model 在回合中执行时标记的）
                pending_switch = getattr(self, "_pending_llm_switch", None)
                if pending_switch is not None:
                    self._pending_llm_switch = None
                    try:
                        # switch_llm 自带 llm_switched（同空间 chrome 再刷一次，幂等）
                        self.switch_llm(pending_switch[0], pending_switch[1])
                    except Exception as exc:
                        logger.warning(f"Deferred LLM switch failed: {exc}")
                # Only reset to IDLE if the turn was still RUNNING. If the turn
                # loop set status to FAILED (stagnation, iteration limit, or
                # repeated tool errors), preserve that status so callers and
                # tests can detect the failure.
                if self.status == CoaraStatus.RUNNING:
                    self.status = CoaraStatus.IDLE
                self._assistant_stream_hook = None
                # 正文回投统一走 EndRegistry 流式路由（各端在回合/跟话注入时
                # 登记通道），不再有收尾补发镜像——旧的 final-reply mirror
                # 与流式路由并存会双显/错序，已随单一机制收敛删除。

    def _is_delegated_subagent(self) -> bool:
        """当前实例是不是某次 delegate 建出来、且用户可见的子智能体。"""
        return bool(_delegated_parent_tool_call_id(self))

    async def _route_subagent_chunk(self, chunk: str) -> None:
        """子智能体正文流：带父标识投给**发起端**，折叠在它的 delegate 工具行里。

        刻意不走 ``_route_chunk_to_current_end``：那条路面向主会话视图带，
        子智能体中间产出进了带就会被 hydrate 当主会话正文复现出来。
        端寻址按 ``_subagent_origin_source``（谁发起的 delegate，就投给谁）——
        web 落视图带、matrix 进房间、attach 走连接，各端自行按父标识折进那条行。
        """
        source = _subagent_origin_source(self)
        if not source:
            return
        root = getattr(self, "_root_ref", None)
        registry = getattr(root, "end_registry", None) if root is not None else None
        if registry is None:
            return
        parent_session = str(getattr(self, "_delegate_parent_session_id", "") or "")
        frame = {
            "kind": "subagent_chunk",
            "text": chunk,
            # tool_call_id 给 CLI 折叠块认父；parent_tool_call_id 给 matrix/web 通道
            # 判定「这是子智能体帧，折进父行」——缺了它帧会退化成普通正文发进房间
            "tool_call_id": str(getattr(self, "_delegate_parent_tool_call_id", "") or ""),
            "parent_tool_call_id": str(getattr(self, "_delegate_parent_tool_call_id", "") or ""),
            "coara_id": str(getattr(getattr(self, "identity", None), "coara_id", "") or ""),
            "subagent_id": str(getattr(self, "_delegate_subagent_id", "") or ""),
            "session_id": parent_session,
            "workspace_dir": str(getattr(self, "_delegate_parent_workspace_dir", "") or ""),
        }
        origin = getattr(self, "_subagent_origin", None)
        channel_id = str(origin[1] or "") if isinstance(origin, tuple) and len(origin) > 1 else ""
        try:
            outcome = registry.deliver(source, parent_session, frame, channel_id=channel_id)
            if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
                await outcome.value
        except Exception:  # noqa: BLE001 — 子智能体正文投递失败绝不影响它干活
            logger.debug("route subagent chunk failed", exc_info=True)

    async def _route_chunk_to_current_end(self, chunk: str, seg_source: str) -> None:
        """把正文 chunk 按当前注入段投递到归属端的当前活跃通道（EndRegistry）。

        输出跟随最新注入端：段切换后，归属端的注册通道接到后续 chunk。
        无注册通道（该端离线/未接）时静默跳过并 WARNING 留痕；不另设第二落点
        ——落带唯一入口是端通道内的 TurnStream（web 端 sender → _record →
        persist → WebViewStore.append_event），在路由层再落一次就是重复帧。

        投递出错吞掉（单端故障不拖死回合）。
        """
        if not chunk.strip():
            return
        chunk = _sanitize_outbound_text(chunk)
        if not chunk:
            return
        root = self._root_ref
        registry = getattr(root, "end_registry", None) if root is not None else None
        if registry is None:
            logger.debug(f"route chunk: no end_registry (seg_source='{seg_source}')")
            return
        session_id = str(getattr(self, "session_id", "") or "")
        frame = {
            "kind": "chunk",
            "text": chunk,
            "session_id": session_id,
            # 空间归属：端侧靠它判断这帧属不属于当前看的空间。不带这一笔，端侧
            # 的边界守卫对「无字段帧」只能放行——切空间后就可能把别的空间的输出
            # 画到眼前的对话里（显示互串）。
            "workspace_dir": str(getattr(self, "workspace_dir", "") or ""),
            "turn_id": str(getattr(getattr(self, "_active_turn", None), "turn_id", "") or ""),
        }
        outcome = registry.deliver(
            seg_source,
            session_id,
            frame,
            channel_id=self._segments.channel_id,
        )
        if not outcome.hit:
            # 兜底两级：归属端无通道 → 回合发起端；发起端也无（background 唤醒
            # 回合永远没有自己的通道）→ 会话归属端（session_origin，最近一次
            # 真实用户输入端）。都无通道才丢弃，且 WARNING 留痕。
            session_origin = getattr(self, "session_origin", None) or {}
            origin_ch = str(session_origin.get("channel_id") or "")
            candidates: list[tuple[str, str]] = []
            launch_source = str(getattr(self, "_active_turn_source", "") or "")
            if launch_source and launch_source != seg_source:
                candidates.append((launch_source, origin_ch if launch_source == session_origin.get("source") else ""))
            origin_source = str(session_origin.get("source") or "")
            if origin_source and origin_source not in (seg_source, launch_source):
                candidates.append((origin_source, origin_ch))
            for cand_source, cand_ch in candidates:
                outcome = registry.deliver(cand_source, session_id, frame, channel_id=cand_ch)
                if outcome.hit:
                    logger.debug(f"route chunk: no sender for seg end '{seg_source}', fallback to '{cand_source}'")
                    break
        if not outcome.hit:
            logger.warning(
                f"route chunk: no sender for end '{seg_source}' "
                f"(session={session_id}, "
                f"launch='{getattr(self, '_active_turn_source', '')}', {len(chunk)} chars dropped)"
            )
            return
        try:
            if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
                await outcome.value
        except Exception:
            logger.debug(f"route chunk to end '{seg_source}' failed", exc_info=True)

    async def _deliver_plan_review_to_end(self, body: str) -> bool:
        """把计划审阅正文按当前注入段投递到端通道（与助手正文同一 chunk 通路）。

        计划审阅不是独立通道消息（旧 send_text/"info" 帧 attach 端不消费）——
        走 EndRegistry chunk 路由后，attach/web 发起端经各自 TurnStream 正常上屏，
        与普通回合正文同一事实源。无端通道（本地 CLI / 无界面 / 测试）返回 False，
        由调用方回退 CliScrollback 等本地兜底。
        """
        if not body.strip():
            return False
        root = self._root_ref
        registry = getattr(root, "end_registry", None) if root is not None else None
        if registry is None:
            logger.debug("plan review: no end_registry")
            return False
        seg_source = self._segments.source if self._segments is not None else ""
        if not seg_source:
            seg_source = str(getattr(self, "_active_turn_source", "") or "")
        if not seg_source:
            return False
        sess_id = str(getattr(self, "session_id", "") or "")
        channel_id = self._segments.channel_id if self._segments is not None else ""
        try:
            outcome = registry.deliver(seg_source, sess_id, {"kind": "chunk", "text": body}, channel_id=channel_id)
            if not outcome.hit:
                return False
            if asyncio.iscoroutine(outcome.value) or asyncio.isfuture(outcome.value):
                await outcome.value
        except Exception:
            logger.debug("plan review chunk delivery failed", exc_info=True)
            return False
        return True

    def _route_session_id(self) -> str:
        """输出帧路由键的 session_id：子智能体（非 user_facing）回退父会话。

        端通道按父会话 session_id 注册（回合消费者在父会话回合启动时登记）；
        子智能体有自己的 sa-xxx session_id，直接拿它路由会查不到通道，
        子智能体活动（工具 diff）将全部丢失。
        """
        sess_id = str(getattr(self, "session_id", "") or "")
        if not getattr(getattr(self, "identity", None), "user_facing", True):
            parent = getattr(self, "_subagent_parent", None)
            if parent is not None:
                sess_id = str(getattr(parent, "session_id", "") or sess_id)
        return sess_id

    def _route_tool_diff(self, payload: dict[str, Any]) -> None:
        """把工具 diff 按当前注入段 source 路由到端通道（与正文 chunk 同源）。

        executor 在 tool_complete 事件发布后调用：diff 帧经 EndRegistry.deliver
        投递（命中判据是 RouteResult.hit），端通道 sender 自行渲染。归属 = payload.source（回合段来源），
        与正文 chunk 同一事实源——diff 跟最近注入端，不再各端各自订阅过滤。
        无 display_blocks 或非交互端（background/event）时不投。

        ``_OUTPUT_FRAME_DIFF_ENABLED`` 开关：diff 帧统一投递需各端 sender 的
        diff 渲染与前端/视图存储适配就绪；开启前 diff 保持既有消费
        （trace_broadcast / diff_bridge / display_controller），避免双投。
        """
        if not self._OUTPUT_FRAME_DIFF_ENABLED:
            return
        if not payload.get("display_blocks"):
            return
        # 子智能体的 diff：与它的工具行同路——带父标识投发起端，端上折进发起它的
        # delegate 行，不铺成主列表的独立卡片（「子智能体的改动也进折叠」）。
        if not getattr(getattr(self, "identity", None), "user_facing", True):
            _route_subagent_tool_frame(
                self,
                payload,
                {
                    "kind": "diff",
                    "display_blocks": payload.get("display_blocks"),
                    "diff_lines": payload.get("diff_lines"),
                    "is_error": bool(payload.get("is_error", False)),
                    "tool_name": payload.get("tool_name", ""),
                    "tool_call_id": payload.get("tool_call_id", ""),
                },
            )
            return
        # subagent_origin 与 executor 的 tool_complete payload 同源——主流 source 判定
        # 读 payload 的 subagent_origin，子智能体 diff 按派发来源投父会话 sender。
        _payload = dict(payload)
        _origin_snap = getattr(self, "_subagent_origin", None)
        if isinstance(_origin_snap, (tuple, list)) and len(_origin_snap) >= 2 and str(_origin_snap[0] or ""):
            _payload.setdefault("subagent_origin", str(_origin_snap[0] or ""))
        _route_tool_frame(
            self,
            {
                "kind": "diff",
                "display_blocks": payload.get("display_blocks"),
                "diff_lines": payload.get("diff_lines"),
                "is_error": bool(payload.get("is_error", False)),
                "tool_name": payload.get("tool_name", ""),
                # subagent_origin 与 executor 的 tool_complete payload 同源——主流
                # source 判定读它，子智能体 diff 按派发来源投父会话 sender。
                "subagent_origin": str(
                    payload.get("subagent_origin")
                    or getattr(self, "_subagent_origin", ("", None))[0]
                    or ""
                ),
            },
            payload=_payload,
            what="diff",
        )

    def _route_tool_line(self, payload: dict[str, Any]) -> None:
        """把工具行（``✓ read(...)``）按当前注入段 source 路由到端通道。

        与 diff 帧同源同路由。web 端把它作为聊天流的一员插在正文段落之间
        （落视图文件，刷新回放位置不变）；CLI attach 收到后显式忽略（自有
        scrollback）；matrix 通道把帧包成 ``[COARA_TOOL]`` 进房间，手机端渲染。

        子智能体的工具行走另一条路（``_route_subagent_tool_line``）：带父标识
        投给发起端，端上折进发起它的 delegate 工具行——一个 delegate 几十行子
        工具绝不刷进主会话正文流。
        """
        label = str(payload.get("tool_label") or "").strip()
        if not label:
            return
        if getattr(self, "_cli_silent", False):
            # 系统维护 agent（janitor/daily）全程静默，其工具行走不进任何端。
            return
        if not getattr(getattr(self, "identity", None), "user_facing", True):
            _route_subagent_tool_line(self, payload, label)
            return
        frame: dict[str, Any] = {
            "kind": "tool",
            "text": label,
            "tool_name": payload.get("tool_name", ""),
            "tool_call_id": payload.get("tool_call_id", ""),
            "is_error": bool(payload.get("is_error", False)),
            "duration_ms": payload.get("duration_ms"),
        }
        # 慢工具预览（手机）：仍在跑时带 running，端上转圈；完成帧不带，覆盖同 id 行
        if bool(payload.get("running")):
            frame["running"] = True
            frame.pop("duration_ms", None)
        _route_tool_frame(
            self,
            frame,
            payload=payload,
            what="tool line",
        )

    async def _turn_phase_watchdog(self, turn_id: str) -> None:
        """回合进行中每 60s 落一条相位快照（DEBUG），用于事后定位卡顿."""
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await asyncio.sleep(60)
                desc = self._turn_phase.describe()
                if desc:
                    logger.debug(f"turn {turn_id} phase: {desc}")

    def _resolve_context_input_tokens(self, system_prompt: str, messages: list[Message]) -> int:
        return context_window_manager.resolve_input_tokens(
            system_prompt=system_prompt,
            messages=messages,  # type: ignore[arg-type]
            tool_definitions=self._get_tool_definitions_for_llm(),
            snapshot=self._llm_usage_snapshot,
        )

    def _evaluate_context_guard(
        self,
        system_prompt: str,
        messages: list[Message],
        max_tokens: int | None = None,
        *,
        used_tokens: int | None = None,
    ):
        tokens = used_tokens if used_tokens is not None else self._resolve_context_input_tokens(system_prompt, messages)
        return context_window_manager.evaluate_guard(tokens, max_tokens=max_tokens)

    def _stop_for_stagnation(self, iteration: int, message: str, reason: str) -> str:
        self.message_history.append(Message(role=MessageRole.ASSISTANT, content=message))
        self.status = CoaraStatus.FAILED
        self._emit_trace(
            "stagnation_stop",
            message,
            level="error",
            payload={"iteration": iteration, "reason": reason},
        )
        self._emit_final_turn_traces(message, completed_message=f"Stagnation stop: {reason}")
        return message

    def _get_output_truncation_settings(self):
        from src.core.config import config_manager

        try:
            cfg = getattr(config_manager, "config", None)
        except Exception:
            # 配置尚未加载（如测试直接构造 CoaraBase）——回落 raw config
            cfg = None
        raw = getattr(cfg, "output_truncation", None) if cfg is not None else None
        if raw is None:
            raw = getattr(config_manager, "_raw_config", {}).get("output_truncation")
        return settings_from_config(raw or {})

    def _resolved_output_max_tokens(self) -> int:
        from src.llm.service import llm_service

        return llm_service.resolve(
            self.llm_profile,
            provider_name=self.provider_name,
            model=self.model_name,
        ).max_tokens

    async def _complete_turn(
        self,
        system_prompt: str,
        messages: list[Message],
        signal,
    ) -> LLMResponse:
        from src.llm.active_context import resolve_active_llm

        tool_definitions = self._get_tool_definitions_for_llm()
        resolved = llm_service.resolve(
            self.llm_profile,
            provider_name=self.provider_name,
            model=self.model_name,
        )
        llm_ctx = resolve_active_llm(self)

        response: LLMResponse | None = None
        call_err: BaseException | None = None
        try:
            # 让前端能区分「等待模型响应」与「本地处理中」（大上下文请求可能等数分钟）
            self._emit_trace(
                "llm_request_start",
                f"LLM request → {llm_ctx.provider_name}/{self.model_name}",
                payload={"provider": llm_ctx.provider_name, "model": self.model_name},
            )
            self._turn_phase.set("awaiting_llm", f"{llm_ctx.provider_name}/{self.model_name}")
            response = await llm_service.complete(
                LLMRequest(
                    profile=self.llm_profile,
                    provider=self.provider_name,
                    model=self.model_name,
                    messages=messages,
                    system_prompt=system_prompt,
                    tools=tool_definitions,
                    signal=signal,
                    on_assistant_delta=getattr(self, "_assistant_stream_hook", None),
                )
            )
            self._turn_phase.set("processing")
        except BaseException as exc:
            call_err = exc

        turn_meta = getattr(self, "_llm_debug_turn_meta", None)
        if self.identity.user_facing:
            # FlowRoot（构建对话）与主会话同为 user_facing，但归属不同主体：
            # llmlog 按 _session_agent_kind 区分，不混为两个「主会话」
            agent_kind = "构建对话" if self._session_agent_kind == "flow" else "主会话"
        else:
            agent_kind = getattr(self.identity.persona, "name", "") or self.identity.name
        overview = build_agent_overview(
            user_facing=bool(self.identity.user_facing),
            persona_name=agent_kind if not self.identity.user_facing else "",
            background=bool(getattr(self, "_delegate_background", False)),
            bound_tool_names=self._tool_manager.get_bound_tool_names(),
        )
        if self._session_agent_kind == "flow":
            overview = "构建对话"
        self._session_llm_call_count = int(getattr(self, "_session_llm_call_count", 0) or 0) + 1
        batch_tool_calls = len(response.tool_calls) if response is not None and response.tool_calls else 0
        self._session_tool_call_count = int(getattr(self, "_session_tool_call_count", 0) or 0) + batch_tool_calls
        meta = Meta(
            session_id=(turn_meta or {}).get("session_id") or self.session_id,
            turn_id=(turn_meta or {}).get("turn_id") or "",
            user_input=(turn_meta or {}).get("user_input") or getattr(self, "_llm_debug_user_input", "") or "",
            llm_call=self._session_llm_call_count,
            tool_call_count=self._session_tool_call_count,
            provider_name=llm_ctx.provider_name,
            agent_id=self.identity.name,
            agent_kind=agent_kind,
            overview=overview,
        )

        # 磁盘镜像写入放线程池：序列化完整对话 + 写盘在 event loop 里同步做
        # 会阻塞 CLI 输入（会话越大单条 entry 越大，342MB 级镜像时每轮 LLM 都卡）
        # FlowRoot（构建对话）与 daily（日报整理）不与任何工作空间绑定，挂独立伪键
        if self._session_agent_kind == "flow":
            log_workspace = FLOW_WORKSPACE_KEY
        elif str(getattr(self.identity.persona, "name", "") or "").strip().lower() == "daily":
            log_workspace = DAILY_WORKSPACE_KEY
        else:
            log_workspace = str(self.workspace_dir)
        await asyncio.to_thread(
            log_llm_call,
            log_workspace,
            meta,
            system_prompt=system_prompt,
            messages=messages,
            model=self.model_name,
            max_tokens=resolved.max_tokens,
            temperature=resolved.temperature,
            tools=tool_definitions,
            response=response,
            call_err=call_err,
        )

        if call_err is not None:
            raise call_err

        try:
            # 指纹失败不炸成功回合：None 时 resolve_input_tokens 退化长度比较
            payload_hash = payload_fingerprint_cached(system_prompt, tool_definitions)
        except Exception:
            payload_hash = None
        self._llm_usage_snapshot.record_turn(
            usage=dict(response.usage or {}),
            history_len=len(messages),
            system_len=len(system_prompt),
            tool_count=len(tool_definitions),
            payload_hash=payload_hash,
            partial=(response.finish_reason or "").strip().lower() == "partial",
        )

        return response

    def submit_continuation_input(
        self,
        text: str,
        image_blocks: list[dict[str, Any]] | None = None,
        *,
        source: str = "",
        agent_origin: str = "",
        agent_origin_channel: str = "",
    ) -> None:
        """Buffer a user message to be injected mid-turn.

        ``image_blocks`` carries optional Vision attachments so a follow-up
        pasted mid-turn keeps its images (Plan B: multimodal continuation).
        ``source`` tags the follow-up's origin (``cli``/``matrix``/``web``);
        required for correct CLI mirroring when the active turn is unrelated
        (e.g. phone message during a ``background`` awaken turn).
        ``agent_origin`` / ``agent_origin_channel`` tag a *subagent result* with
        the delegate-call user input origin + matrix room (never clears the
        plan lock — it is system injection).
        """
        # Windows 控制台/粘贴可能产生 UTF-16 代理项（无法 UTF-8 编码），
        # 进入事件流会让显示订阅者编码失败——入口统一清洗
        from src.utils.text_utils import sanitize_surrogates

        text = sanitize_surrogates(text)
        origin = str(source or "").strip()
        agent_src = str(agent_origin or "").strip()
        origin_ch = str(agent_origin_channel or "").strip()
        from src.coara.turn_context import get_turn_channel_id

        _origin_channel_id = str(get_turn_channel_id() or "")
        # 带来源的接续输入 = 用户中途介入：解除 plan_mode submit 的待批准锁。
        # 系统注入（delegate 结果等）不带 source，不误清。
        if origin:
            self._plan_pending_approval = False
        # 入队只记 source 在队列项上，不刷新「最近一次输入端」——那是注入语义，
        # 归属到 turn loop 迭代头真正注入上下文那一刻（开新段时才更新）。
        # 入队≠注入：跟话入队后 LLM 仍在跑上一段的输出，端归属不能提前切换。
        self._continuation_inputs.append(
            ContinuationInput(
                text=text,
                image_blocks=image_blocks,
                source=origin,
                agent_origin=agent_src,
                agent_origin_channel=origin_ch,
                channel_id=_origin_channel_id,
                deferred_remote_ctx=self._pending_deferred_remote_ctx,
            )
        )
        # Stamp moves onto the queue item; do not leave a shared slot for the next end.
        self._pending_deferred_remote_ctx = None
        self._deferred_remote_ctx = None
        self._continuation_event.set()
        self._emit_trace(
            "continuation_input_received",
            f"Continuation input received: {text[:80]}",
            payload={
                "text": text,
                "image_count": len(image_blocks or []),
                "source": origin,
                "agent_origin": agent_src,
                "agent_origin_channel": origin_ch,
            },
        )

    def drain_continuation_inputs(self) -> list[ContinuationInput]:
        """Pop all buffered continuation inputs."""
        if not self._continuation_inputs:
            return []
        items = self._continuation_inputs[:]
        self._continuation_inputs.clear()
        if not self._continuation_inputs:
            self._continuation_event.clear()
        return items

    def set_deferred_remote_ctx(
        self, room_id, send_text, interaction_channel, *, source: str = "", actor: str = ""
    ) -> None:
        """Stamp remote turn context for the *next* ``submit_continuation_input``.

        Called by the ingress defer path (busy turn) so the turn loop can
        re-apply ``turn`` ContextVars when it consumes that specific input
        （审批/询问走远端通道）。Stamp is per queue item — a later end cannot
        overwrite an earlier follow-up's approval channel.

        正文输出归属由 **segment**（最近一次用户注入端）经 EndRegistry 路由——
        各端在跟话注入时已登记自己的流式通道，不再设收尾补发镜像。
        """
        ctx = (room_id, send_text, interaction_channel, source, actor)
        self._pending_deferred_remote_ctx = ctx
        # Compat mirror until submit attaches the stamp to the queue item.
        self._deferred_remote_ctx = ctx

    def clear_deferred_remote_ctx(self) -> None:
        """Drop pending/compat deferred remote context (turn cleanup)."""
        self._deferred_remote_ctx = None
        self._pending_deferred_remote_ctx = None

    # ------------------------------------------------------------------
    # Foreground delegate tracking
    # ------------------------------------------------------------------

    def register_foreground_delegate(self, task_id: str, task: Any, description: str) -> None:
        """Register a foreground delegate task launched asynchronously.

        The turn loop's exit guard consults this registry to prevent the
        turn from exiting while foreground subagents are still running.
        """
        self._pending_foreground_delegates[task_id] = task
        self._foreground_delegate_descriptions[task_id] = description

    def on_foreground_delegate_done(self, task_id: str, description: str, task: Any) -> None:
        """Called when a foreground delegate asyncio.Task completes.

        Extracts the result and injects it as a continuation input so the
        next iteration sees it and can continue reasoning.

        Cancelled tasks are skipped: cancellation happens during
        ``_reset_transient_state`` / turn cleanup, so injecting a
        "子代理被取消" message would be stale noise.

        Released delegates（回合出口放行）: session busy → same continuation
        injection; session idle → park the result into session history (and
        persist) so the next turn sees it, without waking a new turn.
        """
        released = task_id in self._released_foreground_delegates
        self._pending_foreground_delegates.pop(task_id, None)
        self._released_foreground_delegates.pop(task_id, None)
        self._foreground_delegate_descriptions.pop(task_id, None)

        if task.cancelled():
            return

        from src.core.message_tags import system_info

        if task.exception() is not None:
            exc = task.exception()
            # str(exc) 可为空串（如裸 CancelledError/Error()）：回退异常类型名，保证失败信息非空可辨
            result_text = f"子代理失败: {str(exc) or type(exc).__name__}"
        else:
            tool_result = task.result()
            # _run_subagent swallows CancelledError and returns ToolResult.cancelled
            # — treat that like task.cancelled() so interrupt cleanup stays quiet.
            if getattr(tool_result, "is_cancelled", False):
                return
            content = getattr(tool_result, "content", None)
            result_text = str(content) if content is not None else str(tool_result)

        message = system_info(f"[前台子智能体已完成] [{task_id}]\n任务：{description}\n结果：{result_text}")
        # 段归属：delegate 调用时父会话注入段来源 + matrix 房间，随结果注入传给
        # turn loop（与子智能体 diff 同锚点；收官直推不靠 ContextVar）。
        sub_origin = ""
        sub_channel = ""
        try:
            result = task.result()
            meta = getattr(result, "metadata", None) or {}
            sub_origin = str(meta.get("subagent_origin") or "")
            sub_channel = str(meta.get("subagent_origin_channel") or "")
        except Exception:  # noqa: BLE001
            sub_origin = ""
            sub_channel = ""
        if released and not self.has_active_turn():
            # 放行后的迟到结果：会话空闲 → 进驻历史（下一轮可见），不唤醒新回合
            self.message_history.append(Message(role=MessageRole.USER, content=message))
            try:
                loop = asyncio.get_running_loop()
                # 落盘任务必须持引用：fire-and-forget 任务随时可能被 GC
                # 回收协程，落盘静默丢失
                persist_task = loop.create_task(asyncio.to_thread(self.persist_session_to_disk))
                self._session_persist_tasks.add(persist_task)
                persist_task.add_done_callback(self._session_persist_tasks.discard)
            except RuntimeError:
                pass
            return
        self.submit_continuation_input(
            message,
            agent_origin=sub_origin,
            agent_origin_channel=sub_channel,
        )

    def release_pending_foreground_delegates(self) -> list[str]:
        """放行全部在跑的前台子智能体：回合可正常结束，完成结果按迟到语义路由。

        Returns the released task_ids (still running only).
        """
        released: list[str] = []
        for task_id, task in list(self._pending_foreground_delegates.items()):
            if task.done():
                continue
            self._released_foreground_delegates[task_id] = task
            self._pending_foreground_delegates.pop(task_id, None)
            released.append(task_id)
        return released

    def has_pending_foreground_delegates(self) -> bool:
        """True if any foreground delegate task is still running."""
        # Clean up finished tasks first
        done_ids = [tid for tid, task in self._pending_foreground_delegates.items() if task.done()]
        for tid in done_ids:
            self._pending_foreground_delegates.pop(tid, None)
            self._foreground_delegate_descriptions.pop(tid, None)
        return bool(self._pending_foreground_delegates)

    def pending_foreground_delegate_descriptions(self) -> str:
        """Return a formatted list of pending foreground delegate descriptions."""
        lines = []
        for task_id, desc in self._foreground_delegate_descriptions.items():
            lines.append(f"- [{task_id}] {desc}")
        return "\n".join(lines)

    def cancel_all_pending_foreground_delegates(self, *, include_released: bool = True) -> None:
        """Cancel all running foreground delegate tasks (e.g. on turn interrupt).

        include_released=True（中断路径）：连放行的一起停，Ctrl+C 语义不变。
        include_released=False（回合正常退出的 finally）：放行的继续跑，
        迟到结果经 done 回调驻留历史。
        """
        tasks = list(self._pending_foreground_delegates.items())
        if include_released:
            tasks += list(self._released_foreground_delegates.items())
        for task_id, task in tasks:
            if not task.done():
                task.cancel()
                # Emit failed so CLI spinner drops the live node. Tasks cancelled
                # before ``_run_subagent`` runs never emit subagent_failed themselves
                # (seen when /new raced fire-and-forget foreground_async janitor).
                self._emit_trace(
                    "subagent_failed",
                    f"Subagent cancelled: {task_id}",
                    payload={
                        "subagent_id": task_id,
                        "description": self._foreground_delegate_descriptions.get(task_id, ""),
                        "error": "cancelled",
                        "workspace_dir": str(getattr(self, "workspace_dir", "") or ""),
                    },
                )
        self._pending_foreground_delegates.clear()
        if include_released:
            self._released_foreground_delegates.clear()
        self._foreground_delegate_descriptions.clear()

    def has_active_turn(self) -> bool:
        """True while this instance still owns an in-flight turn.

        Single busy predicate for defer / followup / janitor / overview gates.
        ``status`` may flip to IDLE on normal completion paths before finally
        clears ``_active_turn`` / ``_inside_turn``; do not require RUNNING or
        those mid-teardown windows look idle and flip mid-turn decisions.
        """
        return self._inside_turn or self._active_turn is not None or self._process_lock.locked()

    @property
    def flow_coordinator(self) -> Any:
        """本实例的会话内 flow 图协调器（惰性创建，实例间互不可见）。"""
        if self._flow_coordinator is None:
            from src.coara.flow_coordinator import FlowCoordinator

            self._flow_coordinator = FlowCoordinator()
        return self._flow_coordinator

    def is_turn_busy(self) -> bool:
        """Alias of :meth:`has_active_turn` — one busy clock for the whole runtime."""
        return self.has_active_turn()

    def interrupt_current_turn(
        self,
        reason: str = "user_interrupt",
        *,
        interrupt_source: str | None = None,
        cancel_delegates: bool = True,
    ) -> bool:
        runtime = self._active_turn
        # 中断=用户对本会话失去耐心：仍在等锁的排队回合一并取消（否则锁释放后
        # 它们会照常跑完，输出回投到已收尾的端流——答非所问/正文串台）。
        # 空队列时 no-op。取消记账先行：等锁协程拿锁后的第一件事实质是
        # ``_turn_queue.remove(_queued)``，remove 对不在队列的项是 no-op，
        # 所以必须在拿锁后按「是否已被取消」二次判定（见 process_message）。
        for _item in self._turn_queue.cancel_all():
            _item.cancelled = True
        if runtime is None or runtime.signal.aborted:
            return False
        runtime.reason = reason
        runtime.controller.abort(reason)
        turn_origin = self._resolve_interrupt_turn_origin()
        source = interrupt_source or "unspecified"
        # Release any pending vault password prompt so a blocked vault(open)
        # tool call returns immediately instead of waiting for the 60s
        # timeout. Without this the vault await can keep the turn alive after stop.
        try:
            from src.vault.prompt_registry import resolve as _resolve_vault_pending

            _resolve_vault_pending({"ok": False, "cancelled": True, "message": "turn interrupted"})
        except Exception:
            logger.debug("vault pending resolve on interrupt failed", exc_info=True)
        # 打断安全网：在途审批统一收口 ApprovalCenter（matrix/web/attach 全端）。
        # cancel_all 把 pending 转 cancelled 并经各端 send_terminal 回推
        # m.coara.approval_resolved 终态帧（matrix 手机据此置灰），比旧的矩阵独立 cancel 覆盖更全。
        try:
            from src.coara.approval_center import get_approval_center

            get_approval_center().cancel_all(reason=reason)
        except Exception:
            logger.debug("ApprovalCenter cancel_all on interrupt failed", exc_info=True)
        # Forcefully abort any pending HTTP requests so the turn stops
        # immediately even when asyncio cancellation does not promptly
        # propagate to the HTTP layer (notably on Windows).
        try:
            llm_service.abort_active()
            if self.provider is not None:
                self.provider.abort()
        except Exception as exc:
            logger.warning(f"Provider abort failed: {exc}")
        # Hard-stop every in-flight subagent and background task (fg/bg agents +
        # bash task jobs): interrupt children, then asyncio.Task.cancel(). Soft
        # signal alone is not enough mid-tool / mid-LLM. Nested child interrupts
        # pass cancel_delegates=False.
        if cancel_delegates:
            try:
                from src.tools.builtin.delegate.delegate import hard_cancel_all_running_delegates

                cancel_info = hard_cancel_all_running_delegates(reason=reason)
                # Snapshot for interrupt history injection (resume tip with task_ids).
                self._interrupt_cancelled_delegates = list(cancel_info.get("cancelled_delegates") or [])
            except Exception as exc:
                logger.warning(f"Hard-cancel background work failed: {exc}")
                self._interrupt_cancelled_delegates = []
            self.cancel_all_pending_foreground_delegates()
            # Drop queued continuation so cancelled-delegate done callbacks cannot
            # inject stale "子智能体已完成" into the next turn.
            self._continuation_inputs.clear()
        else:
            self._interrupt_cancelled_delegates = []
        self._emit_trace(
            "turn_interrupt_requested",
            "Interrupt requested for current turn",
            level="warning",
            payload={
                "turn_id": runtime.turn_id,
                "reason": reason,
                "turn_origin": turn_origin,
                "interrupt_source": source,
                "cancel_delegates": cancel_delegates,
            },
        )
        # 打断是预期路径（Ctrl+C / /stop / new_session / 切换空间），不污染 error log：
        # 记 info 留痕即可，trace 里的 turn_interrupted 事件保留给 WebUI。
        logger.bind(
            coara_id=self.identity.coara_id,
            coara_name=self.identity.name,
            session_id=self.session_id,
            workspace_dir=self.workspace_dir,
            event="turn_interrupt",
            turn_id=runtime.turn_id,
            reason=reason,
            turn_origin=turn_origin,
            interrupt_source=source,
        ).info(f"Turn interrupted: reason={reason} turn_origin={turn_origin} interrupt_source={source}")
        return True

    def _resolve_interrupt_turn_origin(self) -> str:
        """Whether the active turn was started from remote ingress vs local CLI."""
        source = getattr(self, "_active_turn_source", "") or ""
        if source == "matrix":
            return "remote"
        try:
            from src.coara.turn_context import has_turn_channel

            return "remote" if has_turn_channel() else "cli"
        except Exception:
            return "cli"

    def _raise_if_interrupted(self, runtime: TurnRuntime) -> None:
        if runtime.signal.aborted:
            raise CoaraRunCancelledError(runtime.reason)

    def _build_system_prompt(self) -> str:
        """Build system prompt.

        Strategy: Static template rendered once, runtime context appended every turn.
        Environment / subagent info is appended as text rather than injected
        as separate messages, keeping message_history pure conversation.
        """
        # 1. Cache (Static prompt never changes during session)
        if "static" not in self._static_prompt_cache:
            builder = PromptBuilder()
            if self.identity.persona.yaml_config is not None:
                builder.set_yaml_config(self.identity.persona.yaml_config)
            else:
                builder.set_role_prompt(
                    self.identity.persona.system_prompt_template or "You are a helpful AI assistant."
                )
            builder.set_workspace_dir(self.workspace_dir)
            prompt = builder.build()
            prompt = self._inject_skill_list(prompt)
            prompt = self._inject_deferred_tool_list(
                prompt,
                self._tool_manager,
                self.identity.is_owner_context,
            )
            self._static_prompt_cache["static"] = prompt

        prompt = self._static_prompt_cache["static"]

        # 动态段：被禁用的工具明确告知（tools 参数已过滤，这里给模型硬信号避免误调）
        disabled_names = self._tool_manager.get_disabled_names()
        if disabled_names:
            names_text = "、".join(f"`{n}`" for n in disabled_names)
            prompt += (
                "\n\n<系统提醒>以下工具当前已被禁用，禁止调用："
                f"{names_text}。如需恢复请运行 /tools on <名称>。</系统提醒>"
            )
        return prompt

    def _inject_skill_list(self, prompt: str) -> str:
        """Render ``${COARA_SKILL_LIST}`` with discovered skill names (root.md opt-in).

        两段式：listed（人工策展）技能名直接列出；挂起技能（自动生成、未策展）
        只给裸名字并指引用 skill(action="search") 查描述——与挂起工具清单同构。
        子智能体无技能模块：需要技能时由主会话把技能内容写进任务指令。
        """
        if "${COARA_SKILL_LIST}" not in prompt:
            return prompt
        all_skills = self.skill_manager.get_all()
        listed = [s.name for s in all_skills if s.listed]
        unlisted = [s.name for s in all_skills if not s.listed]
        sections: list[str] = []
        sections.append("、".join(listed) if listed else "（无）")
        if unlisted:
            shown, overflow = unlisted[:30], len(unlisted) - 30
            names_text = "、".join(shown) + (f" 等 {len(unlisted)} 个" if overflow > 0 else "")
            sections.append('挂起技能（仅名字，描述用 `skill(action="search")` 查询）：' + names_text)
        return prompt.replace("${COARA_SKILL_LIST}", "\n".join(sections))

    @staticmethod
    def _inject_deferred_tool_list(
        prompt: str,
        tool_manager: Any,
        is_owner_ctx: bool,
    ) -> str:
        """Render ``${COARA_DEFERRED_TOOL_LIST}`` — deferred built-in tools.

        Deferred tools get name + one-line description. The listing is static
        for the whole session: revealed tools stay listed so the system prompt
        prefix never changes on activation (prompt-cache friendly).
        """
        if "${COARA_DEFERRED_TOOL_LIST}" not in prompt:
            return prompt

        deferred = tool_manager.get_deferred_tool_summaries(is_owner_ctx, include_revealed=True)

        if deferred:
            listing = '挂起的内置工具（`tool(action="activate", name="…")` 装载后即可调用）：\n' + "\n".join(
                f"- `{item['name']}` — {item['description']}" for item in deferred
            )
        else:
            listing = "（当前无挂起工具）"
        return prompt.replace("${COARA_DEFERRED_TOOL_LIST}", listing)

    def _invalidate_prompt_cache(self) -> None:
        self._static_prompt_cache.clear()
        self._tool_manager.invalidate_cache()

    # ── Plan mode ──

    def enter_plan_mode(self, plan_file_path: Path) -> None:
        """Enter plan mode: restrict tools to read-only and set plan file."""
        self._tool_manager.enter_plan_mode(plan_file_path)
        self._invalidate_prompt_cache()
        self._emit_plan_mode_changed()

    def exit_plan_mode(self) -> None:
        """Exit plan mode: restore full tool set."""
        self._tool_manager.exit_plan_mode()
        self._invalidate_prompt_cache()
        self._emit_plan_mode_changed()

    def _emit_plan_mode_changed(self) -> None:
        """计划模式开关后发 trace，显示端据此刷新 chrome/状态栏。

        带 session_id/workspace_dir 供服务端按 pin 空间过滤（与 llm_switched
        同级的空间 chrome 事件，不是端作用域回合事件）——attach 客户端据此
        更新 is_plan_mode 镜像，状态栏「☰ 计划模式」即时出现/消失。
        """
        self._emit_trace(
            "plan_mode_changed",
            "计划模式已开启" if self.is_plan_mode else "计划模式已退出",
            payload={
                "is_plan_mode": self.is_plan_mode,
                "session_id": str(getattr(self, "session_id", "") or ""),
                "workspace_dir": str(getattr(self, "workspace_dir", "") or ""),
            },
        )

    @property
    def is_plan_mode(self) -> bool:
        return self._tool_manager.is_plan_mode

    @property
    def plan_file_path(self) -> Path | None:
        return self._tool_manager._plan_file_path

    def _get_visible_tool_definitions(self) -> list[dict[str, Any]]:
        return self._tool_manager.get_visible_tool_definitions(self.identity.is_owner_context)

    def _get_tool_definitions_for_llm(self) -> list[dict[str, Any]]:
        if self._tool_definitions_override is not None:
            return self._tool_definitions_override
        return self._tool_manager.get_tool_definitions_for_llm(self.identity.is_owner_context)

    def register_tool(self, tool: BaseTool, *, replace: bool = False) -> None:
        self._tool_manager.register_tool(tool, replace=replace)
        self._invalidate_prompt_cache()

    def register_tools(self, tools: list[BaseTool]) -> None:
        self._tool_manager.register_tools(tools)

    async def bootstrap_tools(self, *, with_skill_tool: bool = True) -> None:
        from src.tools import register_builtin_tools
        from src.tools.builtin.skills.skills import SkillTool
        from src.tools.registry import tool_registry

        register_builtin_tools()
        self.register_tools(tool_registry.list_all())
        if with_skill_tool:
            self.register_tool(SkillTool(parent_coara=self))

    async def load_skills(self, only: list[str] | None = None) -> None:
        """Load discovered skills for the current workspace.

        When ``only`` is provided, restrict the loaded skills to those whose
        ``name`` appears in the list. ``None`` (default) loads all discovered
        skills, preserving existing behavior for Root/sub-agents.
        """
        await self.skill_manager.discover(self.workspace_dir, coara_home=resolve_coara_home(self.workspace_dir))
        all_skills = self.skill_manager.get_all()
        if only is None:
            self._skills = all_skills
        else:
            only_set = set(only)
            self._skills = [s for s in all_skills if s.name in only_set]
        logger.debug(f"Loaded {len(self._skills)} skills for {self.identity.name}")
        self._invalidate_prompt_cache()

    # 只有端输入注入（cli/web/matrix/cli-attached）的回合才推进空间 last-run；
    # janitor/后台/事件注入不进此集合，运行本身不更新 last-run。
    _LAST_RUN_USER_SOURCES = ("cli", "web", "matrix", "cli-attached")
    # 输出帧统一投递：diff 帧与正文 chunk 同走 EndRegistry 段路由。三端 sender
    # 已适配帧协议并各自渲染 diff（web TurnStream/前端 type=diff 帧、matrix
    # [COARA_DIFF]、attach diff 帧）；旧的分散消费（diff_bridge 订阅、
    # display_controller 暂存、trace_broadcast diff 推送）已剥离。
    _OUTPUT_FRAME_DIFF_ENABLED = True

    def _stamp_segment_model(self, seg: Any) -> None:
        """开段时记录该段注入端选定的模型，并更新空间 last-run（仅端输入）。

        segment 与 last-run 同一性质：段记「最近一次注入端」，last-run 记
        「最后一次端输入选定的模型」，都在输入注入时刻取会话当前模型。
        """
        seg.provider = str(getattr(self, "provider_name", "") or "")
        seg.model = str(getattr(self, "model_name", "") or "")
        if str(getattr(seg, "source", "") or "") not in self._LAST_RUN_USER_SOURCES:
            return
        self._record_last_run_model()

    def _record_last_run_model(self) -> None:
        """把当前实际使用的模型持久化为空间 last-run（无变化不写盘）。

        供重启加载与 janitor 读取：空间内核默认模型 = 最后一次正常对话
        实际运行的模型，恒有值（未对话过的空间由全局默认兜底）。
        """
        try:
            manager = getattr(self, "workspace_manager", None)
            if manager is None or getattr(manager, "registry", None) is None:
                return
            provider = str(getattr(self, "provider_name", "") or "").strip()
            model = str(getattr(self, "model_name", "") or "").strip()
            if not provider or not model:
                return
            workspace_dir = getattr(self, "workspace_dir", None)
            if not workspace_dir:
                return
            workspace_id = manager.match_path_to_workspace_id(Path(workspace_dir))
            if workspace_id is None:
                return
            from src.workspace.llm_binding import touch_entry_llm

            touch_entry_llm(manager, workspace_id, provider, model)
        except Exception:
            logger.debug("Failed to record workspace last-run model", exc_info=True)

    def switch_llm(self, provider_name: str, model: str | None = None, *, origin_source: str = "") -> tuple[str, str]:
        """Switch the active LLM connection for this agent (session-only, not persisted).

        任意 provider 之间可随意切换（含跨协议 driver），下一次 LLM 调用即生效。
        跨 driver 时历史思考内容（reasoning_content）按对方协议转出会 400 且对
        新模型无语义价值——切换即丢弃全部历史思考，对话内容与工具结果保留。
        ``origin_source``：发起端（web/matrix/cli-attached/缺省=CLI），随
        ``llm_switched`` 事件下发，供同空间其它端区分「自己切的」与「他端切的」。
        """
        from src.core.errors import ConfigError, ProviderNotFoundError
        from src.llm.registry import provider_registry

        if not provider_registry.has(provider_name):
            raise ProviderNotFoundError(provider_name)
        provider = provider_registry.get(provider_name)
        model_name = (model or provider.default_model or "").strip()
        if not model_name:
            raise ConfigError(f"Provider '{provider_name}' has no model specified")

        old_driver = getattr(self.provider, "driver", "")
        new_driver = getattr(provider, "driver", "")
        dropped_thinking = 0
        if old_driver and new_driver and old_driver != new_driver:
            # 跨协议切换：清空历史思考（过程产物，跨 provider 无语义），保留对话
            for msg in self.message_history:
                if getattr(msg, "reasoning_content", None):
                    msg.reasoning_content = None
                    dropped_thinking += 1
            self._invalidate_prompt_cache()

        self.provider = provider
        self.provider_name = provider_name
        self.model_name = model_name
        chrome: dict[str, Any] = {
            "provider": provider_name,
            "model": model_name,
            "cross_driver": bool(old_driver and new_driver and old_driver != new_driver),
            "dropped_thinking": dropped_thinking,
            "origin_source": origin_source,
            # attach 状态栏权威：勿让客户端回退本地 env 探测（会误显「添加 provider」）
            "provider_has_key": bool(str(getattr(provider, "api_key", "") or "").strip()),
        }
        with contextlib.suppress(Exception):
            from src.workspace.llm_binding import workspace_llm_chrome_fields

            chrome.update(workspace_llm_chrome_fields(self))
        with contextlib.suppress(Exception):
            ctx_window = int(self.provider.get_context_window(model_name) or 0)
            if ctx_window:
                chrome["context_window"] = ctx_window
        self._emit_trace(
            "llm_switched",
            f"Switched to {provider_name}/{model_name}",
            payload=chrome,
        )
        return provider_name, model_name

    def get_status(self) -> dict[str, Any]:
        from src.core.config import config_manager
        from src.core.error_log import resolve_error_log_path

        coara_home = config_manager.config.coara_home if config_manager._config else None
        errors_log_path = resolve_error_log_path(self.workspace_dir, coara_home=coara_home)
        return {
            "coara_id": self.identity.coara_id,
            "session_id": self.session_id,
            "audit_session_id": self.audit_session_id,
            "name": self.identity.name,
            "status": self.status.value,
            "provider": self.provider_name,
            "model": self.model_name,
            "message_count": len(self.message_history),
            "tools": list(self._tool_manager.tools.keys()),
            "skills": [s.name for s in self._skills],
            "tool_visibility": self._tool_manager.dump_visibility(self.identity.is_owner_context),
            "workspace_dir": str(self.workspace_dir),
            "coara_home": str(coara_home) if coara_home else None,
            "errors_log_path": str(errors_log_path),
        }

    async def shutdown(self) -> None:
        self.status = CoaraStatus.TERMINATED
        self._emit_trace("shutdown", "coara shut down")

    async def _wait_for_process_lock_release(self, *, timeout_seconds: float = 30.0) -> bool:
        """等 `_process_lock` 释放；返回 True 表示已释放，False 表示超时仍未释放。"""
        deadline = time.monotonic() + timeout_seconds
        while self._process_lock.locked():
            if time.monotonic() >= deadline:
                logger.warning("Timed out waiting for active turn to stop before new session")
                return False
            await asyncio.sleep(0.05)
        return True

    async def start_new_session(self, *, interrupt_source: str = "new_session") -> str:
        if self._inside_turn:
            # We're inside a turn (same event loop). Fire the abort signal so
            # the in-flight LLM call is cancelled, but defer the state clear
            # until the old turn has fully exited: the caller may share the
            # running turn's stack, so awaiting the process lock inline would
            # deadlock; and clearing immediately lets the dying turn keep
            # appending (tool results, interrupted-turn closure notes) into
            # the freshly cleared history of the new session.
            self.interrupt_current_turn("new_session", interrupt_source=interrupt_source)
            new_session_id = str(uuid.uuid4())
            task = asyncio.create_task(self._finish_new_session_after_turn_exit(new_session_id, interrupt_source))
            self._deferred_new_session_task = task
            return new_session_id
        if self.has_active_turn() or self._process_lock.locked():
            self.interrupt_current_turn("new_session", interrupt_source=interrupt_source)
            await self._wait_for_process_lock_release()
        async with self._process_lock:
            self._clear_session_state()
        await self._finalize_new_session(interrupt_source)
        return self.session_id

    async def _finish_new_session_after_turn_exit(self, session_id: str, interrupt_source: str) -> None:
        """Deferred inside-turn ``start_new_session``: reuse the same
        wait-lock-then-clear-under-lock protocol as the non-turn path once
        the aborted turn has released ``_process_lock``."""
        try:
            released = await self._wait_for_process_lock_release()
            if not released:
                # 旧回合无视 abort 存活：放弃清理（保留旧会话现场），否则延迟
                # 清理会挂在存活的旧回合之后，用户稍后发的新消息历史反被抹掉
                logger.warning(
                    "Deferred new-session cleanup abandoned: previous turn did not stop in time; "
                    "please retry /new after the turn settles"
                )
                return
            async with self._process_lock:
                self._clear_session_state(session_id=session_id)
            await self._finalize_new_session(interrupt_source)
        except Exception:
            logger.exception("Deferred new-session finalize failed")
        finally:
            # 无论成功、放弃还是异常，都必须解除延迟任务引用：它存活期间
            # process_message 持续拒绝新消息（"正在开始新会话"），不清理
            # 会话就永久锁死，用户再也无法重试 /new
            if self._deferred_new_session_task is not None and self._deferred_new_session_task.done():
                self._deferred_new_session_task = None

    async def _finalize_new_session(self, interrupt_source: str) -> None:
        """Post-clear new-session setup (vault seal, skills reload, seed)."""

        # New session boundary: seal the vault so the next turn does not retain
        # access to decrypted private data.
        vault_service = getattr(self, "vault_service", None)
        if vault_service is not None:
            try:
                vault_service.lock()
            except Exception:
                logger.exception("Failed to lock vault on new session")

        # Pick up dashboard edits to skills.default_include without a full restart.
        from src.core.config import get_config

        await get_config(force_reload=True)
        await self.load_skills()

        # Inject environment seed so the LLM has workspace context from the
        # very first turn. Without this, /new leaves message_history empty
        # and the environment context is missing until the next user turn.
        from src.coara.injections.environment_injector import build_environment_seed_messages

        seed = await asyncio.to_thread(build_environment_seed_messages, self.workspace_dir)
        self.message_history.extend(seed)
        await asyncio.to_thread(self.persist_session_to_disk)
        self._emit_trace(
            "session_started",
            "New session started",
            payload={
                "session_id": self.session_id,
                "interrupt_source": interrupt_source,
                # 空间定位：与 model_switched 同款，Matrix 订阅按手机视图空间过滤
                **workspace_llm_chrome_fields(self),
            },
        )

    def _apply_restored_state(
        self,
        session_id: str,
        message_history: list,
        *,
        restored_event_keys: list[str] | None = None,
        restored_event_seqs: list[int] | None = None,
        usage_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Apply restored session state (session id, history) and clear
        transient scratch space so the previous workspace's loop/shell state
        doesn't leak in.

        Shared by peer WorkspaceSession restore (``WorkspaceSession._restore_from_disk``).
        ``restored_event_keys/seqs`` 为事件投影对齐游标（恢复路径由投影器给出），
        用于把 recorder 的已记投影对齐到恢复后的历史，避免恢复后首次落盘全量重写。
        """
        self.session_id = session_id
        self.audit_session_id = session_id
        self._trace_emitter.session_id = session_id
        self.message_history = message_history
        # 先清瞬态（内含 _llm_usage_snapshot.clear()），再恢复 usage 快照，
        # 否则刚恢复的快照会被清掉
        self._reset_transient_state()
        if usage_snapshot:
            self._llm_usage_snapshot.restore(usage_snapshot)
        self._rebuild_session_log()
        recorder = getattr(self, "_session_log", None)
        if recorder is not None and restored_event_keys is not None:
            recorder.reset_projection(
                keys=restored_event_keys,
                seqs=restored_event_seqs or [],
            )

    def _reset_transient_state(self) -> None:
        """Clear per-turn/per-session scratch space without touching
        ``message_history``, ``session_id``, or ``_skill_session``.

        Used both by ``_clear_session_state`` (full reset, which additionally
        clears history + skills + regenerates session id) and by
        ``_apply_restored_state`` on RootCoara (which sets history + skills
        from persisted data but still needs a clean transient layer so stale
        loop/shell state from the previous workspace doesn't leak in).
        """
        if self._file_read_states:
            invalidate_read_caches_for_paths(self._file_read_states.keys())
            self._file_read_states.clear()
        self._llm_usage_snapshot.clear()
        self._tool_manager.clear_revealed()
        self.loop_detector.clear()
        self.shell_failure_streak_guard.clear()
        self._recent_progress_signatures.clear()
        # Cancel any orphaned foreground delegates from a previous turn/session.
        self.cancel_all_pending_foreground_delegates()
        # Clear buffered continuation inputs so done-callbacks from cancelled
        # delegates above don't inject stale messages into the next turn.
        self._continuation_inputs.clear()
        # Turn-scoped flags/debug scratch must not leak across sessions.
        self._inside_turn = False
        self._active_turn_source = ""
        self._llm_debug_turn_meta = None
        self._llm_debug_user_input = ""
        self._streamed_assistant_chars = 0
        self._final_deliver_message = None
        self._todo_park_message = None
        self._pending_llm_switch = None
        self._session_llm_call_count = 0
        self._session_tool_call_count = 0
        self._invalidate_prompt_cache()

    def is_flow_subject(self) -> bool:
        """True when this coara is the FlowRoot second subject (agent_kind=flow)."""
        return getattr(self, "_session_agent_kind", "") == "flow"

    def _rebuild_session_log(self) -> None:
        """按当前 session_id 重建会话事件记录器（唯一事实源，无开关）；失败静默为 None。

        录像带跟主体不跟执行目录：主会话→工作空间带；FlowRoot 与会话内 flow
        节点→工作流系统带；引擎节点→引擎系统带（跨进程隔离）。
        """
        self._session_log = None
        try:
            from src.session_log.recorder import build_recorder
            from src.session_log.store import workflow_session_log_path

            log_path = None
            tape = getattr(self, "_session_tape", "")
            if self.is_flow_subject() or tape == "flow":
                log_path = workflow_session_log_path("flow", coara_home=self._session_state_coara_home())
            elif tape == "engine":
                log_path = workflow_session_log_path("engine", coara_home=self._session_state_coara_home())

            self._session_log = build_recorder(
                workspace_dir=self.workspace_dir,
                session_id=self.session_id,
                coara_id=self.identity.coara_id,
                coara_name=self.identity.name,
                agent_kind=getattr(self, "_session_agent_kind", "") or "main",
                coara_home=self._session_state_coara_home(),
                log_path=log_path,
            )
        except Exception:
            logger.exception("Session log recorder init failed for {}", self.workspace_dir)
            self._session_log = None

    def _clear_session_state(self, *, session_id: str | None = None) -> None:
        self._reset_transient_state()
        self._skill_session.clear()
        self._invalidate_prompt_cache()
        self.message_history.clear()
        self.session_id = session_id or str(uuid.uuid4())
        self.audit_session_id = self.session_id
        self._trace_emitter.session_id = self.session_id
        # /new 换了 session_id：事件记录器必须跟着重绑（新会话投影从零开始），
        # 否则事件挂到旧会话下
        self._rebuild_session_log()

    async def _purge_legacy_audit_logs(self) -> None:
        """Remove deprecated session tool-audit directories for this workspace."""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        from src.core.config import config_manager
        from src.core.error_log import purge_session_audit_logs

        coara_home = config_manager.config.coara_home if config_manager._config else None
        purge_session_audit_logs(workspace_dir=self.workspace_dir, coara_home=coara_home)

    @property
    def _trace_sink(self) -> Any:
        return self._trace_emitter.sink

    def set_trace_sink(self, sink: Any) -> None:
        self._trace_emitter.sink = sink

    def _emit_trace(
        self, event_type: str, message: str, *, payload: dict[str, Any] | None = None, level: str = "info"
    ) -> None:
        """Thin wrapper delegating to the trace emitter."""
        if self.is_flow_subject():
            origin_scope = "flow_loop"
        elif self.identity.user_facing:
            origin_scope = "main_loop"
        else:
            origin_scope = "subagent_loop"
        # 统一补当前回合 source：工具/收尾等事件此前常无 payload，发送端按端
        # 过滤会把无 source 的 completed 丢掉 → attach CLI spinner 转不停。
        # 显式给了 source 的不覆盖（user_message 等自行指定）。
        turn_source = str(getattr(self, "_active_turn_source", "") or "").strip()
        if turn_source:
            if payload is None:
                payload = {"source": turn_source}
            elif not str(payload.get("source") or "").strip():
                payload = {**payload, "source": turn_source}
        # 多 CLI attach：补发起连接 channel_id，trace 只回投该连接（各端独立）。
        try:
            from src.coara.turn_context import get_turn_channel_id

            ch = str(get_turn_channel_id() or "").strip()
        except Exception:
            ch = ""
        if ch:
            if payload is None:
                payload = {"channel_id": ch}
            elif not str(payload.get("channel_id") or "").strip():
                payload = {**payload, "channel_id": ch}
        self._trace_emitter.emit(event_type, message, payload=payload, level=level, origin_scope=origin_scope)

    def _session_state_coara_home(self) -> Path | None:
        """Resolve coara_home for workspace session files (state/history/marker)."""
        from src.core.config import config_manager

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager._config.coara_home
        wm = getattr(self, "workspace_manager", None)
        if wm is not None and getattr(wm, "coara_home", None) is not None:
            coara_home = wm.coara_home
        return coara_home

    def _mark_turn_in_flight(self, turn_id: str) -> None:
        """Write the turn in-flight marker (atomic small file).

        与 ``persist_session_to_disk`` 同门禁：只有拥有工作空间会话文件的
        会话才写标记。标记写入失败不得阻断回合（仅降级为恢复时无注记）
        Flow 写独立 ``flow_turn_in_flight.json``，不覆盖主会话标记。
        """
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import mark_flow_turn_in_flight, mark_turn_in_flight

            if self.is_flow_subject():
                mark_flow_turn_in_flight(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                    turn_id=turn_id,
                )
            else:
                mark_turn_in_flight(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                    turn_id=turn_id,
                )
        except Exception:
            logger.debug("Failed to mark turn in-flight for {}", self.workspace_dir, exc_info=True)

    def _clear_turn_in_flight(self) -> None:
        """Clear the turn in-flight marker after the turn's history is persisted."""
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import clear_flow_turn_in_flight, clear_turn_in_flight

            if self.is_flow_subject():
                clear_flow_turn_in_flight(
                    self.workspace_dir,
                    coara_home=self._session_state_coara_home(),
                )
            else:
                clear_turn_in_flight(
                    self.workspace_dir,
                    coara_home=self._session_state_coara_home(),
                )
        except Exception:
            logger.debug("Failed to clear turn in-flight marker for {}", self.workspace_dir, exc_info=True)

    def persist_session_to_disk(self) -> None:
        """落盘会话状态：事件日志同步（唯一事实源）+ 会话索引。

        消息事件经 ``sync_history`` 前缀对账写入事件日志；usage 快照走
        ``session/meta`` 事件。session_history.json 快照线已删除。
        Safe to call from worker threads (blocking disk IO). Owner/user-facing
        sessions only — ephemeral subagents do not own workspace session files.

        Flow 第二主体：写同一条 ``session_events.jsonl``，但索引走
        ``flow_session_state.json``，**禁止**写主会话 ``session_state.json``。
        """
        if not (self.identity.user_facing or self.identity.is_owner_context):
            return
        try:
            from src.coara.workspace_state import save_flow_session_state, save_session_state

            # janitor 是附着在每个用户空间上的维护机制（拿目标空间上下文跑维护回合），
            # 只同步录像带，不占有会话索引：写了 session_state 会顶掉主会话索引、把
            # last_updated 推新，启动补扫据此判定「会话有更新」而反复触发自己（死循环）。
            # daily 不在此列——它已是独立的 internal 系统空间主体（user_facing=True，
            # 自己的 workspace_dir），该写自己的 session_state（自己的会话索引与录像带）。
            persona_name = str(getattr(self.identity.persona, "name", "") or "").strip().lower()
            if persona_name == "janitor":
                pass
            elif self.is_flow_subject():
                save_flow_session_state(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                )
            else:
                save_session_state(
                    self.workspace_dir,
                    self.session_id,
                    coara_home=self._session_state_coara_home(),
                )
        except Exception:
            logger.exception("Failed to persist session state for {}", self.workspace_dir)
        recorder = getattr(self, "_session_log", None)
        if recorder is None:
            return
        try:
            recorder.sync_history(list(self.message_history))
            recorder.record_session_meta({"usage_snapshot": self._llm_usage_snapshot.to_dict()})
        except Exception:
            logger.exception("Failed to sync session event log for {}", self.workspace_dir)

    def _serialize_messages_for_trace(self, messages: list[Message]) -> list[dict[str, Any]]:
        return serialize_messages_for_trace(messages)

    def __repr__(self) -> str:
        return f"coaraBase(name={self.identity.name!r}, status={self.status.value})"
