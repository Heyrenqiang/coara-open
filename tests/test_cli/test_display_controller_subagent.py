"""CliDisplayController 子智能体工具 diff 显示回归测试（内核化后回显契约）。

契约：子智能体的工具调用要回显到发起端（CLI 前台空间）。
- 本空间子智能体（workspace_dir == 前台，session_id 独立 sa-*）的 tool_complete
  diff 走前台暂存（等 ✓ 有序 flush），不再被 session_id 判定漏到即时直渲
- janitor/daily 等系统静默子智能体（cli_silent）的 diff 仍不显示
- 其它（后台）工作空间的工具 diff 仍即时直渲、带 [空间] 标签
- matrix 端不消费 tool 事件（无订阅路径），手机零工具显示不受影响
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.cli.display_controller import CliDisplayController, _PendingFgDiff


def _make_controller(fg_session_id: str = "sess-main", fg_workspace: str = "D:/ws/a") -> CliDisplayController:
    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(
            session_id=fg_session_id,
            workspace_dir=fg_workspace,
            identity=SimpleNamespace(coara_id="coara-main"),
        ),
        identity=SimpleNamespace(coara_id="root-1"),
        has_active_turn=lambda: False,
    )
    console = SimpleNamespace()
    return CliDisplayController(
        root=root,
        console=console,
        subagent_spinner=SimpleNamespace(),
        background_spinner=SimpleNamespace(request_redraw=lambda **kwargs: None),
    )


def _diff_frame(**payload) -> dict:
    """通道 diff 帧（EndRegistry.deliver → cli-attached sender → TurnStream → 客户端）。"""
    base = {
        "kind": "diff",
        "display_blocks": [{"kind": "diff", "lines": ["+x"]}],
        "tool_name": "edit",
    }
    base.update(payload)
    return base


def test_subagent_tool_diff_queues_with_foreground() -> None:
    """diff 帧进前台暂存（等 ✓ 有序 flush）——输出帧统一路由后一律按帧驱动。"""
    ctl = _make_controller()
    ctl.queue_diff_frame(_diff_frame())
    assert len(ctl._pending_fg_diffs) == 1
    item = ctl._pending_fg_diffs[0]
    assert isinstance(item, _PendingFgDiff)
    assert item.display_blocks == [{"kind": "diff", "lines": ["+x"]}]
    assert item.title_prefix == ""  # 前台无 [空间] 前缀


def test_diff_frame_without_blocks_ignored() -> None:
    """无 display_blocks 的 diff 帧忽略（janitor/daily 在源头被过滤，不进通道）。"""
    ctl = _make_controller()
    ctl.queue_diff_frame(_diff_frame(display_blocks=None))
    assert ctl._pending_fg_diffs == []


def test_main_session_diff_still_queued() -> None:
    """主会话 diff 帧同样进前台暂存。"""
    ctl = _make_controller()
    ctl.queue_diff_frame(_diff_frame())
    assert len(ctl._pending_fg_diffs) == 1


def test_flush_pending_fg_diffs_renders_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush 把暂存 diff 按序渲染并清空队列。"""
    rendered: list[dict] = []
    monkeypatch.setattr(
        "src.coara.tool_output.pipeline.render_terminal_from_event",
        lambda **kwargs: rendered.append(kwargs),
    )
    ctl = _make_controller()
    for _ in range(2):
        ctl.queue_diff_frame(_diff_frame())
    assert len(ctl._pending_fg_diffs) == 2
    ctl.flush_pending_fg_diffs()
    assert ctl._pending_fg_diffs == []
    assert len(rendered) == 2


