"""Tests for ws updates actions — mark_read watermark push to Matrix."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tools.builtin.ws.updates_ops import execute_updates_action
from src.workspace.updates.store import WorkspaceUpdatesStore


def _coara(tmp_path, push: AsyncMock | None) -> SimpleNamespace:
    ns = SimpleNamespace(
        event_source_manager=None,
        workspace_manager=SimpleNamespace(coara_home=tmp_path),
    )
    if push is not None:
        ns._push_workspace_updates_state_to_matrix = push
    return ns


def _seed(store: WorkspaceUpdatesStore, workspace: str = "shop") -> None:
    store.append(
        workspace=workspace,
        source_id="test",
        event_type="test.event",
        dedupe_key="k1",
        text="hello",
        payload={},
    )


@pytest.mark.asyncio
async def test_mark_read_advances_watermark_and_pushes_state(tmp_path) -> None:
    push = AsyncMock()
    coara = _coara(tmp_path, push)
    store = WorkspaceUpdatesStore(tmp_path)
    _seed(store)
    assert store.unread_count("shop") == 1

    result = await execute_updates_action(coara, "updates_mark_read", alias="shop")

    assert not result.is_error
    assert store.unread_count("shop") == 0
    push.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_read_without_root_push_method_still_succeeds(tmp_path) -> None:
    coara = _coara(tmp_path, push=None)
    store = WorkspaceUpdatesStore(tmp_path)
    _seed(store)

    result = await execute_updates_action(coara, "updates_mark_read", alias="shop")

    assert not result.is_error
    assert store.unread_count("shop") == 0


@pytest.mark.asyncio
async def test_mark_read_push_failure_does_not_fail_action(tmp_path) -> None:
    push = AsyncMock(side_effect=RuntimeError("matrix offline"))
    coara = _coara(tmp_path, push)
    _seed(WorkspaceUpdatesStore(tmp_path))

    result = await execute_updates_action(coara, "updates_mark_read", alias="shop")

    assert not result.is_error


@pytest.mark.asyncio
async def test_updates_board_renders_rules_and_groups(tmp_path) -> None:
    """过目单：规则头 + 待处理 + 已处置，一次拿全。"""
    coara = _coara(tmp_path, None)
    store = WorkspaceUpdatesStore(tmp_path)
    pending = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="b1",
        text="hello",
        payload={},
        salience="high",
    )
    assert pending is not None
    reviewed = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="b2",
        text="world",
        payload={},
    )
    assert reviewed is not None
    store.set_disposition(reviewed.message_id, "elevated", reviewed_by="janitor", note="需要用户拍板")

    result = await execute_updates_action(coara, "updates_board", alias="shop")

    assert not result.is_error
    text = result.content or ""
    assert "工作空间过目单" in text
    assert "处理规则" in text
    assert "必须写 note" in text
    assert pending.message_id in text
    assert "待处理（1）" in text
    assert "已处置（1）" in text
    assert reviewed.message_id in text
    assert "需要用户拍板" in text


@pytest.mark.asyncio
async def test_disposition_requires_note(tmp_path) -> None:
    """dismiss/elevate/resolve 必须写 note，缺失即拒绝且不写轨迹。"""
    coara = _coara(tmp_path, None)
    store = WorkspaceUpdatesStore(tmp_path)
    msg = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="n1",
        text="hello",
        payload={},
    )
    assert msg is not None

    for action in ("updates_dismiss", "updates_elevate", "updates_resolve"):
        result = await execute_updates_action(coara, action, message_id=msg.message_id)
        assert result.is_error, f"{action} 缺 note 应拒绝"
        loaded = store.get(msg.message_id)
        assert loaded is not None
        assert loaded.disposition == "pending"
        assert loaded.review_history == []


@pytest.mark.asyncio
async def test_dismiss_rejects_high_salience(tmp_path) -> None:
    """high 显著禁止 dismiss（规则头宣称的硬约束必须有实现）：返回明确错误且不写轨迹。"""
    coara = _coara(tmp_path, None)
    store = WorkspaceUpdatesStore(tmp_path)
    high = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="h1",
        text="alarm",
        payload={},
        salience="high",
    )
    assert high is not None

    result = await execute_updates_action(coara, "updates_dismiss", message_id=high.message_id, note="噪音")
    assert result.is_error
    text = result.content or ""
    assert "高显著" in text and "elevate" in text
    loaded = store.get(high.message_id)
    assert loaded is not None
    assert loaded.disposition == "pending"
    assert loaded.review_history == []

    # 改判为 elevate 放行（规则允许 high 呈阅）
    elevated = await execute_updates_action(
        coara, "updates_elevate", message_id=high.message_id, note="需要用户拍板"
    )
    assert not elevated.is_error

    # normal 显著照常可 dismiss
    normal = store.append(
        workspace="shop",
        source_id="test",
        event_type="test.event",
        dedupe_key="n2",
        text="noise",
        payload={},
        salience="normal",
    )
    assert normal is not None
    dismissed = await execute_updates_action(coara, "updates_dismiss", message_id=normal.message_id, note="噪音")
    assert not dismissed.is_error


@pytest.mark.asyncio
async def test_updates_guidance_points_to_cli_command(tmp_path) -> None:
    """review 工具已删除：列表/过目单的用户可见指引必须指向 /ws updates，不得再提 review(action=…)。"""
    coara = _coara(tmp_path, None)
    store = WorkspaceUpdatesStore(tmp_path)
    _seed(store)

    list_result = await execute_updates_action(coara, "updates_list", alias="shop", status="all")
    assert not list_result.is_error
    list_text = list_result.content or ""
    assert "/ws updates" in list_text
    assert "review(" not in list_text

    board_result = await execute_updates_action(coara, "updates_board", alias="shop")
    assert not board_result.is_error
    board_text = board_result.content or ""
    assert "/ws updates" in board_text
    assert "review(" not in board_text
    assert "high 显著必须 elevate 或 resolve" in board_text
