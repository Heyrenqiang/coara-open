"""#331: 未预期异常整轮回滚时注入磁盘副作用注记（回滚不撤销已落盘的工具改动）"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.coara.subagent_store import SubagentRecord, SubagentStore
from src.coara.turn_orchestrator import (
    _build_rollback_side_effects_note,
    _collect_delegate_effects,
    _format_delegate_effects_detail,
)
from src.core.time import now_iso
from src.core.types import MessageRole, SubagentStatus, ToolCall
from src.llm.provider import LLMResponse
from tests.helpers import FakeProvider, make_test_coara


def test_side_effects_note_lists_tools_and_files() -> None:
    note = _build_rollback_side_effects_note(
        RuntimeError("boom"),
        file_effects=[("write", "D:/ws/a.txt"), ("edit", "D:/ws/b.py"), ("edit", "D:/ws/b.py")],
        other_tools=["read", "read", "grep"],
    )

    assert "RuntimeError" in note
    assert "- write: D:/ws/a.txt" in note
    # 同一工具同一文件只列一次
    assert note.count("- edit: D:/ws/b.py") == 1
    assert "read×2" in note
    assert "grep" in note
    assert "仍然生效" in note


def test_side_effects_note_lists_failed_delegate_task_ids() -> None:
    note = _build_rollback_side_effects_note(
        RuntimeError("boom"),
        file_effects=[],
        other_tools=[],
        delegates=[("sa-coaras-20ad47cc", "修复 matrix 内核侧问题包", "failed")],
    )
    assert "sa-coaras-20ad47cc" in note
    assert "resume" in note
    assert "修复 matrix" in note
    assert "断点" in note


def test_side_effects_note_empty_without_tool_activity() -> None:
    assert (
        _build_rollback_side_effects_note(
            RuntimeError("boom"),
            file_effects=[],
            other_tools=[],
        )
        == ""
    )


@pytest.mark.asyncio
async def test_rollback_injects_side_effects_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.txt"
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", name="write", arguments={"path": str(target), "contents": "磁盘已改"})
                ],
            ),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    def _boom(*args, **kwargs):
        raise RuntimeError("history apply exploded")

    monkeypatch.setattr("src.coara.turn_orchestrator.apply_tool_results_to_history", _boom)

    with pytest.raises(RuntimeError, match="history apply exploded"):
        _ = [chunk async for chunk in coara.process_message("请写入文件")]

    # 磁盘副作用不回滚：write 已实际落盘
    assert target.read_text(encoding="utf-8") == "磁盘已改"
    # 整轮历史已回滚：本轮用户消息与工具结果均不在历史中
    assert not any(message.role == MessageRole.TOOL_RESULT for message in coara.message_history)
    assert not any(message.content == "请写入文件" for message in coara.message_history)
    # 但留下副作用注记：工具名 + 目标文件清单，告知下一轮模型磁盘已改
    last = coara.message_history[-1]
    assert last.role == MessageRole.USER
    assert "write" in last.content
    assert str(target) in last.content
    assert "仍然生效" in last.content


def _delegate_execution(
    task_id: str,
    *,
    is_error: bool = False,
    is_cancelled: bool = False,
    mode: str | None = None,
    outcome: str | None = None,
    description: str = "任务",
):
    """Build a minimal delegate execution stub consumed by _collect_delegate_effects."""
    metadata: dict = {"task_id": task_id, "subagent_id": task_id, "description": description}
    if mode is not None:
        metadata["mode"] = mode
    if outcome is not None:
        metadata["outcome"] = outcome
    result = SimpleNamespace(metadata=metadata, content="ok", is_error=is_error, is_cancelled=is_cancelled)
    tool_call = SimpleNamespace(name="delegate", arguments={"description": description})
    return SimpleNamespace(tool_call=tool_call, result=result)


def _seed_store_record(tmp_path: Path, task_id: str, status: str) -> None:
    """Persist a subagent breakpoint record under the workspace store."""
    now = now_iso()
    SubagentStore(tmp_path).save(
        SubagentRecord(
            agent_id=task_id,
            subagent_type="coaras",
            description="任务",
            message_history=[{"role": "user", "content": "干活"}],
            status=status,
            created_at=now,
            updated_at=now,
            session_id="s1",
            child_coara_id="c1",
        )
    )


def test_delegate_effects_maps_store_status_when_receipt_has_no_outcome(tmp_path: Path) -> None:
    """spawn 回执成功但 metadata 无 outcome：按 SubagentStore 真实状态标注，不再一律已完成。"""
    _seed_store_record(tmp_path, "sa-coaras-92851921", SubagentStatus.FAILED.value)
    _seed_store_record(tmp_path, "sa-coaras-run", SubagentStatus.RUNNING_FOREGROUND.value)
    _seed_store_record(tmp_path, "sa-coaras-idle", SubagentStatus.IDLE.value)
    coara = SimpleNamespace(workspace_dir=tmp_path, workspace_manager=None)
    delegates: list[tuple[str, str, str]] = []
    _collect_delegate_effects(
        [
            # 死于 kimi 403：真实状态 FAILED，不得再标成「已完成」
            _delegate_execution("sa-coaras-92851921"),
            _delegate_execution("sa-coaras-run"),
            _delegate_execution("sa-coaras-idle"),
            # store 查不到（已清理/从未落盘）：按回执现状回退 ok
            _delegate_execution("sa-coaras-gone"),
            # 显式 outcome / 后台 mode：走回执分支，不查 store
            _delegate_execution("sa-coaras-explicit", outcome="ok"),
            _delegate_execution("sa-coaras-bg", mode="background"),
        ],
        delegates,
        coara=coara,
    )
    by_id = {tid: outcome for tid, _, outcome in delegates}
    assert by_id["sa-coaras-92851921"] == "failed"
    assert by_id["sa-coaras-run"] == "running"
    assert by_id["sa-coaras-idle"] == "ok"
    assert by_id["sa-coaras-gone"] == "ok"
    assert by_id["sa-coaras-explicit"] == "ok"
    assert by_id["sa-coaras-bg"] == "background"


def test_delegate_effects_without_coara_context_falls_back_to_ok() -> None:
    """无 coara 上下文（旧调用方/直调）：store 不可用，保持旧语义兜底 ok。"""
    delegates: list[tuple[str, str, str]] = []
    _collect_delegate_effects([_delegate_execution("sa-coaras-x")], delegates)
    assert delegates == [("sa-coaras-x", "任务", "ok")]


def test_delegate_note_labels_running_outcome() -> None:
    """注记对 running outcome 给出「运行中（断点可 resume）」文案。"""
    detail = _format_delegate_effects_detail([("sa-coaras-abc", "做某事", "running")])
    assert "sa-coaras-abc" in detail
    assert "运行中（断点可 resume）" in detail
