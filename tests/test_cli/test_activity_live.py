"""ActivityLiveTracker status-line behavior."""

from __future__ import annotations

from src.cli.activity_live import ActivityLiveTracker
from src.core.events import TraceEvent


def test_delegate_wait_block_is_suppressed() -> None:
    """delegate(action=wait) 是内部同步点：不建 live 块，也不留 scrollback 尾巴。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="delegate",
            payload={
                "tool_call_id": "call-w1",
                "tool_name": "delegate",
                "arguments": {"action": "wait"},
                "coara_id": "root-1",
            },
        )
    )
    assert tracker.get_status_lines() == []

    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="delegate",
            payload={
                "tool_call_id": "call-w1",
                "tool_name": "delegate",
                "is_error": False,
            },
        )
    )
    assert tracker.flush_finished_blocks() == []


def test_thinking_progress_does_not_render_in_status_lines() -> None:
    """子智能体思考预览不进动态区（内心独白不是状态，多分身会互相覆盖）。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "explore-1",
                "subagent_type": "explore",
                "description": "审计文档",
            },
        )
    )
    before = tracker.get_status_lines()

    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="thinking_progress",
            message="thinking",
            payload={"coara_id": "explore-1", "content_preview": "所有证据已齐。标记完成并输出报告"},
        )
    )
    after = tracker.get_status_lines()

    assert after == before
    assert not any("所有证据已齐" in line for line in after)


