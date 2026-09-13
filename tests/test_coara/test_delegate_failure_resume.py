"""子智能体失败处理增强：瞬时失败自动续跑一次、判死附 resume 引导。

覆盖任务三件套：
① transient 失败自动续跑一次成功
② transient 连续失败判死且错误文案带 resume 引导
③ permanent 失败不自动续跑
④ CANCELLED 不自动续跑
⑤ 防循环标志（一次 delegate 调用只自动续一次）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.base import CoaraBase
from src.coara.builtin_agents import get_subagent
from src.coara.subagent_store import SubagentStore
from src.core.errors import EmptyResponseError, LLMError
from src.core.types import CoaraStatus, Message, MessageRole
from src.tools.builtin.delegate import delegate as delegate_mod
from src.tools.builtin.delegate.delegate import DelegateToolInvocation
from tests.helpers import make_test_coara


def _coaras_config():
    return get_subagent("coaras")


@pytest.fixture(autouse=True)
def _reset_delegate_registries():
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()
    yield
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()


def _make_invocation(parent: CoaraBase) -> DelegateToolInvocation:
    return DelegateToolInvocation(
        {
            "action": "spawn",
            "description": "失败续跑测试",
            "prompt": "do work",
            "subagent_type": "coaras",
        },
        parent,
    )


def _stub_subagent(tmp_path: Path, name: str) -> CoaraBase:
    sub = make_test_coara(tmp_path / name)
    sub.message_history = [Message(role=MessageRole.USER, content="任务")]
    return sub


@pytest.mark.asyncio
async def test_transient_failure_auto_resume_succeeds(monkeypatch, tmp_path: Path) -> None:
    """① transient（EmptyResponseError）失败 → 自动续跑一次 → 成功按正常完成返回。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    # 第一次跑：判死（EmptyResponseError 瞬时）
    first = _stub_subagent(tmp_path, "sub1")

    async def _first_fail(content: str, **kwargs):
        first.status = CoaraStatus.FAILED
        first._turn_failure = EmptyResponseError("模型返回空响应")
        yield "Error: 空响应"

    first.process_message = _first_fail

    # 续跑重建的子智能体（走 _build_subagent → CoaraBase）：直接成功
    async def _ok_process(self: CoaraBase, content: str, **kwargs):
        self.status = CoaraStatus.IDLE
        self.message_history.append(Message(role=MessageRole.USER, content=content))
        self.message_history.append(Message(role=MessageRole.ASSISTANT, content="续跑完成：最终交付"))
        yield "续跑完成：最终交付"

    monkeypatch.setattr(CoaraBase, "process_message", _ok_process)

    events: list[str] = []
    parent.set_trace_sink(lambda e: events.append(e.event_type))

    subagent_id = "sa-coaras-retry01"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_error is False
    assert result.metadata.get("auto_resumed") is True
    assert "续跑完成" in (result.content or "")
    assert "subagent_retrying" in events
    assert "subagent_complete" in events
    # finally 不得把续跑成功后的 IDLE 覆盖成 FAILED
    store = inv._subagent_store_for_dir(tmp_path / "sub1")
    record = store.load(subagent_id)
    assert record is not None
    assert record.status == "idle"
    restored_history = SubagentStore.deserialize_message_history(record.message_history)
    assert any(
        message.role == MessageRole.ASSISTANT and "续跑完成" in str(message.content)
        for message in restored_history
    )


@pytest.mark.asyncio
async def test_transient_failure_twice_dies_with_resume_guidance(monkeypatch, tmp_path: Path) -> None:
    """② transient 连续失败 → 判死，错误文案带 delegate(action=resume) 引导。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    first = _stub_subagent(tmp_path, "sub1")

    async def _always_fail(self: CoaraBase, content: str, **kwargs):
        self.status = CoaraStatus.FAILED
        self._turn_failure = EmptyResponseError("模型返回空响应")
        yield "Error: 空响应"

    first.process_message = lambda content, **kw: _always_fail(first, content, **kw)
    monkeypatch.setattr(CoaraBase, "process_message", _always_fail)

    subagent_id = "sa-coaras-retry02"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_error is True
    assert f'delegate(action="resume", task_id="{subagent_id}")' in (result.content or "")
    # 自动续跑过一次（防循环标志置位）
    assert inv._auto_resumed_once is True


@pytest.mark.asyncio
async def test_quota_failure_keeps_resume_hint_with_task_id(monkeypatch, tmp_path: Path) -> None:
    """配额耗尽：不自动续跑，但断点 id 仍给出，换模型后可 resume。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    first = _stub_subagent(tmp_path, "sub1")

    async def _quota_fail(content: str, **kwargs):
        first.status = CoaraStatus.FAILED
        first._turn_failure = LLMError(
            "Error code: 403 - You've reached your 5-hour usage limit access_terminated_error"
        )
        yield "Error: usage limit"

    first.process_message = _quota_fail

    subagent_id = "sa-coaras-quota01"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_error is True
    assert inv._auto_resumed_once is False
    assert result.metadata and result.metadata.get("task_id") == subagent_id
    assert f'task_id="{subagent_id}"' in (result.content or "")
    assert "断点已封存" in (result.content or "")
    assert "resume 无意义" not in (result.content or "")


