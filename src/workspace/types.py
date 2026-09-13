"""Workspace registry and mount types."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WorkspaceKind(StrEnum):
    MANAGED = "managed"
    REFERENCE = "reference"
    INTERNAL = "internal"
    # 代码编程空间：用户手动声明，janitor 据此维护「空间内目录文件」模块。
    # 长期属性，由用户设置，janitor 不写。
    CODE = "code"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUSPENDED = "suspended"


class MountMode(StrEnum):
    READ_WRITE = "rw"
    READ_ONLY = "ro"


class ViewCapability(StrEnum):
    """空间的视图能力需求——端能否接入，取决于端能否渲染该空间的展示物。

    - ``all``：展示物是普通会话/文件，三端（CLI/web/手机）都渲染得了，全端可接。
    - ``web_only``：展示物含需 web 画布的内容（工作流图/配置表单等），只有 web 能渲染，
      CLI/手机不接。空间模型因此无限制：未来任何新形态空间（看板/文档/画布）声明
      自己的视图能力即可，不改路由逻辑。
    """

    ALL = "all"
    WEB_ONLY = "web_only"


class WorkspaceEntry(BaseModel):
    """A registered workspace owned by the user."""

    name: str
    id: str
    path: str
    kind: WorkspaceKind = WorkspaceKind.MANAGED
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    mode: MountMode = MountMode.READ_WRITE
    # 视图能力：端能否接入此空间。缺省 all（普通空间三端可接）；工作流/配置等
    # 系统空间声明 web_only，仅 web 渲染其专属展示物。daily 这类会话型系统空间取 all。
    view: ViewCapability = ViewCapability.ALL
    # 空间身份（docs/空间模型与内容注册表.md）：内容类型 → 默认门面；storefront 覆盖
    # 类型默认；home_view 指向展示主页的路由（用量=/usage、配置=/config）。仓库空间
    # 三者全空 = 对话主页（v8 不写 space.yaml 也成立）。
    content_type: str | None = None
    storefront: str | None = None
    home_view: str | None = None
    # 空间里对话的主体（docs/空间模型与内容注册表.md §2 persona）：internal 系统空间
    # 可绑专属 agent（如记录空间的 persona=daily——条目名是展示名「记录」，对话主体
    # 仍是 daily）。缺省 None = 无专属主体；internal 会话创建时 persona 优先、name 兜底
    persona: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    # 空间级 LLM 绑定：缺省 = 跟随全局默认（llm_preferences.yaml）
    provider: str | None = None
    model: str | None = None

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()

    def end_allowed(self, end: str) -> bool:
        """端（cli/web/matrix）能否接入此空间：web 全能，其余端仅接 view=all 的空间。"""
        if self.view == ViewCapability.WEB_ONLY:
            return end == "web"
        return True


class WorkspaceRegistryDocument(BaseModel):
    """On disk: ``registry/workspaces.yaml`` with top-level ``workspaces`` / ``default_workspace``."""

    version: int = 1
    default_workspace: str | None = None
    workspaces: dict[str, WorkspaceEntry] = Field(default_factory=dict)


class ResolvedPath(BaseModel):
    """Result of VFS path resolution."""

    workspace_id: str
    path: Path
    relative: str
    mode: MountMode


class WorkspacePermissions(BaseModel):
    """Per-workspace ACL (optional yaml alongside registry)."""

    workspace: str
    defaults: dict[str, dict[str, str]] = Field(default_factory=dict)
    path_rules: list[dict[str, Any]] = Field(default_factory=list)
    tool_rules: list[dict[str, Any]] = Field(default_factory=list)
