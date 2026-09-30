"""通用模块会话主体工厂（agentic 模块专属 CoaraBase）"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.events import TraceEvent
from src.core.logger import logger
from src.core.types import CoaraPersona

if TYPE_CHECKING:
    from src.coara.module_registry import ModuleSpec
    from src.coara.root import RootCoara


def make_subject_trace_sink(bus: Any, subject: str) -> Callable[[TraceEvent], None]:
    """trace 转发器：打 subject 标签后转发到 Root 事件总线（模块会话与 FlowRoot 共用）。"""

    def _sink(event: TraceEvent) -> None:
        tagged = dict(event.payload or {})
        tagged["subject"] = subject
        bus.publish(
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

    return _sink


def _load_agent_prompt(agent_name: str) -> tuple[str, str, Any | None]:
    """从 AgentRegistry 加载模块 persona（yaml+md 配对）。"""
    from src.prompt.agent_registry import AgentRegistry

    registry = AgentRegistry()
    defn = registry.get_agent(agent_name)
    if defn is None:
        logger.warning("module agent prompt missing: %s; using fallback", agent_name)
        return agent_name, f"你是 {agent_name} 模块的专属助手。中文交流。", None
    return defn.name, defn.system_prompt_template, defn.yaml_config


def _resolve_tools_from_yaml(yaml_config: Any) -> list[str] | None:
    """从 agent yaml 的 tools_include 提取工具白名单；["*"] 或空 = None（全集）。

    AgentPromptConfig 是扁平结构（tools_include: list[str]）。
    """
    try:
        include = getattr(yaml_config, "tools_include", None) if yaml_config is not None else None
        if not include:
            return None
        include = [str(t) for t in include]
        if "*" in include:
            return None
        return include
    except Exception:
        return None


async def build_module_subject(
    root_coara: RootCoara,
    *,
    name: str,
    agent_name: str,
    role: str,
    expertise_areas: list[str],
    subject: str,
    session_agent_kind: str,
) -> Any:
    """模块会话主体的公共装配段（create_module_root 与 create_flow_root 共用）。

    职责：persona 加载（AgentRegistry）→ CoaraBase 创建（继承前台 provider/model）
    → workspace_manager 共享 → 内置 + runtime 工具注册 → ToolGatewayTool →
    subject trace sink。各模块的专属段（工具白名单 / orchestrator 常驻 /
    会话恢复等）由调用方在此之后叠加。
    """
    from src.coara.base import CoaraBase
    from src.coara.runtime_tools import register_runtime_tools

    fg = root_coara.foreground_coara
    workspace_dir = Path(fg.workspace_dir).expanduser().resolve()

    resolved_name, prompt, yaml_config = _load_agent_prompt(agent_name)
    persona = CoaraPersona(
        name=resolved_name,
        role=role,
        expertise_areas=expertise_areas,
        system_prompt_template=prompt,
        yaml_config=yaml_config,
    )
    coara = CoaraBase(
        name=name,
        persona=persona,
        workspace_dir=workspace_dir,
        provider=fg.provider,
        provider_name=fg.provider_name,
        model=fg.model_name,
        user_facing=True,
        is_owner_context=True,
        max_tool_iterations=600,
        session_agent_kind=session_agent_kind,
    )
    # 共享 workspace_manager（VFS resolver）
    coara.workspace_manager = root_coara.workspace_manager

    # 进程级无状态工具 + runtime 工具
    from src.tools import register_builtin_tools
    from src.tools.registry import tool_registry

    register_builtin_tools()
    coara.register_tools(tool_registry.list_all())
    register_runtime_tools(coara)

    # 挂起工具入口
    try:
        from src.tools.builtin.integration.tool import ToolGatewayTool

        coara.register_tool(ToolGatewayTool(parent_coara=coara), replace=True)
    except Exception as exc:
        logger.warning(f"Tool gateway registration failed for module {name}: {exc}")

    # trace 事件带 subject，路由到该模块的 WebUI 会话
    coara.set_trace_sink(make_subject_trace_sink(root_coara.event_bus, subject))

    return coara


async def create_module_root(root_coara: RootCoara, spec: ModuleSpec) -> Any:
    """为 agentic 模块创建专属会话主体（CoaraBase）"""
    subject = spec.subject
    agentic = spec.agentic

    coara = await build_module_subject(
        root_coara,
        name=f"module-{spec.id}",
        agent_name=agentic.persona_agent or spec.id,
        role=agentic.role or spec.title,
        expertise_areas=list(agentic.expertise_areas),
        subject=subject,
        session_agent_kind=spec.id,
    )

    # 工具面白名单：agent yaml tools_include 优先，其次模块声明
    yaml_config = coara.identity.persona.yaml_config
    allow = _resolve_tools_from_yaml(yaml_config)
    if allow is None and agentic.tools is not None:
        allow = list(agentic.tools)
    if allow is not None:
        # 物理移除白名单外的工具，让模块会话真正收不到（权限边界）。
        allow_set = set(allow)
        for name in list(coara._tool_manager.tools.keys()):
            if name not in allow_set:
                coara._tool_manager.tools.pop(name, None)

    # local_search 需绑 Root 的 records_store（模块主体不经 workspace_session 定制，
    # 拿不到这条绑定），常驻非挂起——记录助手直接检索，无需先 tool activate
    if getattr(root_coara, "records_store", None) is not None:
        from src.tools.builtin.records.local_search import LocalSearchTool

        coara.register_tool(
            LocalSearchTool(store=root_coara.records_store, parent_coara=coara, defer=False),
            replace=True,
        )
    # 配置模块专属：改完 providers.yaml 后触发热重载，立即生效
    if spec.id == "config":
        from src.tools.builtin.integration.reload_providers import ReloadProvidersTool

        coara.register_tool(ReloadProvidersTool(), replace=True)

    logger.info(
        "Module root created: %s (subject=%s, session=%s, tools=%s)",
        spec.id,
        subject,
        coara.session_id,
        "allowlist" if allow is not None else "full",
    )
    return coara
