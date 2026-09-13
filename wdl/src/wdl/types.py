"""wdl 运行时数据类型。"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowInstanceState(StrEnum):
    """Workflow instance lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class WorkflowInstanceRecord(BaseModel):
    """Persisted workflow instance state for crash recovery."""

    instance_id: str
    wdl_text: str
    status: WorkflowInstanceState = WorkflowInstanceState.PENDING
    inputs_json: str = "{}"
    current_step_id: str | None = None
    context_json: str = "{}"
    wake_at: datetime | None = None
    wait_event: str | None = None
    wait_timeout: datetime | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def context_snapshot(self) -> dict[str, Any]:
        """context_json 的 dict 视图（损坏时回退空 dict，不炸恢复路径）。"""
        try:
            parsed = json.loads(self.context_json or "{}")
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
