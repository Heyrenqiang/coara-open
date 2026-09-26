"""Workspace update entry types — typed inbound events queued per workspace."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.core.time import now_iso

UpdateStatus = Literal["unread", "read", "archived"]

# 显著性：决定前台待处理视图曝光（high 上浮，其余仅收件箱红点）
SALIENCE_LEVELS = ("low", "normal", "high")

# 处理模式；语义见 src.event_sources.types.HandleMode 的 docstring
HANDLE_MODES = ("park", "janitor")

# 处置轨迹（与 status 正交）：pending=待过目；elevated=已过目并呈阅用户；
# resolved=已处置；dismissed=已忽略（留轨迹不再上浮）
DISPOSITIONS = ("pending", "elevated", "resolved", "dismissed")


class ReviewEntry(BaseModel):
    """一条处置轨迹：谁、何时、做了什么、备注。"""

    by: str
    at: str
    action: str  # dismissed | elevated | resolved
    note: str = ""


class WorkspaceUpdate(BaseModel):
    """One typed entry in a workspace's updates stream（消息内容信封）."""

    message_id: str
    workspace: str
    source_id: str
    event_type: str
    type: str = "note"  # noqa: A003 — JSON 字段名按设计稿固定为 type
    payload_ref: str | None = None
    status: UpdateStatus = "unread"
    dedupe_key: str
    title: str
    text: str
    display_text: str = ""
    salience: str = "normal"
    handle_mode: str = "park"
    source_kind: str = ""
    disposition: str = "pending"
    reviewed_by: str = ""
    reviewed_at: str | None = None
    review_note: str = ""
    review_history: list[ReviewEntry] = Field(default_factory=list)
    salience_by: str = ""
    salience_at: str | None = None
    expires_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
    read_at: str | None = None
    archived_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceUpdate:
        return cls.model_validate(data)
