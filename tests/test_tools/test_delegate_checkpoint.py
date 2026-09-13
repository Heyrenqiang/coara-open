"""Subagent incremental checkpoint: mid-run breakpoint writes to SubagentStore.

The terminal write in ``_run_subagent`` only happens at run end; a process
kill mid-run used to lose all intermediate progress. The checkpointer
rewrites the agent's single record (throttled) at every tool-batch boundary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.coara.subagent_store import SubagentStore
from src.core.types import Message, MessageRole, SubagentStatus
from src.tools.builtin.delegate.delegate import SubagentCheckpointer


def _fake_subagent(history: list[Message]) -> SimpleNamespace:
    return SimpleNamespace(
        message_history=history,
        session_id="child-session",
        identity=SimpleNamespace(coara_id="child-coara"),
    )


def _checkpointer(store: SubagentStore, *, min_interval: float = 5.0) -> SubagentCheckpointer:
    return SubagentCheckpointer(
        store,
        agent_id="sa-coaras-test",
        subagent_type="coaras",
        description="demo task",
        background=True,
        parent_coara_id="parent-coara",
        min_interval=min_interval,
    )


def test_checkpoint_writes_running_record_with_history(tmp_path: Path) -> None:
    store = SubagentStore(tmp_path)
    subagent = _fake_subagent([Message(role=MessageRole.USER, content="干活")])
    ckpt = _checkpointer(store)

    assert ckpt.maybe_write(subagent) is True

    record = store.load("sa-coaras-test")
    assert record is not None
    assert record.status == SubagentStatus.RUNNING_BACKGROUND.value
    assert record.mode == "background"
    assert record.parent_coara_id == "parent-coara"
    assert record.message_history == [{"role": "user", "content": "干活"}]


def test_checkpoint_throttles_within_interval(tmp_path: Path) -> None:
    store = SubagentStore(tmp_path)
    subagent = _fake_subagent([Message(role=MessageRole.USER, content="干活")])
    ckpt = _checkpointer(store, min_interval=60.0)

    assert ckpt.maybe_write(subagent) is True
    subagent.message_history.append(Message(role=MessageRole.ASSISTANT, content="进展"))
    # 节流窗口内：不重复写盘，磁盘上的历史仍是旧版本
    assert ckpt.maybe_write(subagent) is False
    record = store.load("sa-coaras-test")
    assert record is not None and len(record.message_history) == 1

    # 强制写（窗口过后等价行为）：中间进展落盘
    ckpt.write(subagent)
    record = store.load("sa-coaras-test")
    assert record is not None and len(record.message_history) == 2


def test_checkpoint_then_crash_recovers_to_resumable(tmp_path: Path) -> None:
    """中途被杀：断点带中间进展，启动对账标 failed 后 resume 可受理"""
    store = SubagentStore(tmp_path)
    subagent = _fake_subagent(
        [
            Message(role=MessageRole.USER, content="干活"),
            Message(role=MessageRole.ASSISTANT, content="中间进展"),
        ]
    )
    ckpt = _checkpointer(store)
    ckpt.maybe_write(subagent)

    # 进程死亡 → 重启对账
    assert store.recover_stale_running() == 1
    record = store.load("sa-coaras-test")
    assert record is not None
    assert record.status == SubagentStatus.FAILED.value
    assert len(record.message_history) == 2
    assert record.message_history[1]["content"] == "中间进展"
