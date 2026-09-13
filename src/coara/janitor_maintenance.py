"""janitor 维护流程本体——内核固化维护管道，不是子智能体。

到点/触发事件后，内核直接按既定流程跑一遍：
读目标空间会话历史 → 跑一轮 LLM 维护回合（process_message 信封含 janitor.md）→
写 ws.md 概况 / record → 落完成标记。

跑的过程像会话（一个完整 ReAct 回合，多轮 LLM + 工具调用），但 janitor 没有
智能体身份：无 sa- id、不进子智能体台账、不占目标会话 turn 队列、不回投父会话、
不进 web 活动区。载体是临时 CoaraBase 实例（模仿 module_root.create_module_root），
不经过 delegate，天然避开全部子智能体语义。

工具表契约：与目标空间父会话完全一致（一个不加、一个不减），保证 prompt cache
前缀一致。主会话常驻 record；janitor 不再维护专用白名单，也不再挂 review。

录像带归属（过程带与主带分离）：
- janitor 回合的详细事件流不写进目标空间主录像带 session_events.jsonl，避免污染
  用户会话、避免磁盘重放把维护过程当空间历史重放；过程记录落独立维护带
  janitor_events.jsonl，供独立排查。
- janitor 的产出留目标空间：ws.md 更新、record 写入、
  janitor_activity.json 完成标记——这些在目标空间数据面，用户可见。
"""

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
    """加载 janitor 注入提醒（persona 骨架 + janitor.md 职责说明）。

    janitor 不是 agent（无独立 persona，接着父会话上下文跑）——它的 md+yaml 是
    「注入提醒」，与 compression.md 同性质，统一放
    src/coara/prompts/injections/（注入提示词目录），不经 agents 注册表。
    工具表不走 yaml 白名单，装配时与父会话对齐。
    """
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
    """取目标空间会话的静态系统提示词作为 janitor 的 persona 模板。

    与旧 delegate 路径同语义（已退役）：persona 同源则
    base 内部提示词缓存失效后重建多少次都一致（播种 cache 会被 register_tool
    清掉、重建回退成 janitor.md）。读父会话只读缓存，缺失时触发一次构建再读；
    仍缺则回退 janitor.yaml 原配置。
    """
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
    # （长前缀一致——骨架/技能段与目标会话同源，利于 provider 缓存命中）。
    # janitor.md 不进系统提示词，随本轮 process_message 的 <系统提醒> 信封一次带齐。
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
    """镜像 Root 上的 vault / reminder / send_file（与 WorkspaceSession 对齐）。"""
    if getattr(root, "vault_service", None) is not None:
        try:
            from src.tools.builtin.vault.vault import VaultTool

            janitor.vault_service = root.vault_service
            janitor.register_tool(VaultTool(parent_coara=janitor), replace=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"janitor vault tool skipped: {exc}")
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
    """喂历史：外部快照（压缩前上下文）优先，其次活会话深拷贝，未加载走磁盘重放。

    ``history_snapshot`` 由压缩派发链传入：压缩刚把活会话历史替换成摘要，
    此时沉淀必须基于压缩前的完整上下文才有意义——快照优先级最高。
    """
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


async def _drain_maintenance_turn(janitor: Any, prompt: str, workspace_dir: str, coara_home: str) -> None:
    """跑一轮维护回合；过程事件落目标空间维护带（与主带分离），不写主录像带。"""
    _ = (workspace_dir, coara_home)
    # 过程带：janitor 的 LLM 请求/工具调用经 llm-calls 镜像与日志留痕；会话事件
    # 由 recorder 写，但 session_agent_kind=subagent 被投影层排除，且 recover_session
    # 只重放目标 session_id 的主会话事件——维护回合是独立 session_id，不污染主带。
    stream = janitor.process_message(
        prompt,
        trust_level="owner",
        show_tool_summary=False,
        source="background",
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
    """创建一个 janitor 维护回合任务，返回 asyncio.Task（供单飞/完成钩子挂钩）。

    由 workspace_protocol 的派发层调用（已过完单飞/冷却闸门）。返回 None 表示
    装配失败（配置缺失等），调用方按失败处理。``prompt`` 须已含完整 <系统提醒>
    （动态参数 + janitor.md），本函数不再另注一条。
    """
    provider, model = _resolve_janitor_llm(root, workspace_dir)
    janitor = _build_janitor_coara(root, workspace_dir=workspace_dir, provider=provider, model=model)
    if janitor is None:
        return None

    history = _load_target_history(root, workspace_dir, coara_home, history_snapshot=history_snapshot)
    janitor.message_history = _ensure_env_seed(history, workspace_dir)

    async def _run() -> None:
        try:
            await _drain_maintenance_turn(janitor, prompt, workspace_dir, coara_home)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"janitor maintenance turn failed for {workspace_name}: {exc}")
            raise

    task = asyncio.create_task(_run(), name=f"janitor-{workspace_name}")
    return task


def _resolve_janitor_llm(root: Any, workspace_dir: str) -> tuple[str | None, str | None]:
    """provider/model：空间 last-run（entry 绑定 = 最后正常对话运行的模型）。

    janitor 无端归属，一定跟随该空间最后运行的模型；不再读取 janitor 专属
    配置（已退役，不用配）。未绑定（从未对话）时返回 (None, None) 由 base
    跟随全局默认。
    """
    from src.coara.workspace_protocol import _workspace_entry_llm

    return _workspace_entry_llm(root, workspace_dir)
