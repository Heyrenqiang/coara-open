"""janitor 维护流程本体——内核固化维护管道，不是子智能体"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

from src.core.coara_home import workspace_id_for
from src.core.logger import logger
from src.core.types import CoaraPersona, Message

# 维护回合工具迭代上限（对齐 delegate 的 SUBAGENT_MAX_TOOL_ITERATIONS）。
_JANITOR_MAX_TOOL_ITERATIONS = 1500


def _janitor_config() -> Any | None:
    """加载 janitor 注入提醒（persona 骨架 + janitor.md 职责说明）"""
    from src.coara.builtin_agents import SubAgentConfig
    from src.prompt.yaml_loader import YamlPromptLoader

    prompts_dir = Path(__file__).resolve().parent / "prompts" / "injections"
    md_path = prompts_dir / "janitor.md"
    yaml_path = prompts_dir / "janitor.yaml"
    try:
        md_body = md_path.read_text(encoding="utf-8").strip()
        if not md_body:
            return None
        config = YamlPromptLoader().load(yaml_path)
        prefix = config.system_prompt.strip()
        config.system_prompt = f"{prefix}\n\n{md_body}" if prefix else md_body
        return SubAgentConfig(
            name="janitor",
            role=config.role or "janitor",
            description=config.when_to_use or "",
            system_prompt=config.system_prompt,
            tools=[],
            yaml_config=config,
        )
    except Exception as exc:
        logger.warning(f"Failed to load janitor config: {exc}")
        return None


def _parent_coara(root: Any, workspace_dir: str) -> Any | None:
    """取目标空间会话的 coara（父会话）；未加载返回 None。"""
    live = getattr(root, "_sessions", {}).get(workspace_id_for(workspace_dir))
    if live is None:
        return None
    return getattr(live, "coara", None)


def _target_static_prompt(root: Any, workspace_dir: str, cfg: Any) -> tuple[str, Any]:
    """取目标空间会话的静态系统提示词作为 janitor 的 persona 模板"""
    from dataclasses import replace

    template = cfg.system_prompt
    yaml_config = cfg.yaml_config
    parent = _parent_coara(root, workspace_dir)
    static: str | None = None
    if parent is not None:
        cache = getattr(parent, "_static_prompt_cache", None)
        static = cache.get("static") if isinstance(cache, dict) else None
        if not static:
            try:
                parent._build_system_prompt()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"janitor static prompt: parent build failed, fallback to janitor.yaml: {exc}")
                static = None
            else:
                cache = getattr(parent, "_static_prompt_cache", None)
                static = cache.get("static") if isinstance(cache, dict) else None
    if static:
        template = static
        if yaml_config is not None:
            yaml_config = replace(yaml_config, system_prompt=static)
    return template, yaml_config


def _build_janitor_coara(
    root: Any,
    *,
    workspace_dir: str,
    provider: str | None,
    model: str | None,
) -> Any | None:
    """装配 janitor 维护主体：临时 CoaraBase 实例，无 sa- 身份、不进台账。

    工具表与目标空间父会话一致（prompt cache 前缀）；无活父会话时按主会话标准装载。
    """
    from src.coara.base import CoaraBase

    cfg = _janitor_config()
    if cfg is None:
        logger.error("janitor config missing")
        return None

    # 前缀一致性：janitor 没有自己的系统提示词，模板取目标空间会话的静态提示词
    template, yaml_config = _target_static_prompt(root, workspace_dir, cfg)

    janitor = CoaraBase(
        name="janitor",
        persona=CoaraPersona(
            name="janitor",
            role=cfg.role,
            system_prompt_template=template,
            yaml_config=yaml_config,
        ),
        workspace_dir=Path(workspace_dir),
        provider_name=provider or None,
        model=model or None,
        user_facing=False,
        # record 等 owner_only；is_owner_context=True 才会挂进 LLM 可见表。
        is_owner_context=True,
        # is_owner_context 会让 base 默认 agent_kind=main，导致事件被 hydrate 进
        # web 聊天区；显式 subagent 让投影层继续排除（产出另经 record/ws.md 留空间）。
        session_agent_kind="subagent",
        max_tool_iterations=_JANITOR_MAX_TOOL_ITERATIONS,
    )
    janitor.workspace_manager = getattr(root, "workspace_manager", None)

    _seed_session_like_tools(janitor, root)
    _align_tools_exactly_to_parent(janitor, root, workspace_dir)
    return janitor


def _seed_session_like_tools(janitor: Any, root: Any) -> None:
    """按主会话标准装载工具（无专用白名单；含常驻 record）。"""
    from src.coara.runtime_tools import register_runtime_tools
    from src.coara.tool_registry import register_root_scoped_tools
    from src.tools import register_builtin_tools
    from src.tools.registry import tool_registry

    register_builtin_tools()
    janitor.register_tools(tool_registry.list_all())
    register_runtime_tools(janitor)
    register_root_scoped_tools(janitor, ws_parent=root)

    try:
        from src.tools.builtin.integration.tool import ToolGatewayTool

        janitor.register_tool(ToolGatewayTool(parent_coara=janitor), replace=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"janitor tool gateway skipped: {exc}")

    _bind_records_tools(root, janitor)
    _mirror_root_optional_tools(janitor, root)


def _bind_records_tools(root: Any, janitor: Any) -> None:
    """绑定 record + local_search（与 WorkspaceSession 同 defer 默认）。"""
    store = getattr(root, "records_store", None)
    if store is None:
        return
    from src.tools.builtin.records.local_search import LocalSearchTool
    from src.tools.builtin.records.record import RecordTool

    janitor.register_tool(RecordTool(store=store, parent_coara=janitor), replace=True)
    janitor.register_tool(LocalSearchTool(store=store, parent_coara=janitor), replace=True)


def _mirror_root_optional_tools(janitor: Any, root: Any) -> None:
    """镜像 Root 上的 reminder / send_file（与 WorkspaceSession 对齐）。"""
    if getattr(root, "reminder_service", None) is not None:
        try:
            from src.tools.builtin.manifest import REMINDER_TOOL_TYPE

            janitor.register_tool(REMINDER_TOOL_TYPE(root.reminder_service), replace=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"janitor reminder tool skipped: {exc}")

    root_tm = getattr(root, "_tool_manager", None)
    if root_tm is None or "send_file" not in root_tm.tools:
        return
    try:
        from src.tools.builtin.integration.outbound_file import OutboundFileRouter
        from src.tools.builtin.integration.send_file import SendFileTool

        root_send = root_tm.tools["send_file"]
        bridge = getattr(root_send, "_bridge", None)
        if bridge is None:
            bridge = getattr(root, "_outbound_file_router", None)
        if isinstance(bridge, OutboundFileRouter):
            janitor.register_tool(
                SendFileTool(bridge=bridge, workspace_root=Path(janitor.workspace_dir)),
                replace=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"janitor send_file skipped: {exc}")


def _align_tools_exactly_to_parent(janitor: Any, root: Any, workspace_dir: str) -> None:
    """工具表 ≡ 父会话：可执行 keys 与 LLM 定义均对齐；一个不加、一个不减。

    无活父会话时保留 `_seed_session_like_tools` 结果（与切入后主会话装载同构）。
    """
    parent = _parent_coara(root, workspace_dir)
    if parent is None:
        return

    parent_tm = getattr(getattr(parent, "_tool_manager", None), "tools", None)
    if not isinstance(parent_tm, dict) or not parent_tm:
        return

    janitor_tm = janitor._tool_manager.tools
    parent_names = set(parent_tm.keys())

    for name in list(janitor_tm.keys()):
        if name not in parent_names:
            janitor_tm.pop(name, None)

    store = getattr(root, "records_store", None)
    for name, tool in parent_tm.items():
        if name == "record" and store is not None:
            from src.tools.builtin.records.record import RecordTool

            janitor.register_tool(RecordTool(store=store, parent_coara=janitor), replace=True)
            continue
        if name == "local_search" and store is not None:
            from src.tools.builtin.records.local_search import LocalSearchTool

            defer = bool(getattr(tool, "should_defer", True))
            janitor.register_tool(
                LocalSearchTool(store=store, parent_coara=janitor, defer=defer),
                replace=True,
            )
            continue
        if name in janitor_tm:
            continue
        # 父有、janitor 缺：共享实例（同空间路径解析）；避免漏装导致执行失败。
        janitor_tm[name] = tool

    try:
        parent_defs = parent._get_tool_definitions_for_llm()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"janitor tools align: parent defs unavailable: {exc}")
        return
    if not parent_defs:
        return
    janitor._tool_definitions_override = copy.deepcopy(parent_defs)
    logger.debug(f"janitor tools aligned to parent ({len(parent_defs)} defs, {len(janitor_tm)} tools)")


def _load_target_history(
    root: Any,
    workspace_dir: str,
    coara_home: str,
    history_snapshot: list[Message] | None = None,
) -> list[Message]:
    """喂历史：外部快照（压缩前上下文）优先，其次活会话深拷贝，未加载走磁盘重放"""
    if history_snapshot:
        return copy.deepcopy(history_snapshot)
    workspace_id = workspace_id_for(workspace_dir)
    live = getattr(root, "_sessions", {}).get(workspace_id)
    live_coara = getattr(live, "coara", None) if live is not None else None
    live_history = getattr(live_coara, "message_history", None)
    if live_history:
        return copy.deepcopy(live_history)

    try:
        from src.coara.workspace_state import recover_session

        rec = recover_session(Path(workspace_dir), coara_home=Path(coara_home))
        if rec:
            _, history, _ = rec
            if history:
                return list(history)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"janitor history recover failed for {workspace_dir}: {exc}")
    return []


def _ensure_env_seed(history: list[Message], workspace_dir: str) -> list[Message]:
    """无环境种子时前置（对齐 WorkspaceSession._restore_from_disk 的行为）。"""
    if history:
        return history
    try:
        from src.coara.injections.environment_injector import build_environment_seed_messages

        return [*build_environment_seed_messages(workspace_dir), *history]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"janitor env seed skipped for {workspace_dir}: {exc}")
        return history


async def _drain_maintenance_turn(
    janitor: Any,
    prompt: str,
    workspace_dir: str,
    coara_home: str,
    *,
    source: str,
) -> None:
    """跑一轮维护回合；过程事件落目标空间维护带（与主带分离），不写主录像带。"""
    _ = (workspace_dir, coara_home)
    # source 沿用目标会话最近输入端（调用方解析），兜底 cli-attached。
    stream = janitor.process_message(
        prompt,
        trust_level="owner",
        show_tool_summary=False,
        source=source,
    )
    async for _ in stream:
        pass


def run_janitor_maintenance(
    root: Any,
    *,
    workspace_name: str,
    workspace_dir: str,
    coara_home: str,
    prompt: str,
    history_snapshot: list[Message] | None = None,
) -> asyncio.Task | None:
    """创建一个 janitor 维护回合任务，返回 asyncio.Task（供单飞/完成钩子挂钩）"""
    provider, model = _resolve_janitor_llm(root, workspace_dir)
    janitor = _build_janitor_coara(root, workspace_dir=workspace_dir, provider=provider, model=model)
    if janitor is None:
        return None

    history = _load_target_history(root, workspace_dir, coara_home, history_snapshot=history_snapshot)
    janitor.message_history = _ensure_env_seed(history, workspace_dir)

    # source 沿用目标会话最近输入端（source 只接受三端值），会话不在内存或从未输入时兜底 cli-attached
    workspace_id = workspace_id_for(workspace_dir)
    live = getattr(root, "_sessions", {}).get(workspace_id)
    live_coara = getattr(live, "coara", None) if live is not None else None
    _source = str(getattr(live_coara, "_last_user_input_source", "") or "").strip() or "cli-attached"

    async def _run() -> None:
        try:
            await _drain_maintenance_turn(janitor, prompt, workspace_dir, coara_home, source=_source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"janitor maintenance turn failed for {workspace_name}: {exc}")
            raise

    task = asyncio.create_task(_run(), name=f"janitor-{workspace_name}")
    return task


def _resolve_janitor_llm(root: Any, workspace_dir: str) -> tuple[str | None, str | None]:
    """provider/model：空间 last-run（entry 绑定 = 最后正常对话运行的模型）"""
    from src.coara.workspace_protocol import _workspace_entry_llm

    return _workspace_entry_llm(root, workspace_dir)
