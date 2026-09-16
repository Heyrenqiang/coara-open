"""Module registry — the plugin contract for sidebar modules.

每个模块（对话/工作流/用量/配置/…）是一个自包含插件，声明：
- 基本身份（id/title/icon/route）
- 与工作空间的关系（scoped/bindable/filterable/none，见设计文档 §2）
- agentic 能力（是否有专属会话、persona、工具白名单）

后端注册表是 single source of truth；前端模块清单经 API 拉取（或镜像）。
新增模块 = 注册一个插件，框架自动接入，不改核心。

参考：docs/模块化插件架构设计.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 工作空间关系类型（设计文档 §2.1）
WORKSPACE_RELATIONS = ("scoped", "bindable", "filterable", "none")


@dataclass(frozen=True, slots=True)
class AgenticSpec:
    """模块的 agentic（专属会话）能力声明。

    persona_agent：agent 注册表中的 persona 名（决定系统提示词）。
    tools：会话可调用的工具白名单（None = 继承默认全集）。
    """

    enabled: bool = False
    persona_agent: str = ""
    role: str = ""
    expertise_areas: tuple[str, ...] = ()
    tools: tuple[str, ...] | None = None
    # WS subject / 历史 source 后缀（"flow" / "config" / ...）。空 = 用模块 id。
    subject: str = ""


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """侧边栏模块（插件）的完整声明。"""

    id: str
    title: str
    icon: str = ""
    route: str = ""
    workspace_relation: str = "none"
    agentic: AgenticSpec = field(default_factory=AgenticSpec)
    # 侧边栏顺序（小在前）。
    order: int = 100

    def __post_init__(self) -> None:
        if self.workspace_relation not in WORKSPACE_RELATIONS:
            raise ValueError(
                f"module '{self.id}': invalid workspace_relation "
                f"'{self.workspace_relation}' (must be one of {WORKSPACE_RELATIONS})"
            )

    @property
    def subject(self) -> str:
        """WS subject / 历史 source：'module:<id>'，工作流例外兼容 'flow'。"""
        return self.agentic.subject or self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "icon": self.icon,
            "route": self.route,
            "workspace_relation": self.workspace_relation,
            "order": self.order,
            "agentic": {
                "enabled": self.agentic.enabled,
                "subject": self.subject,
                "role": self.agentic.role,
            },
        }


class ModuleRegistry:
    """进程级模块注册表（单例）。"""

    def __init__(self) -> None:
        self._modules: dict[str, ModuleSpec] = {}

    def register(self, spec: ModuleSpec) -> None:
        self._modules[spec.id] = spec

    def get(self, module_id: str) -> ModuleSpec | None:
        return self._modules.get(module_id)

    def list(self) -> list[ModuleSpec]:
        return sorted(self._modules.values(), key=lambda m: m.order)

    def by_subject(self, subject: str) -> ModuleSpec | None:
        for m in self._modules.values():
            if m.agentic.enabled and m.subject == subject:
                return m
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"modules": [m.to_dict() for m in self.list()]}


module_registry = ModuleRegistry()


def _register_builtin_modules() -> None:
    """注册内置模块。对话/消息/文件/发布/工作流/记录/用量/配置。

    系统空间（消息/记录/用量/配置/工作流）均有 agentic 专属会话与 persona；
    工作流以「示例工作空间」身份在册——与其它系统空间同构（persona + 专属
    画布页面），是出厂自带的参考实现。执行层不在内核：WDL 实例运行由独立
    wdl 软件承载。
    「LLM 请求」已移入独立开发者工具（src/devtools · coara-devtools），不进发布版。
    """
    module_registry.register(
        ModuleSpec(
            id="chat",
            title="对话",
            icon="MessageOutlined",
            route="/chat",
            workspace_relation="scoped",
            order=0,
        )
    )
    module_registry.register(
        ModuleSpec(
            id="review",
            title="消息",
            icon="AuditOutlined",
            route="/review",
            workspace_relation="filterable",
            order=10,
            agentic=AgenticSpec(
                enabled=True,
                persona_agent="review-assistant",
                role="消息助手",
                expertise_areas=("workspace updates", "triage", "summary"),
                subject="review",
            ),
        )
    )
    module_registry.register(
        ModuleSpec(
            id="files",
            title="文件",
            icon="FileOutlined",
            route="/files",
            workspace_relation="scoped",
            order=20,
        )
    )
    module_registry.register(
        ModuleSpec(
            id="workflow",
            title="工作流",
            icon="ApartmentOutlined",
            route="/workflow",
            workspace_relation="bindable",
            order=40,
            agentic=AgenticSpec(
                enabled=True,
                persona_agent="flow-root",
                role="工作流构建教练",
                expertise_areas=("workflow orchestration", "debugging", "automation"),
                tools=None,  # 继承现有 FlowRoot 工具面
                subject="flow",  # 兼容现有 subject="flow"
            ),
        )
    )
    module_registry.register(
        ModuleSpec(
            id="records",
            title="记录",
            icon="BookOutlined",
            route="/records",
            workspace_relation="filterable",
            order=50,
            agentic=AgenticSpec(
                enabled=True,
                persona_agent="records-assistant",
                role="记录助手",
                expertise_areas=("records search", "digest", "summary"),
                subject="records",
            ),
        )
    )
    module_registry.register(
        ModuleSpec(
            id="usage",
            title="用量",
            icon="BarChartOutlined",
            route="/usage",
            workspace_relation="filterable",
            order=60,
            agentic=AgenticSpec(
                enabled=True,
                persona_agent="usage-assistant",
                role="用量助手",
                expertise_areas=("usage analytics", "cost", "model distribution"),
                subject="usage",
            ),
        )
    )
    module_registry.register(
        ModuleSpec(
            id="config",
            title="配置",
            icon="SettingOutlined",
            route="/config",
            workspace_relation="none",
            order=80,
            agentic=AgenticSpec(
                enabled=True,
                persona_agent="config-assistant",
                role="配置助手",
                expertise_areas=("providers", "llm config", "coara settings", "records.enabled"),
                subject="config",
            ),
        )
    )


_register_builtin_modules()
