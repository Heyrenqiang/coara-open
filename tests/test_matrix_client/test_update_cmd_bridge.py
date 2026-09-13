"""批复指令侧信道（[COARA_UPDATE_CMD]）测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.matrix_client.update_cmd_bridge import (
    _execute,
    format_update_ack,
    is_update_cmd_message,
    parse_update_cmd,
)
from src.workspace.updates.store import WorkspaceUpdatesStore


def _seed(store: WorkspaceUpdatesStore, **overrides):
    params = {
        "workspace": "shop",
        "source_id": "test",
        "event_type": "reminder_due",
        "dedupe_key": "k1",
        "text": "正文",
        "payload": {"title": "标题"},
        "type": "reminder",
    }
    params.update(overrides)
    return store.append(**params)


class _FakeCoara:
    def __init__(self) -> None:
        self._process_lock = asyncio.Lock()
        self.received: list[str] = []

    def has_active_turn(self) -> bool:
        return True  # 走 queued 分支，同步可见

    def submit_continuation_input(self, text: str) -> None:
        self.received.append(text)


def _fake_root(store: WorkspaceUpdatesStore, coara: _FakeCoara | None = None):
    entry = SimpleNamespace(id="ws-1", name="shop")
    coara = coara or _FakeCoara()

    async def ensure_workspace_session(e):
        return SimpleNamespace(coara=coara)

    return SimpleNamespace(
        _updates_store=lambda: store,
        workspace_manager=SimpleNamespace(
            registry=SimpleNamespace(resolve_name_or_id=lambda _n: entry),
        ),
        ensure_workspace_session=ensure_workspace_session,
        _coara=coara,
    )


def test_parse_update_cmd_round_trip() -> None:
    body = '[COARA_UPDATE_CMD]\n{"action":"read","message_id":"upd-1"}\n[/COARA_UPDATE_CMD]'
    assert is_update_cmd_message(body)
    data = parse_update_cmd(body)
    assert data == {"action": "read", "message_id": "upd-1"}
    assert not is_update_cmd_message("普通聊天 [COARA_UPDATES] 不算")
    assert parse_update_cmd("没有指令") is None


def test_format_update_ack() -> None:
    text = format_update_ack({"ok": True, "action": "read"})
    assert text.startswith("[COARA_UPDATE_ACK]")
    assert '"ok": true' in text


@pytest.mark.asyncio
async def test_execute_read_archive_mark_read(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    m1 = _seed(store, dedupe_key="a")
    m2 = _seed(store, dedupe_key="b")
    root = _fake_root(store)

    ack = await _execute(root, {"action": "read", "message_id": m1.message_id})
    assert ack["ok"] is True
    assert store.get(m1.message_id).status == "read"

    ack = await _execute(root, {"action": "archive", "message_id": m2.message_id})
    assert ack["ok"] is True
    assert store.get(m2.message_id).status == "archived"

    _seed(store, dedupe_key="c")
    ack = await _execute(root, {"action": "mark_read", "workspace": "shop"})
    assert ack["ok"] is True
    assert store.unread_count("shop") == 0

    ack = await _execute(root, {"action": "read", "message_id": "upd-none"})
    assert ack["ok"] is False


@pytest.mark.asyncio
async def test_execute_review_routes_to_session(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _seed(store)
    root = _fake_root(store)

    ack = await _execute(root, {"action": "review", "message_id": msg.message_id, "text": "处理它"})
    assert ack["ok"] is True
    assert ack["delivered"] == "queued"
    assert ack["workspace"] == "shop"
    assert len(root._coara.received) == 1
    assert "处理它" in root._coara.received[0]
    assert store.get(msg.message_id).status == "read"


@pytest.mark.asyncio
async def test_schedule_handler_exception_logged() -> None:
    """done_callback 必须取 exception 并记录：ACK 失败不得静默丢失（#41）。"""
    from loguru import logger as loguru_logger

    from src.matrix_client import update_cmd_bridge

    class _BoomStore:
        def mark_read(self, _mid):
            raise RuntimeError("boom-store")

    root = SimpleNamespace(_updates_store=lambda: _BoomStore())
    body = '[COARA_UPDATE_CMD]\n{"action":"read","message_id":"m1"}\n[/COARA_UPDATE_CMD]'
    seen: list[str] = []
    sink_id = loguru_logger.add(lambda m: seen.append(str(m)), level="ERROR")
    try:
        update_cmd_bridge.schedule_handle_update_cmd(root, "!r:x", body)
        await asyncio.gather(*list(update_cmd_bridge._CMD_TASKS), return_exceptions=True)
        await asyncio.sleep(0)  # done_callback 经 call_soon 调度
    finally:
        loguru_logger.remove(sink_id)
    assert any("boom-store" in m for m in seen)