def test_late_diff_frame_no_active_turn_renders_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-1：diff 帧带 turn_id 且当前无本地活跃流（回合已结束）时直接渲染，不暂存。"""
    rendered: list[dict] = []
    monkeypatch.setattr(
        "src.coara.tool_output.pipeline.render_terminal_from_event",
        lambda **kwargs: rendered.append(kwargs),
    )
    ctl = _make_controller()
    # 无活跃流（_turn_streams 无 / 全 done）
    ctl.root.foreground_coara._turn_streams = {}
    ctl.queue_diff_frame(_diff_frame(turn_id="srv-turn-ended"))
    # 不暂存，直接渲染
    assert ctl._pending_fg_diffs == []
    assert len(rendered) == 1
    assert rendered[0]["display_blocks"] == [{"kind": "diff", "lines": ["+x"]}]


def test_late_diff_frame_with_active_turn_still_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-1：diff 帧带 turn_id 且当前有本地活跃流时仍暂存（等 ✓ 有序 flush）。"""
    ctl = _make_controller()
    # 有活跃流（未 done）
    ctl.root.foreground_coara._turn_streams = {"local-1": {"done": False}}
    ctl.queue_diff_frame(_diff_frame(turn_id="srv-turn-active"))
    assert len(ctl._pending_fg_diffs) == 1


def test_diff_frame_no_turn_id_still_queued() -> None:
    """无 turn_id 的 diff 帧（旧协议/兼容路径）仍按原逻辑暂存。"""
    ctl = _make_controller()
    ctl.root.foreground_coara._turn_streams = {}
    ctl.queue_diff_frame(_diff_frame())  # 无 turn_id
    assert len(ctl._pending_fg_diffs) == 1


def _continuation_injected_event(**payload) -> SimpleNamespace:
    base = {"user_texts": [], "user_sources": [], "subagent_texts": [], "subagent_sources": []}
    base.update(payload)
    return SimpleNamespace(event_type="continuation_input_injected", coara_id="coara-main", payload=base)


def _controller_with_echo() -> tuple[CliDisplayController, list[list[str]]]:
    echo: list[list[str]] = []
    ctl = _make_controller()
    ctl.background_spinner = SimpleNamespace(
        write_echo_lines=lambda lines: echo.append(lines),
        request_redraw=lambda: None,
    )
    return ctl, echo


def test_subagent_result_remote_source_not_echoed() -> None:
    """web 回合里前台子智能体收官：subagent_texts 带来源 web，CLI 不回显。"""
    ctl, echo = _controller_with_echo()
    event = _continuation_injected_event(
        subagent_texts=["[sa-coaras-ab12] 摸底仓库\n找到 3 个入口文件"],
        subagent_sources=["web"],
    )
    ctl._on_continuation_input_injected(event)
    assert echo == []


def test_subagent_result_local_source_echoed() -> None:
    """CLI 本端回合的子智能体收官：本端缓冲里没有对应折叠块时照常回显（宁多勿丢）。"""
    ctl, echo = _controller_with_echo()
    event = _continuation_injected_event(
        subagent_texts=["[sa-coaras-ab12] 摸底仓库\n找到 3 个入口文件"],
        subagent_sources=["cli"],
    )
    ctl._on_continuation_input_injected(event)
    assert len(echo) == 1
    assert "[sa-coaras-ab12] 摸底仓库" in echo[0][0]


def test_subagent_result_folds_into_known_block() -> None:
    """能归位到折叠块时不再单打结果行（最终结果是折叠块的一组）。"""
    ctl, echo = _controller_with_echo()
    ctl.fold.note_start(tool_call_id="call-1", subagent_id="sa-coaras-ab12", agent_type="coaras", task="摸底")
    event = _continuation_injected_event(
        subagent_texts=["[sa-coaras-ab12] 摸底仓库\n找到 3 个入口文件"],
        subagent_sources=["cli"],
    )
    ctl._on_continuation_input_injected(event)
    assert len(echo) == 1  # 只剩摘要行，结果行折进块里
    assert "▸ coaras子智能体" in echo[0][0]
    assert "找到 3 个入口文件" not in echo[0][0]


def test_subagent_result_old_payload_fallback_to_active_source() -> None:
    """旧内核 payload 无 subagent_sources：回退当前回合来源过滤（web 回合照样拦）。"""
    ctl, echo = _controller_with_echo()
    event = _continuation_injected_event(subagent_texts=["[sa-aide-ab12] 调研\n结果汇总"])
    ctl.root.foreground_coara._active_turn_source = "web"
    ctl._on_continuation_input_injected(event)
    assert echo == []
    ctl.root.foreground_coara._active_turn_source = "cli"
    ctl._on_continuation_input_injected(event)
    assert len(echo) == 1
