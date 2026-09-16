"""FlowRoot：WebUI 工作流示例空间的专属会话主体（第二主体）。

与主会话（root / WorkspaceSession）完全并立：
- 独立 system prompt（``prompts/agents/flow-root.md``）与独立 session_id
- 同一工作空间同一条录像带 ``session_events.jsonl``，``agent_kind=flow``
- 索引 ``flow_session_state.json``（禁止写主会话 ``session_state.json``）
- orchestrator 常驻；实例级 FlowCoordinator（flow_subject="flow"）
- 由 WebServer 惰性创建并持有；图在进程内跨空间保留，workspace_dir 跟随前台
  （新 spawn 默认工作目录）；会话事件写入当前前台空间的录像带

工作流以「示例工作空间」身份回归空间体系：与其它系统空间同构（独立 persona +
专属画布页面），是出厂自带的参考实现——用户可照此范式构建自己的空间
（自定义 schema、独立引擎、内核工具、专属页面）。执行层不在此：WDL 实例
运行由独立 wdl 软件承载。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.events import TraceEvent
from src.core.logger import logger
from src.core.types import CoaraPersona

if TYPE_CHECKING:
    from src.coara.root import RootCoara


_FLOW_ORCHESTRATOR_PREFIX = (
    "工作流页面实时编排工具：每次 spawn / update / edge 立即在右侧画布渲染节点与连线，"
    "建图过程实时可见，图成形后随时可 run 点火运行。\n\n"
)

_FALLBACK_PROMPT = (
    "你是 WebUI 工作流页面专属的构建助手（FlowRoot），唯一目标是把 agentic 工作流建出来、调通、固化。中文交流。"
)


def _load_flow_root_prompt() -> tuple[str, str, Any | None]:
    """从 AgentRegistry 加载 flow-root 提示词（与 root 同目录配对 yaml+md）。"""
    from src.prompt.agent_registry import AgentRegistry

    registry = AgentRegistry()
    defn = registry.get_agent("flow-root")
    if defn is None:
        logger.warning("flow-root agent prompt missing; using fallback")
        return "flow-root", _FALLBACK_PROMPT, None
    return defn.name, defn.system_prompt_template, defn.yaml_config


def bind_flow_workspace(coara: Any, workspace_dir: Path | str) -> None:
    """前台空间切换时：更新 FlowRoot 工作目录（新 spawn 节点的默认 cwd）。

    录像带不重绑：工作流主体的带是系统级单带（workflows/session_events.jsonl），
    不随前台空间迁移——历史不再散落各空间。
    """
    path = Path(workspace_dir).expanduser().resolve()
    current = Path(coara.workspace_dir).expanduser().resolve()
    if current == path:
        return
    coara.workspace_dir = path
    if getattr(coara, "identity", None) is not None:
        coara.identity.workspace_dir = path


def _restore_flow_session(coara: Any, workspace_dir: Path, *, coara_home: Path | None) -> None:
    """冷启动：从当前工作空间的 flow 索引 + 同条录像带恢复历史。"""
    from src.coara.workspace_state import recover_flow_session, replay_session_projection

    recovered = recover_flow_session(
        workspace_dir,
        coara_home=coara_home,
        label="flow-root",
        keep_session_id_on_replay_failure=True,
    )
    if recovered is None:
        return
    last_sid, history = recovered
    try:
        projection = replay_session_projection(workspace_dir, last_sid, coara_home=coara_home)
    except Exception:
        projection = None

    if projection is not None:
        from src.session_log.recorder import message_key as _mk

        # 游标始终对齐投影本体：recover 可能追加中断注记使 history 不等长，
        # 不喂游标会让首次落盘把整段历史重复写盘（见 workspace_session 同款修复）
        coara._apply_restored_state(
            last_sid,
            history,
            restored_event_keys=[_mk(m) for m in projection.messages],
            restored_event_seqs=list(projection.seqs),
            usage_snapshot=projection.usage_snapshot,
        )
        return
    coara._apply_restored_state(last_sid, history)


def switch_flow_draft_session(coara: Any, draft_id: str, *, coara_home: Path | None) -> bool:
    """把构建对话切到指定草案的专属会话（每草案一个 section 一个上下文）。

    - 已绑定且就是当前会话：只刷新 last_active，不动状态
    - 有绑定：持久化当前会话后从系统带按 session_id 重放恢复
    - 无绑定：开全新会话并建立绑定
    回合进行中不切换（本会话的内容属于旧草案上下文）。返回是否发生了切换。
    """
    import uuid

    from src.coara.workspace_state import replay_session_projection
    from src.session_log.store import workflow_session_log_path
    from src.workflow import draft_sessions

    draft_id = (draft_id or "").strip()
    if not draft_id:
        return False
    bound = draft_sessions.session_for(draft_id, coara_home)
    if bound and bound == coara.session_id:
        draft_sessions.mark_active(draft_id, coara_home)
        return False
    if coara.has_active_turn():
        logger.info(f"flow draft session switch to {draft_id} skipped: turn active")
        return False

    coara.persist_session_to_disk()
    if bound:
        tape = workflow_session_log_path("flow", coara_home=coara_home)
        projection = replay_session_projection(
            coara.workspace_dir,
            bound,
            coara_home=coara_home,
            log_path=tape,
        )
        history = list(projection.messages)
        from src.session_log.recorder import message_key as _mk

        # history 直接取自投影，游标无条件对齐（len(keys)==len(history) 恒真，原分支是死代码）
        coara._apply_restored_state(
            bound,
            history,
            restored_event_keys=[_mk(m) for m in history],
            restored_event_seqs=list(projection.seqs),
            usage_snapshot=projection.usage_snapshot,
        )
    else:
        coara._apply_restored_state(str(uuid.uuid4()), [])

    draft_sessions.bind_session(draft_id, coara.session_id, coara_home)
    draft_sessions.mark_active(draft_id, coara_home)
    # 概况指纹复位：下一条用户消息注入新草案的画布概况
    coara._flow_draft_overview_fp = ""
    logger.info(f"FlowRoot switched to draft {draft_id} (session={coara.session_id})")
    return True


async def create_flow_root(root_coara: RootCoara) -> Any:
    """为 WebUI 工作流页面创建全局 FlowRoot 会话主体（CoaraBase）。

    全局常驻：不随前台工作空间切换而重建，会话内图跨空间保留。构造时取
    当前前台工作空间作为初始 workspace_dir（新 spawn 节点的默认工作目录，
    之后由 WebServer 跟随前台空间同步）。

    轻量构造（不跑 initialize_root_services）：共享 Root 的 provider /
    workspace_manager / event_bus；工具面 = 进程级无状态工具 + runtime 工具
    + 常驻 orchestrator + 挂起工具入口。历史落盘 source=web-flow 由
    WebServer 调 process_message(source=...) 指定；会话事件 agent_kind=flow。
    """
    from src.coara.base import CoaraBase
    from src.coara.runtime_tools import register_runtime_tools

    fg = root_coara.foreground_coara
    workspace_dir = Path(fg.workspace_dir).expanduser().resolve()
    coara_home = None
    wm = getattr(root_coara, "workspace_manager", None)
    if wm is not None and getattr(wm, "coara_home", None) is not None:
        coara_home = wm.coara_home

    agent_name, prompt, yaml_config = _load_flow_root_prompt()
    persona = CoaraPersona(
        name=agent_name,
        role="工作流构建教练",
        expertise_areas=["workflow orchestration", "debugging", "automation"],
        system_prompt_template=prompt,
        yaml_config=yaml_config,
    )
    coara = CoaraBase(
        name="flow-root",
        persona=persona,
        workspace_dir=workspace_dir,
        provider=fg.provider,
        provider_name=fg.provider_name,
        model=fg.model_name,
        user_facing=True,
        is_owner_context=True,
        max_tool_iterations=600,
        session_agent_kind="flow",
    )
    # 主体标记：flow 图事件（flow_graph_changed）据此路由到 WebUI 工作台画布
    coara.flow_subject = "flow"  # type: ignore[attr-defined]
    # Root 回链：草案概况注入经 root._web_server.active_workflow_draft_id
    # 拿当前打开的草案（用户盯着的那块画布）
    coara._root_coara = root_coara  # type: ignore[attr-defined]
    # 共享 workspace_manager（VFS resolver）
    coara.workspace_manager = root_coara.workspace_manager
    # workspace_manager 在建构之后才挂上：录像带按最终 home 重解一次
    # （工作流系统带与工作空间无关，生产上同一 home 是原地重绑）
    coara._rebuild_session_log()

    # 进程级无状态工具（web_fetch / task / …）
    from src.tools import register_builtin_tools
    from src.tools.registry import tool_registry

    register_builtin_tools()
    coara.register_tools(tool_registry.list_all())

    # runtime 工具（文件 / shell / todo / delegate 等）
    register_runtime_tools(coara)

    # orchestrator 常驻：同一 OrchestratorTool，实例级覆盖挂起标记与描述前缀
    from src.tools.builtin.orchestrator.orchestrator import _ORCHESTRATOR_DESCRIPTION, OrchestratorTool

    tool = OrchestratorTool(parent_coara=coara)
    tool.should_defer = False
    tool.description = _FLOW_ORCHESTRATOR_PREFIX + _ORCHESTRATOR_DESCRIPTION
    coara.register_tool(tool, replace=True)

    try:
        from src.tools.builtin.integration.tool import ToolGatewayTool

        coara.register_tool(ToolGatewayTool(parent_coara=coara), replace=True)
    except Exception as exc:
        logger.warning(f"Tool gateway registration failed for flow-root: {exc}")

    root_bus = root_coara.event_bus

    def _flow_trace_sink(event: TraceEvent) -> None:
        tagged = dict(event.payload or {})
        tagged["subject"] = "flow"
        root_bus.publish(
            TraceEvent(
                coara_id=event.coara_id,
                coara_name=event.coara_name,
                event_type=event.event_type,
                message=event.message,
                level=event.level,
                timestamp=event.timestamp,
                payload=tagged,
            )
        )

    coara.set_trace_sink(_flow_trace_sink)

    # 冷启动恢复：同工作空间录像带中 agent_kind=flow 的会话
    try:
        _restore_flow_session(coara, workspace_dir, coara_home=coara_home)
    except Exception:
        logger.exception("FlowRoot session restore failed (workspace=%s)", workspace_dir)

    logger.info(f"FlowRoot created for workspace {workspace_dir} (session={coara.session_id})")
    return coara
