"""Tests for the post-divestiture inbound router.

WDL 执行层剥离后（2026-09-08），scheduler 里的历史 process_result（工作流
终态回投）不再有生产者，一律忽略且不发布任何事件、不注入会话。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coara.inbound_router import dispatch_scheduler_message


@pytest.mark.asyncio
async def test_legacy_workflow_result_is_ignored() -> None:
    root = SimpleNamespace(event_bus=MagicMock())
    await dispatch_scheduler_message(
        root,
        SimpleNamespace(
            msg_type="process_result",
            content={"msg_type": "WORKFLOW_COMPLETED", "task_id": "wf-1"},
        ),
    )
    root.event_bus.publish.assert_not_called()
