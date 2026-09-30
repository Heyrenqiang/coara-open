"""Module registry — the plugin contract for sidebar modules.

模块声明的磁盘真源（《空间能力系统》槽位三）：内置模块是
``src/coara/prompts/modules/*.yaml`` 声明文件（出厂随包），不再是代码字面量。
用户级 ``users/default/modules/`` 追加在后（同名 id 覆盖内置——后者覆盖前者，
与技能加载三层优先级同范式）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.logger import logger

# 工作空间关系类型（设计文档 §2.1）
WORKSPACE_RELATIONS = ("scoped", "bindable", "filterable", "none")


@dataclass(frozen=True, slots=True)
class AgenticSpec:
    """模块的 agentic（专属会话）能力声明"""

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


def _module_dirs() -> list[Path]:
    """模块声明目录（后者覆盖前者，与技能三层同范式）：内置 → 用户级。"""
    dirs = [Path(__file__).parent / "prompts" / "modules"]
    try:
        from src.core.coara_home import resolve_coara_home

        dirs.append(resolve_coara_home(Path.cwd()) / "users" / "default" / "modules")
    except Exception:
        pass
    return dirs


def _spec_from_yaml(path: Path) -> ModuleSpec | None:
    """从 yaml 声明文件解析 ModuleSpec；解析失败记 WARNING 返回 None。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Module declaration unreadable: {path}: {exc}")
        return None
    if not isinstance(raw, dict):
        logger.warning(f"Module declaration not a mapping: {path}")
        return None
    agentic_raw = raw.get("agentic") or {}
    agentic = AgenticSpec(
        enabled=bool(agentic_raw.get("enabled", False)),
        persona_agent=str(agentic_raw.get("persona_agent", "") or ""),
        role=str(agentic_raw.get("role", "") or ""),
        expertise_areas=tuple(str(a) for a in (agentic_raw.get("expertise_areas") or [])),
        tools=(
            tuple(str(t) for t in agentic_raw["tools"]) if agentic_raw.get("tools") is not None else None
        ),
        subject=str(agentic_raw.get("subject", "") or ""),
    )
    try:
        return ModuleSpec(
            id=path.stem,
            title=str(raw.get("title", path.stem) or path.stem),
            icon=str(raw.get("icon", "") or ""),
            route=str(raw.get("route", "") or ""),
            workspace_relation=str(raw.get("workspace_relation", "none") or "none"),
            agentic=agentic,
            order=int(raw.get("order", 100) or 100),
        )
    except Exception as exc:
        logger.warning(f"Module declaration invalid: {path}: {exc}")
        return None


def _register_builtin_modules() -> None:
    """从磁盘加载模块声明：内置目录 + 用户级目录（同名 id 后者覆盖）。"""
    loaded = 0
    for directory in _module_dirs():
        if not directory.is_dir():
            continue
        for yaml_file in sorted(directory.glob("*.yaml")):
            spec = _spec_from_yaml(yaml_file)
            if spec is not None:
                module_registry.register(spec)
                loaded += 1
    logger.info(f"Module registry loaded {loaded} module declarations")


_register_builtin_modules()
