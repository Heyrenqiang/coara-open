"""FlowRoot：WebUI 工作流示例空间的专属会话主体（第二主体）"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


_FLOW_ORCHESTRATOR_PREFIX = (
    "工作流页面实时编排工具：每次 spawn / update / edge 立即在右侧画布渲染节点与连线，"
    "建图过程实时可见，图成形后随时可 run 点火运行。\n\n"
)


def bind_flow_workspace(coara: Any, workspace_dir: Path | str) -> None:
    """前台空间切换时：更新 FlowRoot 工作目录（新 spawn 节点的默认 cwd）"""
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
    """把构建对话切到指定草案的专属会话（每草案一个 section 一个上下文）"""
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

    公共装配段复用 module_root.build_module_subject；flow 专属段：
    orchestrator 常驻、录像带按最终 home 重解、会话冷恢复。
    """
    from src.coara.module_root import build_module_subject

    coara = await build_module_subject(
        root_coara,
        name="flow-root",
        agent_name="flow-root",
        role="工作流构建教练",
        expertise_areas=["workflow orchestration", "debugging", "automation"],
        subject="flow",
        session_agent_kind="flow",
    )
    # 主体标记：flow 图事件（flow_graph_changed）据此路由到 WebUI 工作台画布
    coara.flow_subject = "flow"  # type: ignore[attr-defined]
    # Root 回链：草案概况注入经 root._web_server.active_workflow_draft_id
    # 拿当前打开的草案（用户盯着的那块画布）
    coara._root_coara = root_coara  # type: ignore[attr-defined]
    # workspace_manager 在建构之后才挂上：录像带按最终 home 重解一次
    # （工作流系统带与工作空间无关，生产上同一 home 是原地重绑）
    coara._rebuild_session_log()

    # orchestrator 常驻：同一 OrchestratorTool，实例级覆盖挂起标记与描述前缀
    from src.tools.builtin.orchestrator.orchestrator import _ORCHESTRATOR_DESCRIPTION, OrchestratorTool

    tool = OrchestratorTool(parent_coara=coara)
    tool.should_defer = False
    tool.description = _FLOW_ORCHESTRATOR_PREFIX + _ORCHESTRATOR_DESCRIPTION
    coara.register_tool(tool, replace=True)

    # 冷启动恢复：同工作空间录像带中 agent_kind=flow 的会话
    coara_home = None
    wm = getattr(root_coara, "workspace_manager", None)
    if wm is not None and getattr(wm, "coara_home", None) is not None:
        coara_home = wm.coara_home
    workspace_dir = Path(coara.workspace_dir).expanduser().resolve()
    try:
        _restore_flow_session(coara, workspace_dir, coara_home=coara_home)
    except Exception:
        logger.exception("FlowRoot session restore failed (workspace=%s)", workspace_dir)

    logger.info(f"FlowRoot created for workspace {workspace_dir} (session={coara.session_id})")
    return coara
