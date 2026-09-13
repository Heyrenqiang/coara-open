"""Inbound event and event-source configuration types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from src.core.time import now_iso


class EventSourceKind(StrEnum):
    FILE_WATCH = "file_watch"
    INTERVAL_POLL = "interval_poll"
    WEBHOOK = "webhook"
    CRON = "cron"


class HandleMode(StrEnum):
    """内容处理模式：park=挂住等用户；janitor=叫醒管家 janitor 过目处置"""

    PARK = "park"
    JANITOR = "janitor"


SALIENCE_LEVELS = ("low", "normal", "high")


class EventSourceDefinition(BaseModel):
    id: str
    enabled: bool = True
    kind: EventSourceKind
    workspace: str

    watch_path: str | None = None
    watch_pattern: str = "*"
    watch_events: list[str] = Field(default_factory=lambda: ["created"])

    interval_seconds: float | None = None
    poll_min_count: int = 1

    # kind=cron 必填：5 字段 cron 表达式（Asia/Shanghai）
    cron: str = ""

    webhook_secret: str | None = None

    salience: str = "normal"
    handle: HandleMode = HandleMode.PARK
    cooldown_seconds: float = 30.0
    # 消息保质期（秒）：事件落收件箱后超过该时长仍未读 → 纯规则过目自动勾掉（dismissed）
    ttl_seconds: float | None = None

    routing_domain: str | None = None
    suggested_delegate: str | None = None

    message_template: str = "[事件 — {{source_id}}]\nworkspace: @{{workspace}}\nevent_type: {{event_type}}\n{{details}}"

    @model_validator(mode="before")
    @classmethod
    def _normalize_handle(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # 未知/废弃 handle 字符串 → 默认 park（避免枚举校验失败）
        current = data.get("handle")
        if current is not None and str(current) not in {m.value for m in HandleMode}:
            data["handle"] = HandleMode.PARK.value
        return data

    @model_validator(mode="after")
    def _normalize_salience(self) -> EventSourceDefinition:
        if self.salience not in SALIENCE_LEVELS:
            self.salience = "normal"
        if self.kind == EventSourceKind.CRON and not self.cron.strip():
            raise ValueError("cron is required when kind=cron")
        return self


class InboundEvent(BaseModel):
    source_id: str
    workspace: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    occurred_at: str = Field(default_factory=now_iso)

    def details_text(self) -> str:
        parts: list[str] = []
        for key, value in self.payload.items():
            parts.append(f"{key}: {value}")
        return "\n".join(parts) if parts else "(no details)"