@pytest.mark.asyncio
async def test_permanent_failure_no_auto_resume(monkeypatch, tmp_path: Path) -> None:
    """③ permanent（参数校验 ValueError）失败 → 不自动续跑，直接判死且注明确定性。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    first = _stub_subagent(tmp_path, "sub1")

    async def _perm_fail(self: CoaraBase, content: str, **kwargs):
        self.status = CoaraStatus.FAILED
        self._turn_failure = ValueError("参数校验失败: path 不能为空")
        yield "Error: 参数校验失败"

    first.process_message = lambda content, **kw: _perm_fail(first, content, **kw)
    monkeypatch.setattr(CoaraBase, "process_message", _perm_fail)

    subagent_id = "sa-coaras-retry03"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_error is True
    assert inv._auto_resumed_once is False  # 未触发自动续跑
    assert "确定性失败" in (result.content or "")
    assert "resume 无意义" in (result.content or "")


@pytest.mark.asyncio
async def test_cancelled_no_auto_resume(monkeypatch, tmp_path: Path) -> None:
    """④ CANCELLED（用户取消）绝不自动续跑。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    first = _stub_subagent(tmp_path, "sub1")

    async def _cancel(content: str, **kwargs):
        import asyncio

        await asyncio.sleep(0)
        raise asyncio.CancelledError()
        yield  # noqa: B018 — 让 process_message 是异步生成器（真实签名）

    first.process_message = _cancel

    subagent_id = "sa-coaras-retry04"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_cancelled or "取消" in (result.content or "")
    assert inv._auto_resumed_once is False


@pytest.mark.asyncio
async def test_auto_resume_only_once_guard(monkeypatch, tmp_path: Path) -> None:
    """⑤ 防循环标志：即使第一次失败是 transient，续跑再失败也只判死，不再二次续跑。"""
    parent = make_test_coara(tmp_path)
    inv = _make_invocation(parent)

    first = _stub_subagent(tmp_path, "sub1")

    async def _fail(self: CoaraBase, content: str, **kwargs):
        self.status = CoaraStatus.FAILED
        self._turn_failure = LLMError("等待模型完整响应超过 300 秒（llm.total_timeout_seconds），已中止本次调用")
        yield "Error: 预算耗尽"

    first.process_message = lambda content, **kw: _fail(first, content, **kw)
    monkeypatch.setattr(CoaraBase, "process_message", _fail)

    # 预置：假设此前已自动续跑过一次 → 本次失败不再续跑
    inv._auto_resumed_once = True
    subagent_id = "sa-coaras-retry05"
    result = await inv._run_subagent(first, subagent_id, subagent_config=_coaras_config(), tool_whitelist=set())

    assert result.is_error is True
    # 预算耗尽属 transient → 判死文案带 resume 引导
    assert f'task_id="{subagent_id}"' in (result.content or "")


def test_classify_budget_error_is_transient() -> None:
    """预算耗尽（with_retry 的 _budget_error）应判 transient。"""
    from src.tools.builtin.delegate.failure_classifier import classify_subagent_failure

    exc = LLMError("等待模型完整响应超过 300 秒（llm.total_timeout_seconds），已中止本次调用")
    assert classify_subagent_failure(exc) == "transient"


def test_classify_quota_exhausted_is_permanent() -> None:
    """配额耗尽（402/insufficient_quota）应判 permanent。"""
    from src.tools.builtin.delegate.failure_classifier import classify_subagent_failure

    exc = LLMError("Error code: 402 - insufficient_balance")
    assert classify_subagent_failure(exc) == "permanent"


def test_classify_unknown_llm_error_is_permanent() -> None:
    """未分类 LLMError（新 provider 未进 _is_retryable_exception 的错误）一律
    permanent——不拿用户任务冒险自动重试（评审 P2 语义钉死）。"""
    from src.tools.builtin.delegate.failure_classifier import classify_subagent_failure

    exc = LLMError("Error code: 418 - provider-specific weirdness")
    assert classify_subagent_failure(exc) == "permanent"
    # 完全未分类的非 LLM 异常同样 permanent
    assert classify_subagent_failure(RuntimeError("unexpected")) == "permanent"
