"""草案画布概况生成与构建对话注入的行为测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.turn_loop.user_turn_injectors import UserTurnContext, inject_flow_draft_overview
from src.workflow.draft_overview import build_draft_overview
from src.workflow.draft_store import WorkflowDraftStore

_WDL = """\
name: 市场调研
nodes:
  起点:
    task: 明确调研主题为信丰脐橙行情
  收集资料:
    task: 检索信丰脐橙今年行情，整理要点
  汇总成文:
    task: 把 {{steps.收集资料.text}} 写成 200 字短文
    provider: kimi
    model: k3
  复核:
    task: 检查事实错误
edges:
  - from: 起点
    to: 收集资料
  - from: 收集资料
    to: 汇总成文
  - from: 汇总成文
    to: 复核
  - from: 复核
    to: 收集资料
    on: error
"""


@pytest.fixture
def draft(tmp_path, monkeypatch):
    monkeypatch.setenv("COARA_HOME", str(tmp_path / "home"))
    store = WorkflowDraftStore()
    return store.save(_WDL, source_subagent="root")


def test_overview_renders_canvas(draft):
    text = build_draft_overview(draft)
    assert text is not None
    assert draft.draft_id in text
    assert "市场调研" in text
    assert "4 节点 / 4 边" in text
    assert "汇总成文（kimi/k3）" in text
    assert "收集资料 → 汇总成文" in text
    assert "复核 --error--> 收集资料" in text
    assert "入口：起点" in text


def _flow_coara(draft_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        _session_agent_kind="flow",
        _root_coara=SimpleNamespace(_web_server=SimpleNamespace(active_workflow_draft_id=draft_id)),
        message_history=[],
    )


@pytest.mark.asyncio
async def test_injects_once_and_reinjects_on_change(draft):
    coara = _flow_coara(draft.draft_id)
    ctx = UserTurnContext(content="继续")

    await inject_flow_draft_overview(coara, ctx)
    assert len(coara.message_history) == 1
    note = str(coara.message_history[0].content)
    assert note.startswith("<系统消息>")
    assert "当前草案概况" in note
    assert "市场调研" in note

    # 未变化不重复注入
    await inject_flow_draft_overview(coara, ctx)
    assert len(coara.message_history) == 1

    # 画布变更（加节点）→ 再次注入
    store = WorkflowDraftStore()
    store.save(_WDL.replace("  复核:", "  复核:\n    routes: one"), draft_id=draft.draft_id)
    await inject_flow_draft_overview(coara, ctx)
    assert len(coara.message_history) == 2
    assert "路由 one" in str(coara.message_history[1].content)


@pytest.mark.asyncio
async def test_skips_non_flow_and_resets_on_missing_draft(draft):
    ctx = UserTurnContext(content="hi")

    # 主会话不注入
    main = SimpleNamespace(_session_agent_kind="", message_history=[])
    await inject_flow_draft_overview(main, ctx)
    assert main.message_history == []

    # 无打开草案
    coara = _flow_coara("")
    await inject_flow_draft_overview(coara, ctx)
    assert coara.message_history == []

    # 草案被删：复位指纹，后续同 id 恢复时会重新注入
    coara = _flow_coara(draft.draft_id)
    await inject_flow_draft_overview(coara, ctx)
    assert len(coara.message_history) == 1
    WorkflowDraftStore().delete(draft.draft_id)
    await inject_flow_draft_overview(coara, ctx)
    assert len(coara.message_history) == 1
    assert coara._flow_draft_overview_fp == ""