def test_delegate_heartbeat_shows_latest_turn_llm_tokens() -> None:
    """子智能体 llm_turn_complete 的 usage 以最后一轮（context 口径）显示在 delegate 行括号里。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="delegate",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "arguments": {"subagent_type": "explore", "description": "摸底"},
                "coara_id": "root-1",
            },
        )
    )
    # Foreground-async spawn ack must NOT kill the live paren.
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="spawned",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "is_error": False,
                "delegate_mode": "foreground_async",
                "delegate_task_id": "sa-1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "explore-1",
                "subagent_type": "explore",
                "description": "摸底",
                "parent_tool_call_id": "call-d1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="llm_turn_complete",
            message="Model turn completed",
            payload={
                "session_id": "sub-session",
                "llm_output": {
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cached_tokens": 800,
                    }
                },
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="llm_turn_complete",
            message="Model turn completed",
            payload={
                "session_id": "sub-session",
                "llm_output": {
                    "usage": {
                        "input_tokens": 1500,
                        "output_tokens": 100,
                        "cached_tokens": 1200,
                    }
                },
            },
        )
    )

    lines = tracker.get_status_lines()
    joined = "\n".join(lines)
    assert "delegate explore: 摸底" in joined
    # Single status line: ``delegate explore: …  (… · 1.5k tok · cache 80%)``
    # context 口径：只显示最后一轮 prompt（1500），不累计（累计会是 2.8k）。
    assert "1.5k tok" in joined
    assert "2.8k tok" not in joined
    assert "cache 80%" in joined
    assert "(" in joined and ")" in joined
    # Elapsed seconds always present even before/with usage.
    assert "s · " in joined or "s)" in joined
    # No legacy heartbeat child row
    assert "已运行" not in joined
    # Spawn ack must not append the inactive ``...`` suffix.
    assert not any(line.endswith("...") for line in lines)


def test_delegate_usage_is_latest_turn_not_cumulative() -> None:
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="delegate",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "arguments": {"subagent_type": "explore", "description": "摸底"},
                "coara_id": "root-1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="spawned",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "is_error": False,
                "delegate_mode": "foreground_async",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "explore-1",
                "subagent_type": "explore",
                "description": "摸底",
                "parent_tool_call_id": "call-d1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="llm_turn_complete",
            message="done",
            payload={"llm_output": {"usage": {"input_tokens": 1000, "output_tokens": 10, "cached_tokens": 800}}},
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="llm_turn_complete",
            message="done",
            payload={
                "llm_output": {
                    # Last turn alone: prompt 2000, cache 1000/2000 = 50%；
                    # 累计口径会是 (800+1000)/(1000+2000)=60%、3.0k tok —— 都不要。
                    "usage": {"input_tokens": 2000, "output_tokens": 10, "cached_tokens": 1000}
                }
            },
        )
    )
    joined = "\n".join(tracker.get_status_lines())
    assert "cache 50%" in joined
    assert "cache 60%" not in joined
    assert "2.0k tok" in joined
    assert "3.0k tok" not in joined


def test_janitor_silent_no_status_or_flush() -> None:
    """janitor tools stay off the live tree and do not flush ✓ lines."""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="background_agent_start",
            message="start",
            payload={
                "task_id": "sa-janitor-1",
                "child_coara_id": "janitor-1",
                "subagent_type": "janitor",
                "description": "janitor 首次生成 @v8",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="janitor-1",
            coara_name="janitor",
            event_type="tool_start",
            message="read",
            payload={
                "tool_call_id": "call-r1",
                "tool_name": "read",
                "arguments": {"path": "D:/x/session_history_janitor.json"},
                "coara_id": "janitor-1",
            },
        )
    )
    assert tracker.get_status_lines() == []
    assert tracker.has_active_blocks() is False

    tracker.ingest(
        TraceEvent(
            coara_id="janitor-1",
            coara_name="janitor",
            event_type="tool_complete",
            message="done",
            payload={
                "tool_call_id": "call-r1",
                "tool_name": "read",
                "is_error": False,
                "coara_id": "janitor-1",
            },
        )
    )
    assert tracker.flush_finished_blocks() == []


def test_foreground_async_delegate_stays_live_until_subagent_complete() -> None:
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="delegate",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "arguments": {"subagent_type": "coaras", "description": "并行", "workspace": "v8"},
                "coara_id": "root-1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="spawned",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "is_error": False,
                "delegate_mode": "foreground_async",
                "delegate_task_id": "sa-1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "child-1",
                "subagent_type": "coaras",
                "description": "并行",
                "parent_tool_call_id": "call-d1",
            },
        )
    )
    lines = tracker.get_status_lines()
    assert len(lines) == 1
    assert " (0s)" in lines[0] or " (0s ·" in lines[0]
    assert not lines[0].endswith("...")

    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_complete",
            message="done",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "child-1",
                "parent_tool_call_id": "call-d1",
            },
        )
    )
    assert tracker.get_status_lines() == []


def test_flow_container_and_pending_node_render_gray() -> None:
    """flow_started 建容器行；pending 节点行灰色静态（spinner 不转），run 后变亮色。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="flow_started",
            message="flow demo 已创建",
            payload={"flow": "demo"},
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="Flow 节点登记：a",
            payload={
                "subagent_id": "flow-demo-a",
                "subagent_type": "coaras",
                "description": "flow demo · a",
                "parent_activity_id": "flow-demo",
                "status": "pending",
            },
        )
    )

    lines = tracker.get_status_lines()
    styles = tracker.get_status_styles()
    assert len(lines) == 2
    # 容器行：全停车 → 灰色
    assert lines[0].startswith("· flow demo")
    assert styles[0] == "class:prompt.subagent.pending"
    # 节点行：pending → 灰色静态符号，不带 ◌（spinner 不转）
    assert lines[1].startswith("  · coaras(flow demo · a)")
    assert styles[1] == "class:prompt.subagent.pending"

    # run 点火：同一节点行变亮色转圈
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="Flow 节点启动：a",
            payload={
                "subagent_id": "flow-demo-a",
                "subagent_type": "coaras",
                "description": "flow demo · a",
                "parent_activity_id": "flow-demo",
                "status": "",
            },
        )
    )
    lines = tracker.get_status_lines()
    styles = tracker.get_status_styles()
    assert lines[1].startswith("  ⎿ coaras(flow demo · a)")
    assert styles[1] == ""
    # 容器行：有运行子节点 → 亮色
    assert lines[0].startswith("◌ flow demo")
    assert styles[0] == ""


