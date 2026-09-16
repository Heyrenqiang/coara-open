"""主⇄子双向消息通道测试（REMAINING_ISSUES #330）。

覆盖：标签体系、delegate action=message、子→主 report 工具、前台 delegate 的
task list/stop 覆盖、环境说明节、通道门控（仅前台 coaras）。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.message_tags import (
    MIDRUN_MSG_OPEN,
    SUBAGENT_MSG_OPEN,
    is_preformatted_injection,
    midrun_message,
    subagent_message,
)
from src.core.types import MessageRole, ToolCall
from src.llm.provider import LLMResponse
from src.tools.builtin.communication.interact import InteractTool
from src.tools.builtin.delegate import delegate as delegate_mod
from src.tools.builtin.delegate.delegate import (
    DelegateToolInvocation,
    delegate_system_prompt,
)
from tests.helpers import FakeProvider, make_test_coara


@pytest.fixture(autouse=True)
def _clean_registries():
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()
    yield
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------


def test_subagent_message_wraps_with_task_id() -> None:
    text = subagent_message("已读完三个文件", task_id="sa-coaras-ab12", description="摸底")
    assert text.startswith(SUBAGENT_MSG_OPEN)
    assert "[sa-coaras-ab12] 摸底" in text
    assert "已读完三个文件" in text
    assert is_preformatted_injection(text)


def test_main_session_message_is_preformatted() -> None:
    text = midrun_message("停下 先别改")
    assert text.startswith(MIDRUN_MSG_OPEN)
    assert is_preformatted_injection(text)


# ---------------------------------------------------------------------------
# action=message 校验与送达
# ---------------------------------------------------------------------------


def test_message_action_requires_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        DelegateToolInvocation({"action": "message", "prompt": "hi"}, None)


def test_message_action_requires_prompt() -> None:
    with pytest.raises(ValueError, match="prompt"):
        DelegateToolInvocation({"action": "message", "task_id": "sa-coaras-x"}, None)


def test_spawn_action_keeps_original_required_fields() -> None:
    with pytest.raises(ValueError, match="subagent_type"):
        DelegateToolInvocation({"description": "d", "prompt": "p"}, None)


def test_resume_action_preserves_prompt() -> None:
    """resume 的 prompt 是已完成任务的追加指令 必须活到 _execute_resume——
    曾被构造期硬置空 追加任务静默丢失 子智能体只重报旧结果。"""
    inv = DelegateToolInvocation({"action": "resume", "task_id": "sa-coaras-x", "prompt": "追加：把风险清掉"}, None)
    assert inv.prompt == "追加：把风险清掉"
    inv2 = DelegateToolInvocation({"action": "resume", "task_id": "sa-coaras-x"}, None)
    assert inv2.prompt == ""


def test_message_action_unknown_action_rejected() -> None:
    with pytest.raises(ValueError, match="未知 action"):
        DelegateToolInvocation({"action": "teleport", "prompt": "p"}, None)


@pytest.mark.asyncio
async def test_execute_message_unknown_task_id_reports_error() -> None:
    """message 发给不在运行中的子智能体：明确报错并指向 resume。

    「补一句话」与「追加任务再跑一轮」代价不同（后者会再花一轮 LLM），落空时不
    静默替换动作——这正是 09-13 的修正点。
    """
    inv = DelegateToolInvocation({"action": "message", "task_id": "sa-coaras-gone", "prompt": "停"}, None)
    result = await inv._execute_message()
    assert result.is_error
    assert "不在运行中" in result.content
    assert "resume" in result.content


@pytest.mark.asyncio
async def test_execute_message_delivers_to_subagent_queue() -> None:
    subagent = SimpleNamespace(_continuation_inputs=[])

    def submit(text: str) -> None:
        subagent._continuation_inputs.append(text)

    subagent.submit_continuation_input = submit
    delegate_mod._RUNNING_SUBAGENTS["sa-coaras-live"] = subagent

    inv = DelegateToolInvocation({"action": "message", "task_id": "sa-coaras-live", "prompt": "换个方向"}, None)
    result = await inv._execute_message()
    assert not result.is_error
    assert len(subagent._continuation_inputs) == 1
    assert subagent._continuation_inputs[0].startswith(MIDRUN_MSG_OPEN)
    assert "换个方向" in subagent._continuation_inputs[0]


# ---------------------------------------------------------------------------
# 子→主 interact 工具（显式发消息，替代旧的中间输出自动转发）
# ---------------------------------------------------------------------------


def _make_invocation(subagent_type: str = "coaras", **params: Any) -> DelegateToolInvocation:
    parent = SimpleNamespace(
        _continuation_inputs=[],
        submit_continuation_input=lambda text: parent._continuation_inputs.append(text),
        _emit_trace=lambda *a, **k: None,
    )
    inv = DelegateToolInvocation(
        {"description": "长任务", "prompt": "干活", "subagent_type": subagent_type, **params},
        parent,
    )
    return inv


def _make_subagent(task_id: str) -> SimpleNamespace:
    subagent = SimpleNamespace(identity=SimpleNamespace(name=task_id))
    return subagent


def test_interact_pushes_tagged_message_to_parent(tmp_path: Path) -> None:
    parent = _make_invocation()._parent
    subagent = _make_subagent("sa-coaras-r1")

    async def _run() -> Any:
        return (
            await InteractTool(parent_coara=subagent, parent_session=parent)
            .create_invocation({"message": "已完成第一步"})
            .execute()
        )

    result = asyncio.run(_run())
    assert not result.is_error
    assert len(parent._continuation_inputs) == 1
    block = parent._continuation_inputs[0]
    assert block.startswith(SUBAGENT_MSG_OPEN)
    assert "[sa-coaras-r1]" in block
    assert "已完成第一步" in block


def test_interact_requires_active_parent_channel(tmp_path: Path) -> None:
    from src.tools.builtin.communication.interact import InteractTool

    subagent = _make_subagent("sa-coaras-orphan")

    async def _run() -> Any:
        return await InteractTool(parent_coara=subagent).create_invocation({"message": "hi"}).execute()

    result = asyncio.run(_run())
    assert result.is_error
    assert "通道" in result.content


def test_deliver_final_sets_final_message_without_injecting_parent(tmp_path: Path) -> None:
    # deliver 是 flow 节点交付工具（message+next，调用即交付），不注入父会话
    from src.tools.builtin.communication.deliver import DeliverTool

    parent = _make_invocation()._parent
    subagent = _make_subagent("sa-coaras-f1")

    async def _run() -> Any:
        return await DeliverTool(parent_coara=subagent).create_invocation({"message": "任务完成，结果如下"}).execute()

    result = asyncio.run(_run())
    assert not result.is_error
    assert subagent._final_deliver_message == "任务完成，结果如下"
    assert parent._continuation_inputs == []  # 交付不注入父会话（结果随节点输出）


def test_interact_interim_injects_parent(tmp_path: Path) -> None:
    from src.tools.builtin.communication.interact import InteractTool

    parent = _make_invocation()._parent
    subagent = _make_subagent("sa-coaras-f0")

    async def _run() -> Any:
        return (
            await InteractTool(parent_coara=subagent, parent_session=parent)
            .create_invocation({"message": "进展正常"})
            .execute()
        )

    result = asyncio.run(_run())
    assert not result.is_error
    assert getattr(subagent, "_final_deliver_message", None) is None
    assert len(parent._continuation_inputs) == 1


async def test_turn_ends_after_report_final(tmp_path: Path) -> None:
    """deliver 执行后回合立即结束：不进入下一轮 LLM，结果落 _final_deliver_message。
    （deliver 是 flow 节点交付工具；interact 不参与此路径）"""
    from src.tools.builtin.communication.deliver import DeliverTool

    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="deliver", arguments={"message": "最终结果：全部完成"})],
            ),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(DeliverTool(parent_coara=coara))
    await coara.initialize()

    async for _chunk in coara.process_message("执行任务"):
        pass

    # 消费点不全局清空：flow 节点在 process_message 返回后、经
    # flow_coordinator._run_node 读 _final_deliver_message 提取结果（节点激活前
    # _run_node:744 已重置，防跨激活残留）。此处校验 deliver 结果落标记、
    # 回合在 deliver 后立即结束（未进入下一轮 LLM）。
    assert coara._final_deliver_message == "最终结果：全部完成"
    assert provider._responses == []  # 只消费一个响应：report final 后未进入下一轮 LLM
    assert any(m.role == MessageRole.TOOL_RESULT and m.name == "deliver" for m in coara.message_history)


def test_interact_rejects_empty_message(tmp_path: Path) -> None:
    from src.tools.builtin.communication.interact import InteractTool

    parent = _make_invocation()._parent
    subagent = _make_subagent("sa-coaras-r2")

    async def _run() -> Any:
        tool = InteractTool(parent_coara=subagent, parent_session=parent)
        return await tool.create_invocation({"message": "  "}).execute()

    result = asyncio.run(_run())
    assert result.is_error
    assert parent._continuation_inputs == []


def test_interact_skipped_for_non_channel_subagents(tmp_path: Path) -> None:
    # janitor / system dispatch 无通道：注册时不绑 parent_session，调用即报错
    from src.tools.builtin.communication.interact import InteractTool

    subagent = _make_subagent("sa-janitor-x")

    async def _run() -> Any:
        return await InteractTool(parent_coara=subagent).create_invocation({"message": "hi"}).execute()

    assert asyncio.run(_run()).is_error


# ---------------------------------------------------------------------------
# 通道门控（仅前台 coaras）
# ---------------------------------------------------------------------------


def test_channel_enabled_for_all_task_coaras() -> None:
    # 通道开放给任务型前台子智能体 coaras/aide（coaras 只能前台，background=True 直接拒绝）；
    # 后台 aide / janitor / 系统派发无通道
    assert _make_invocation("coaras")._channel_enabled() is True
    assert _make_invocation("aide")._channel_enabled() is True
    assert _make_invocation("aide", background=True)._channel_enabled() is False
    assert _make_invocation("janitor", system_dispatch=True)._channel_enabled() is False
    assert _make_invocation("coaras", system_dispatch=True)._channel_enabled() is False


def test_coaras_background_rejected() -> None:
    # coaras 不允许后台运行：LLM 传 background=true 直接报错，引导改用前台或派 aide
    import pytest

    with pytest.raises(ValueError, match="coaras 只能前台"):
        _make_invocation("coaras", background=True)


# ---------------------------------------------------------------------------
# 环境说明节
# ---------------------------------------------------------------------------


def test_delegate_system_prompt_returns_template_unchanged() -> None:
    # Channel section + 不要中间输出 discipline are written directly in the .md
    # files, so delegate_system_prompt no longer rewrites the template.
    assert delegate_system_prompt("你是 coaras", channel=True) == "你是 coaras"
    assert delegate_system_prompt("你是 janitor", channel=False) == "你是 janitor"


def test_coaras_md_contains_channel_and_no_intermediate() -> None:
    from src.coara.builtin_agents import _load_from_md

    sp = _load_from_md("coaras").system_prompt
    assert "输出规范" in sp
    assert "interact" in sp
    assert "{{INCLUDE" not in sp


def test_delegate_system_prompt_no_channel_without_placeholder_returns_base() -> None:
    assert delegate_system_prompt("你是 janitor", channel=False) == "你是 janitor"


# ---------------------------------------------------------------------------
# 前台 delegate：子智能体硬停走 delegate(action=stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_stop_cancels_foreground_task() -> None:
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    task = asyncio.create_task(asyncio.sleep(3600))
    tid = "sa-coaras-stop-fg"
    delegate_mod._RUNNING_FG_TASKS[tid] = task
    delegate_mod._RUNNING_FG_DESCRIPTIONS[tid] = "停我"
    try:
        result = await DelegateToolInvocation({"action": "stop", "task_id": tid}).execute()
        assert not result.is_error
        assert tid in result.content
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        assert tid not in delegate_mod._RUNNING_FG_TASKS
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def test_delegate_stop_requires_approval() -> None:
    from src.tools.builtin.delegate.delegate import DelegateTool

    assert DelegateTool.requires_approval({"action": "stop"}) is True
    assert DelegateTool.requires_approval({"action": "spawn"}) is False


# ---------------------------------------------------------------------------
# 完成注入（终稿直接进入主会话，无中间输出自动转发）
# ---------------------------------------------------------------------------


def test_foreground_done_injects_final_result(tmp_path: Path) -> None:
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)
    result = SimpleNamespace(content="最终答案全文", metadata={})
    task = SimpleNamespace(
        cancelled=lambda: False,
        exception=lambda: None,
        result=lambda: result,
    )
    parent.on_foreground_delegate_done("sa-coaras-norm", "任务名", task)
    assert len(parent._continuation_inputs) == 1
    assert "最终答案全文" in parent._continuation_inputs[0].text


def test_foreground_done_skips_cancelled(tmp_path: Path) -> None:
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)
    task = SimpleNamespace(cancelled=lambda: True, exception=lambda: None, result=lambda: None)
    parent.on_foreground_delegate_done("sa-coaras-cxl", "任务名", task)
    assert parent._continuation_inputs == []


# ---------------------------------------------------------------------------
# resume（从断点恢复被打断的子智能体）
# ---------------------------------------------------------------------------


def _seed_resumable_record(tmp_path: Path, task_id: str, *, status: str = "cancelled", mode: str = "foreground"):
    """Persist a breakpoint record the resume path can load."""
    from src.coara.subagent_store import SubagentRecord, SubagentStore

    store = SubagentStore(tmp_path)
    history = [
        {"role": "user", "content": "原始任务指令：分析这个仓库"},
        {
            "role": "assistant",
            "content": "我开始读取目录结构",
            "tool_calls": [{"id": "tc1", "name": "glob", "arguments": {"pattern": "*.py"}}],
        },
    ]
    store.save(
        SubagentRecord(
            agent_id=task_id,
            subagent_type="coaras",
            description="分析仓库",
            message_history=history,
            status=status,
            created_at="2026-08-06T00:00:00",
            updated_at="2026-08-06T00:00:00",
            session_id="sess-1",
            child_coara_id="coara-child",
            mode=mode,
        )
    )
    return store


def test_resume_requires_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        DelegateToolInvocation({"action": "resume"}, None)


def test_resume_unknown_breakpoint_errors(tmp_path: Path) -> None:
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)
    parent.workspace_dir = tmp_path
    inv = DelegateToolInvocation({"action": "resume", "task_id": "sa-coaras-missing"}, parent)

    async def _run() -> Any:
        return await inv._execute_resume()

    result = asyncio.run(_run())
    assert result.is_error
    assert "找不到子智能体断点" in result.content


def test_resume_rejects_non_interrupted_status(tmp_path: Path) -> None:
    from tests.helpers import make_test_coara

    # idle（已完成）现在可恢复追加任务；running 状态仍拒绝
    _seed_resumable_record(tmp_path, "sa-coaras-busy", status="running_foreground")
    parent = make_test_coara(tmp_path)
    parent.workspace_dir = tmp_path
    inv = DelegateToolInvocation({"action": "resume", "task_id": "sa-coaras-busy"}, parent)

    async def _run() -> Any:
        return await inv._execute_resume()

    result = asyncio.run(_run())
    assert result.is_error
    assert "才能恢复" in result.content


def test_resume_allows_aide(tmp_path: Path) -> None:
    """aide 与 coaras 同机制：可 resume，不再被类型拒绝。"""
    from tests.helpers import make_test_coara

    store = _seed_resumable_record(tmp_path, "sa-aide-x", status="cancelled")
    record = store.load("sa-aide-x")
    record.subagent_type = "aide"
    store.save(record)
    parent = make_test_coara(tmp_path)
    parent.workspace_dir = tmp_path
    inv = DelegateToolInvocation({"action": "resume", "task_id": "sa-aide-x"}, parent)

    async def _run() -> Any:
        return await inv._execute_resume()

    result = asyncio.run(_run())
    # 不再因 aide 类型被拒：能走过类型检查，进入实际恢复路径（前台异步恢复）
    assert not result.is_error or "无通道且不支持恢复" not in result.content


# ---------------------------------------------------------------------------
# 未消化消息持久化（取消/中断时封存，resume 恢复）
# ---------------------------------------------------------------------------


def test_subagent_record_pending_inputs_roundtrip(tmp_path: Path) -> None:
    from src.coara.subagent_store import SubagentRecord, SubagentStore

    store = SubagentStore(tmp_path)
    store.save(
        SubagentRecord(
            agent_id="sa-coaras-pi",
            subagent_type="coaras",
            description="d",
            message_history=[],
            status="cancelled",
            created_at="2026-08-07T00:00:00",
            updated_at="2026-08-07T00:00:00",
            session_id="s",
            child_coara_id="c",
            pending_inputs=["<途中消息>\n进度如何？\n</途中消息>"],
        )
    )
    loaded = store.load("sa-coaras-pi")
    assert loaded is not None
    assert loaded.pending_inputs == ["<途中消息>\n进度如何？\n</途中消息>"]


def test_resume_restores_pending_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消时封存的主会话途中消息，resume 时重新入队（不丢）。"""
    from tests.helpers import make_test_coara

    store = _seed_resumable_record(tmp_path, "sa-coaras-pi2", status="cancelled")
    record = store.load("sa-coaras-pi2")
    assert record is not None
    record.pending_inputs = ["<途中消息>\n先做最小可编译单元\n</途中消息>"]
    store.save(record)

    captured: dict[str, list[str]] = {}

    async def _fake_run(self, subagent, subagent_id, subagent_config, tool_whitelist, signal=None):
        captured["queued"] = list(subagent._continuation_inputs)
        from src.core.tool_base import ToolResult

        return ToolResult.success(content="done")

    monkeypatch.setattr(DelegateToolInvocation, "_run_subagent", _fake_run)

    parent = make_test_coara(tmp_path)
    parent.workspace_dir = tmp_path
    inv = DelegateToolInvocation({"action": "resume", "task_id": "sa-coaras-pi2"}, parent)

    async def _run() -> Any:
        result = await inv._execute_resume()
        # 让前台任务跑完一拍，fake _run_subagent 捕获队列
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_run())
    assert not result.is_error
    assert [ci.text for ci in captured.get("queued", [])] == ["<途中消息>\n先做最小可编译单元\n</途中消息>"]
