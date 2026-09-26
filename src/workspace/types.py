"""Workspace registry and mount types."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WorkspaceKind(StrEnum):
    """空间接入身份——回答「谁在对这个空间说话」，三值互斥。

    - normal：普通用户空间，对话主体是拥有者本人
    - internal：系统服务台空间，对话主体是内部机制（service_desk 只认它可 @）
    - external：对外开放空间，外部访客接入一律 untrusted（沙箱 + 审批硬门）

    只读语义由 mode=ro 承担，不再占 kind 位。
    """

    NORMAL = "normal"
    INTERNAL = "internal"
    EXTERNAL = "external"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUSPENDED = "suspended"


class MountMode(StrEnum):
    READ_WRITE = "rw"
    READ_ONLY = "ro"


class ViewCapability(StrEnum):
    """空间的视图能力需求——端能否接入，取决于端能否渲染该空间的展示物"""

    ALL = "all"
    WEB_ONLY = "web_only"


class WorkspaceEntry(BaseModel):
    """A registered workspace owned by the user."""

    name: str
    id: str
    path: str
    kind: WorkspaceKind = WorkspaceKind.NORMAL
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    mode: MountMode = MountMode.READ_WRITE
    # 视图能力：端能否接入此空间。缺省 all（普通空间三端可接）；工作流/配置等
    # 系统空间声明 web_only，仅 web 渲染其专属展示物。daily 这类会话型系统空间取 all。
    view: ViewCapability = ViewCapability.ALL
    # 三者全空 = 对话主页（v8 不写 space.yaml 也成立）。
    content_type: str | None = None
    storefront: str | None = None
    home_view: str | None = None
    # 可绑专属 agent（如记录空间的 persona=daily——条目名是展示名「记录」，对话主体
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