def test_flow_finished_closes_container() -> None:
    """flow_finished 收容器行：节点完成刷入历史后整树清空。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="flow_started",
            message="flow demo 已创建",
            payload={"flow": "demo"},
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="Flow 节点登记：a",
            payload={
                "subagent_id": "flow-demo-a",
                "subagent_type": "coaras",
                "description": "flow demo · a",
                "parent_activity_id": "flow-demo",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_complete",
            message="Flow 节点完成：a",
            payload={
                "subagent_id": "flow-demo-a",
                "parent_activity_id": "flow-demo",
            },
        )
    )
    # 节点已完成但容器还在 → 容器行仍在（灰）
    assert tracker.get_status_lines() != []
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="flow_finished",
            message="flow demo 完成",
            payload={"flow": "demo"},
        )
    )
    assert tracker.get_status_lines() == []


def test_stale_node_pruned_after_timeout() -> None:
    """对账兜底：无委派容器的孤立节点超长静默（> _STALE_NODE_TIMEOUT_S）仍判 inactive，
    防终态帧丢失后子树僵死（原 120s 阈值；运行中的 delegate 子树受保护不在此列，见另测）。"""
    import time

    from src.cli.activity_live import _STALE_NODE_TIMEOUT_S

    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-stale",
                "child_coara_id": "sub-stale",
                "subagent_type": "explore",
                "description": "陈旧节点",
            },
        )
    )
    # 节点活跃
    assert tracker.get_status_lines() != []
    # 手动把 last_event 拨回超长静默（模拟丢帧后长时间无更新）
    node = tracker._nodes["sa-stale"]
    node.last_event = time.monotonic() - (_STALE_NODE_TIMEOUT_S + 1.0)
    # 触发一次 prune（任意 ingest 或显式调用）
    tracker._prune_inactive()
    # 节点已 inactive，子树不再显示
    assert tracker.get_status_lines() == []


def test_resync_clears_activity_tree_but_keeps_finished_blocks() -> None:
    """P1-4：断连重连后 resync 清活动树（不清 _finished_blocks），与服务端权威对齐。"""
    tracker = ActivityLiveTracker()
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="read",
            payload={"tool_call_id": "t-1", "tool_name": "read", "coara_id": "root-1"},
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="read done",
            payload={"tool_call_id": "t-1", "tool_name": "read", "is_error": False},
        )
    )
    # 已完成工具块待 flush
    assert tracker._finished_blocks
    # 活跃节点（tool_start 无 parent → 根节点）
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={"subagent_id": "sa-1", "subagent_type": "explore", "description": "摸底"},
        )
    )
    assert tracker.get_status_lines() != []
    # resync：清活动树，保留已完成块
    tracker.resync()
    assert tracker.get_status_lines() == []
    assert tracker._finished_blocks  # 已完成块不清（仍需 flush 到滚动区）


def _seed_running_delegate(tracker: ActivityLiveTracker) -> None:
    """建一棵运行中的 delegate 树：delegate(call-d1, async ack 后仍 active) → subagent(sa-1)。"""
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_start",
            message="delegate",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "arguments": {"subagent_type": "coaras", "description": "并行"},
                "coara_id": "root-1",
            },
        )
    )
    # Async spawn ack 不关 delegate 行（子智能体还在跑）
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="tool_complete",
            message="spawned",
            payload={
                "tool_call_id": "call-d1",
                "tool_name": "delegate",
                "is_error": False,
                "delegate_mode": "foreground_async",
                "delegate_task_id": "sa-1",
            },
        )
    )
    tracker.ingest(
        TraceEvent(
            coara_id="root-1",
            coara_name="root",
            event_type="subagent_start",
            message="start",
            payload={
                "subagent_id": "sa-1",
                "child_coara_id": "explore-1",
                "subagent_type": "coaras",
                "description": "并行",
                "parent_tool_call_id": "call-d1",
            },
        )
    )


def test_running_delegate_not_pruned_by_temporary_silence() -> None:
    """回归：委派运行中子智能体「暂时无事件」（LLM 单次长调用/长工具）不得被静默兜底误杀。

    即使整棵子树静默超过旧 120s 阈值、甚至超过新 _STALE_NODE_TIMEOUT_S，
    delegate 容器 active 期间（内核仍在等待收口）活动树必须保留。
    """
    import time

    tracker = ActivityLiveTracker()
    _seed_running_delegate(tracker)
    assert tracker.get_status_lines() != []

    now = time.monotonic()
    for node in tracker._nodes.values():
        node.last_event = now - 500.0  # 超过旧 120s 阈值：曾会把整行误杀
    tracker._prune_inactive()
    assert tracker.get_status_lines() != []

    for node in tracker._nodes.values():
        node.last_event = now - 700.0  # 超过新 600s 兜底阈值：active 委派容器仍保护
    tracker._prune_inactive()
    assert tracker.get_status_lines() != []


def test_llm_turn_complete_refreshes_running_delegate_heartbeat() -> None:
    """llm_turn_complete 是子智能体活着推进的强信号：刷新其行并把心跳传播到 delegate 根。"""
    import time

    tracker = ActivityLiveTracker()
    _seed_running_delegate(tracker)
    now = time.monotonic()
    for node in tracker._nodes.values():
        node.last_event = now - 500.0

    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="llm_turn_complete",
            message="Model turn completed",
            payload={"llm_output": {"usage": {"input_tokens": 1000, "output_tokens": 10}}},
        )
    )
    # _touch 沿父链传播：subagent 行与 delegate 根都拿到心跳
    assert tracker._nodes["sa-1"].last_event > now - 60
    assert tracker._nodes["call-d1"].last_event > now - 60
    tracker._prune_inactive()
    assert tracker.get_status_lines() != []


def test_thinking_progress_keeps_silent_delegate_alive() -> None:
    """长 LLM 调用期间 thinking_progress 心跳续命：不渲染到状态行，但刷新判活时间。"""
    import time

    tracker = ActivityLiveTracker()
    _seed_running_delegate(tracker)
    now = time.monotonic()
    for node in tracker._nodes.values():
        node.last_event = now - 500.0

    tracker.ingest(
        TraceEvent(
            coara_id="explore-1",
            coara_name="explore",
            event_type="thinking_progress",
            message="thinking",
            payload={"coara_id": "explore-1", "content_preview": "分析中"},
        )
    )
    assert tracker._nodes["call-d1"].last_event > now - 60
    assert tracker._nodes["sa-1"].last_event > now - 60
    tracker._prune_inactive()
    assert tracker.get_status_lines() != []


def test_orphan_residue_pruned_after_delegate_closed() -> None:
    """防僵死收口仍有效：delegate 已收口（inactive）后子行终态帧丢失 →
    无活跃容器的孤立残留超长静默被回收，子树不再僵死。"""
    import time

    from src.cli.activity_live import _STALE_NODE_TIMEOUT_S

    tracker = ActivityLiveTracker()
    _seed_running_delegate(tracker)
    # 委派容器收口（正常情况下随 subagent_complete 一起；此处模拟容器已关而子行仍残留）
    tracker._finish("call-d1")
    assert tracker.get_status_lines() != []  # 收口瞬间残留子行仍可见

    node = tracker._nodes["sa-1"]
    node.last_event = time.monotonic() - (_STALE_NODE_TIMEOUT_S + 1.0)
    tracker._prune_inactive()
    # 孤立残留回收：树清空（不再僵死）
    assert tracker.get_status_lines() == []


def test_subagent_tool_row_binds_from_flat_attach_frame() -> None:
    """真实 attach 事件帧（coara_id 平铺在帧上、payload 里没有）也要能挂到子智能体节点。

    回归：客户端 RemoteEventBus 把帧里的 coara_id 提到 TraceEvent 属性上，
    ActivityLiveTracker 若只读 payload.coara_id 会恒空 → 子智能体工具行退化成
    depth=0 的根行 → 被 should_flush_tool_to_history 丢弃：既进不了折叠块、
    也不落滚动区（CLI 折叠块「0 项工具」的根因）。
    """
    from src.cli.remote_event_bus import _event_from_ws_frame

    tracker = ActivityLiveTracker()
    tracker.ingest(
        _event_from_ws_frame(
            {
                "type": "tool_start",
                "coara_id": "root-1",
                "tool_call_id": "call-delegate",
                "tool_name": "delegate",
                "arguments": {"action": "spawn", "subagent_type": "coaras", "description": "摸底内核层"},
            }
        )
    )
    tracker.ingest(
        _event_from_ws_frame(
            {
                "type": "subagent_start",
                "coara_id": "root-1",
                "subagent_id": "sa-1",
                "child_coara_id": "sub-1",
                "subagent_type": "coaras",
                "description": "摸底内核层",
                "parent_tool_call_id": "call-delegate",
            }
        )
    )
    tracker.ingest(
        _event_from_ws_frame(
            {
                "type": "tool_start",
                "coara_id": "sub-1",
                "tool_call_id": "call-tool-1",
                "tool_name": "grep",
                "arguments": {"pattern": "x"},
            }
        )
    )
    tracker.ingest(
        _event_from_ws_frame(
            {
                "type": "tool_complete",
                "coara_id": "sub-1",
                "tool_call_id": "call-tool-1",
                "tool_name": "grep",
                "is_error": False,
            }
        )
    )

    blocks = tracker.flush_finished_blocks()
    assert [b.tool_name for b in blocks] == ["grep"]
    # 挂在子智能体节点下（修复前是 depth=0 的根行，此处会被整条丢弃）
    assert blocks[0].depth > 0
    assert blocks[0].owner_id == "call-delegate"
