"""CLI 子智能体折叠块回归（口径对齐 web 折叠区）。

用户口径：一次 delegate（子智能体）运行**默认只出一行摘要**，任务指令 / 过程
（工具行 + 过程正文 + diff）/ 最终结果全部折进块里；Ctrl+O 展开/收起最近一块；
`/detail [关键词]` 按类型/任务摘要定位某一块、把明细落进滚动区永久回看。

这三条是硬要求，本文件按它们写回归：
① 一次 delegate 默认只产出一行摘要（过程/结果不落滚动区）
② Ctrl+O 能把三组内容打出来（可控假数据，不依赖真实 LLM）
③ `/detail` 定位（无参＝最近 / 关键词命中 / 无命中提示）与补全候选
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.cli.activity_types import ToolCallBlock
from src.cli.display_controller import CliDisplayController
from src.cli.scrollback import CliScrollback
from src.cli.subagent_fold import (
    DETAIL_CAPACITY,
    DelegateLineFilter,
    FoldEntry,
    SubagentFoldStore,
    detail_items,
    format_summary_line,
    strip_result_echo_from_body,
)

WS = "D:/ws/a"
KEY = "call-delegate-1"
SUBAGENT_ID = "sa-coaras-ab12cd34"
TASK = "排查持久化与调度竞态"
BRIEF = "<任务指令>\n排查持久化与调度竞态\n- 读 src/a.py\n- 读 src/b.py"
BODY = "正在读文件…"
RESULT = "结论：竞态在调度唤醒路径"


def _event(event_type: str, **payload) -> SimpleNamespace:
    payload.setdefault("workspace_dir", WS)
    return SimpleNamespace(event_type=event_type, payload=payload, coara_id="coara-main")


def _make_controller(monkeypatch: pytest.MonkeyPatch) -> tuple[CliDisplayController, list[list[str]], list[str]]:
    echo: list[list[str]] = []
    writes: list[str] = []
    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(
            session_id="sess-main",
            workspace_dir=WS,
            identity=SimpleNamespace(coara_id="coara-main"),
            _active_turn_source="cli-attached",
        ),
        identity=SimpleNamespace(coara_id="root-1"),
        has_active_turn=lambda: False,
    )
    spinner = SimpleNamespace(
        write_echo_lines=lambda lines: echo.append(lines),
        request_redraw=lambda **kwargs: None,
    )
    ctl = CliDisplayController(
        root=root,
        console=SimpleNamespace(),
        subagent_spinner=SimpleNamespace(),
        background_spinner=spinner,
    )
    monkeypatch.setattr(CliScrollback, "write", staticmethod(lambda *args, **kwargs: None))
    monkeypatch.setattr(CliScrollback, "write_markup", staticmethod(lambda text: writes.append(text)))
    monkeypatch.setattr(
        "src.coara.tool_output.pipeline.render_terminal_blocks",
        lambda blocks, **kwargs: writes.append("<diff>"),
    )
    return ctl, echo, writes


def _feed_delegate_run(ctl: CliDisplayController) -> None:
    """喂一次完整的 delegate 运行（开场 → 指令 → 过程 → 结果）。"""
    ctl._on_fold_event(
        _event(
            "tool_start",
            source="cli-attached",
            tool_name="delegate",
            tool_call_id=KEY,
            arguments={"subagent_type": "coaras", "description": TASK},
        )
    )
    ctl._on_fold_event(
        _event(
            "subagent_start",
            source="cli-attached",
            subagent_id=SUBAGENT_ID,
            child_coara_id="coara-sub",
            parent_tool_call_id=KEY,
            subagent_type="coaras",
            description=TASK,
        )
    )
    ctl._on_fold_event(
        _event(
            "user_message",
            source="cli-attached",
            delegate_brief=True,
            parent_tool_call_id=KEY,
            content=BRIEF,
        )
    )
    assert ctl.ingest_tool_block(
        ToolCallBlock("call-read-1", "read", "read(D:/ws/a/src/a.py)", finished=True, depth=2),
        owner_key=KEY,
    )
    assert ctl.ingest_tool_block(
        ToolCallBlock("call-edit-1", "edit", "edit(D:/ws/a/src/b.py)", finished=True, depth=2),
        owner_key=KEY,
    )
    ctl.queue_diff_frame(
        {
            "type": "diff",
            "tool_name": "edit",
            "parent_tool_call_id": KEY,
            "display_blocks": [{"kind": "diff", "lines": ["+x"]}],
        }
    )
    ctl.ingest_subagent_frame({"type": "subagent_chunk", "tool_call_id": KEY, "coara_id": "coara-sub", "text": BODY})
    ctl.ingest_subagent_frame({"type": "subagent_result", "tool_call_id": KEY, "coara_id": "coara-sub", "text": RESULT})


# ── ① 一次 delegate 只产出一行摘要 ─────────────────────────────


def test_delegate_run_emits_single_summary_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """过程（工具行 / 正文 / diff）与最终结果都不落滚动区，只留一行摘要。"""
    ctl, echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)

    assert writes == []  # 过程与结果没有进滚动区
    assert len(echo) == 1  # 唯一一条可见产出
    line = echo[0][0]
    assert "▸ coaras子智能体 · " in line
    assert TASK in line
    assert "2 项工具" in line
    assert "完成" in line
    assert line.rstrip().endswith("[/#64748b]")  # 只有摘要行带样式上屏


def test_summary_line_printed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """终态事件与结果帧都到达时，摘要行也只打一次。"""
    ctl, echo, _ = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    ctl._on_subagent_terminal(_event("subagent_complete", source="cli-attached", subagent_id=SUBAGENT_ID))
    ctl._on_fold_event(
        _event(
            "user_message",
            source="cli-attached",
            delegate_brief=True,
            parent_tool_call_id=KEY,
            content=BRIEF,
        )
    )
    assert len(echo) == 1


def test_completed_echo_folds_into_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """`[前台子智能体已完成]` 回显能归位时不单打一行，折进块的最终结果组。"""
    ctl, echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    event = SimpleNamespace(
        event_type="continuation_input_injected",
        coara_id="coara-main",
        payload={
            "user_texts": [],
            "user_sources": [],
            "subagent_texts": [f"[{SUBAGENT_ID}] {TASK}\n{RESULT}"],
            "subagent_sources": ["cli"],
        },
    )
    ctl._on_continuation_input_injected(event)
    assert writes == []  # 不再单独回显
    assert echo  # 摘要仍然只有一次
    block = ctl.fold.latest()
    assert block is not None and block.result.strip()


def test_other_end_subagent_never_builds_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """各端显示独立：web 发起的 delegate 不在 CLI 建块。"""
    ctl, echo, writes = _make_controller(monkeypatch)
    ctl._on_fold_event(
        _event(
            "tool_start",
            source="web",
            tool_name="delegate",
            tool_call_id=KEY,
            arguments={"subagent_type": "coaras", "description": TASK},
        )
    )
    assert ctl.fold.available == 0
    assert echo == [] and writes == []


def test_diff_pinned_after_its_tool_row() -> None:
    """diff 贴在产生它的那次调用之后（按工具名回贴），配不上的排末尾不丢。"""
    store = SubagentFoldStore()
    block = store.ensure("k", agent_type="coaras", task="t")
    assert block is not None
    store.add_tool("k", FoldEntry(kind="tool", tool_call_id="c1", label="read(x)", tool_name="read"))
    store.add_tool("k", FoldEntry(kind="tool", tool_call_id="c2", label="edit(y)", tool_name="edit"))
    store.add_diff("k", tool_call_id="", display_blocks=[{"kind": "diff"}], tool_name="edit")
    store.finish("k")

    items = detail_items(block)
    texts = [item.text for item in items if item.text]
    assert texts.index("    ✓ edit(y)") < next(i for i, item in enumerate(items) if item.display_blocks)
    assert "最终结果" not in "\n".join(texts)


def test_aliases_land_in_one_block() -> None:
    """一块一键：call_id / subagent_id / coara_id 任一路标识都归同一块（乱序到达也不裂块）。"""
    store = SubagentFoldStore()
    store.add_diff("call-1", display_blocks=[{"kind": "diff"}], tool_name="edit")
    store.note_start(
        tool_call_id="call-1", subagent_id="sa-coaras-1", coara_id="coara-sub", agent_type="coaras", task="排查"
    )
    store.append_body("call-1", "正在读")
    store.append_body("sa-coaras-1", "文件…")
    store.set_result("coara-sub", "结论", source="frame")
    block = store.finish("sa-coaras-1")
    assert store.available == 1
    assert block is not None
    assert block.key == "call-1"
    assert block.body == "正在读文件…"
    assert block.result == "结论"
    assert block.status == "complete"


def test_diff_without_matching_tool_row_still_kept() -> None:
    """配不上工具行的 diff 排在过程组末尾——不丢，只是位置近似。"""
    store = SubagentFoldStore()
    block = store.ensure("k", agent_type="coaras", task="t")
    assert block is not None
    store.add_tool("k", FoldEntry(kind="tool", tool_call_id="c1", label="read(x)", tool_name="read"))
    store.add_diff("k", display_blocks=[{"kind": "diff"}], tool_name="write")
    items = detail_items(block)
    assert any(item.display_blocks for item in items)


# ── ② Ctrl+O 打出三组 ─────────────────────────────────────────


def test_toggle_and_detail_print_three_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl, _echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)

    # Ctrl+O：进可重绘层，不往滚动区打字、不刷提醒
    writes.clear()
    assert ctl.toggle_latest() is True
    assert writes == []
    assert ctl.fold.latest() is not None and ctl.fold.latest().expanded is True
    overlay = "\n".join(text for _style, text in ctl.expanded_overlay_lines())
    assert "任务指令" in overlay
    assert "过程" in overlay
    assert "最终结果" in overlay
    assert "✓ read(D:/ws/a/src/a.py)" in overlay
    assert BODY in overlay
    assert RESULT in overlay

    # 收起：可重绘层清空
    assert ctl.toggle_latest() is False
    assert writes == []
    assert ctl.expanded_overlay_lines() == []
    assert all(not b.expanded for b in ctl.fold.expanded_blocks())

    # 再展开：仍只走 overlay
    assert ctl.toggle_latest() is True
    assert writes == []
    assert any("最终结果" in text for _s, text in ctl.expanded_overlay_lines())


def test_toggle_collapses_even_when_newer_block_is_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 delegate 抢走 latest 后，Ctrl+O 仍应收起已展开的旧块，而不是再展开新块。"""
    ctl, _echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    assert ctl.toggle_latest() is True

    ctl.fold.note_start(
        tool_call_id="call-delegate-2",
        subagent_id="sa-coaras-next",
        agent_type="coaras",
        task="另一任务",
    )
    writes.clear()
    assert ctl.toggle_latest() is False
    assert writes == []
    assert ctl.expanded_overlay_lines() == []
    assert ctl.fold.latest() is not None
    assert ctl.fold.latest().key == "call-delegate-2"
    assert ctl.fold.latest().expanded is False
    assert all(not b.expanded for b in ctl.fold.expanded_blocks())


def test_final_result_not_duplicated_in_process_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """末轮答复既走 chunk 又走 result 时，展开只在「最终结果」打一遍。"""
    assert strip_result_echo_from_body("前言\n结论全文", "结论全文") == "前言"
    assert strip_result_echo_from_body("结论全文", "结论全文") == ""

    ctl, _echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    block = ctl.fold.latest()
    assert block is not None
    # 模拟末轮：chunk 先把最终答复累进 body，再来 result 同文
    block.body = RESULT
    block.result = ""
    block.result_source = ""
    ctl.fold.set_result(KEY, RESULT, source="frame")
    assert block.body == ""
    assert block.result == RESULT

    writes.clear()
    # 展开走可重绘层：明细不出现在滚动区
    assert ctl.toggle_latest() is True
    joined = "\n".join(text for _style, text in ctl.expanded_overlay_lines())
    assert joined.count(RESULT) == 1
    assert "最终结果" in joined


def test_toggle_without_blocks_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    ctl, _echo, writes = _make_controller(monkeypatch)
    assert ctl.toggle_latest() is False
    assert writes == []
    assert ctl.expanded_overlay_lines() == []


# ── ③ /detail 定位与补全 ───────────────────────────────────────


def test_find_block_by_keyword_type_or_task() -> None:
    """关键词定位：类型 / 任务摘要子串（忽略大小写）；多命中取最近；无命中返回 None。"""
    store = SubagentFoldStore()
    store.note_start(tool_call_id="c1", agent_type="coaras", task="排查持久化与调度竞态")
    store.note_start(tool_call_id="c2", agent_type="aide", task="RESEARCH pricing models")

    assert store.find_block("coaras").key == "c1"  # 类型命中
    assert store.find_block("AIDE").key == "c2"  # 类型命中（忽略大小写）
    assert store.find_block("pricing").key == "c2"  # 任务摘要子串
    assert store.find_block("调度").key == "c1"
    assert store.find_block("子智能体").key == "c2"  # 多命中（标题）→ 最近一块
    assert store.find_block("不存在的词") is None
    assert store.find_block("").key == "c2"  # 无参＝最近一块
    assert store.find_block().key == "c2"


def test_detail_candidates_are_recent_first() -> None:
    store = SubagentFoldStore()
    for index in range(3):
        store.note_start(tool_call_id=f"c{index}", agent_type="coaras", task=f"任务{index}")
    assert [block.key for block in store.detail_candidates()] == ["c2", "c1", "c0"]


def test_detail_command_prints_matched_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/detail <关键词>`：命中那块明细落滚动区；不置 expanded（否则 overlay 双显）。"""
    from src.cli.attached_chat_runner import _is_detail_command, _render_fold_detail

    ctl, _echo, writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)

    assert _is_detail_command("/detail")
    assert _is_detail_command("/detail coaras")
    assert not _is_detail_command("/details")

    _render_fold_detail(ctl, "/detail 排查")
    joined = "\n".join(writes)
    assert "任务指令" in joined and "最终结果" in joined and RESULT in joined
    block = ctl.fold.latest()
    assert block is not None and block.detail_printed is True
    assert block.expanded is False  # overlay 开关不被置位
    assert ctl.expanded_overlay_lines() == []


def test_detail_command_without_match_prints_yellow_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """无命中：滚动区一行黄字提示（说明没匹配，并提示可补全），不打任何明细。"""
    from src.cli.attached_chat_runner import _render_fold_detail

    ctl, _echo, _writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    hints: list[tuple[str, str]] = []
    monkeypatch.setattr(
        CliScrollback,
        "write",
        staticmethod(lambda text="", **kw: hints.append((text, str(kw.get("style") or "")))),
    )

    _render_fold_detail(ctl, "/detail 不相干的关键词")
    assert len(hints) == 1  # 只有一行提示，不含明细
    text, style = hints[0]
    assert style == "yellow"
    assert "不相干的关键词" in text and "Tab" in text


def test_detail_picker_lists_blocks_recent_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/detail` 补全：候选＝各块摘要（最近在前），已输入关键词做子串过滤。"""
    from prompt_toolkit.document import Document

    from src.cli.slash_pickers import SlashOptionPicker, detail_options

    ctl, _echo, _writes = _make_controller(monkeypatch)
    _feed_delegate_run(ctl)
    ctl.fold.note_start(tool_call_id="c2", agent_type="aide", task="调研定价模型")

    def _doc(text: str) -> Document:
        return Document(text, cursor_position=len(text))

    picker = SlashOptionPicker("/detail", lambda: detail_options(ctl))
    assert [c.text for c in picker.get_completions(_doc("/detail"), None)] == [
        "/detail 调研定价模型",
        "/detail 排查持久化与调度竞态",
    ]
    assert [c.text for c in picker.get_completions(_doc("/detail aide"), None)] == ["/detail 调研定价模型"]
    assert [c.text for c in picker.get_completions(_doc("/detail coop"), None)] == []


# ── 摘要行形态与缺值退化 ───────────────────────────────────────


def test_summary_line_degradations() -> None:
    store = SubagentFoldStore()
    block = store.ensure("k")
    assert block is not None
    line = format_summary_line(block)
    assert line.startswith("▸ 子智能体 · （任务未记录） · 运行中 · ")
    store.note_start(tool_call_id="k", agent_type="aide", task="", background=True)
    store.finish("k", failed=True)
    line = format_summary_line(block)
    assert line.startswith("▸ 后台 aide子智能体 · （任务未记录） · 失败 · ")


def test_summary_task_falls_back_to_brief_without_envelope() -> None:
    """缺 description 时任务摘要取指令首行，跳过 `<任务指令>` 信封行。"""
    store = SubagentFoldStore()
    assert store.note_brief("k", "<任务指令>\n\n排查持久化与调度竞态\n- 读 src/a.py\n</任务指令>") is not None
    assert "排查持久化与调度竞态" in store.summary_line(store.latest())
    assert "<任务指令>" not in store.summary_line(store.latest())


def test_summary_notes_evicted_range() -> None:
    """有界缓冲溢出后，摘要行标注可回看范围。"""
    store = SubagentFoldStore(capacity=2)
    for index in range(3):
        block = store.ensure(f"k{index}")
        assert block is not None
        store.finish(f"k{index}")
        store.set_result(f"k{index}", f"结果{index}", source="frame")
    assert store.available == 2
    assert store.evicted
    assert DETAIL_CAPACITY >= 20  # 建议 ≥20 块可回看
    line = store.summary_line(store.latest())
    assert "可回看最近 2 块" in line


# ── 活动树完成块 → 折叠块（spinner 侧的分流）────────────────────


def _trace(event_type: str, **payload):
    from src.core.events import TraceEvent

    return TraceEvent(coara_id="root-1", coara_name="root", event_type=event_type, message=event_type, payload=payload)


def test_subagent_tool_rows_diverge_to_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    """子智能体的工具行不再刷滚动区，交折叠块；主会话/无主的行照旧落滚动区。"""
    from src.cli.spinner import BackgroundSpinner, SubagentSpinnerManager

    landed: list[str] = []
    monkeypatch.setattr("src.cli.streaming.write_tool_history_html", lambda html: landed.append(html))

    folded: list[tuple[str, str]] = []

    class _Sink:
        def ingest_tool_block(self, block, *, owner_key):
            if not owner_key:
                return False
            folded.append((owner_key, block.label))
            return True

    spinner = BackgroundSpinner()
    manager = SubagentSpinnerManager()
    manager.bind_session("sess-main")
    spinner.bind_subagent_spinner(manager)
    spinner.bind_subagent_fold(_Sink())

    # 活动树里 delegate 行要先在（工具的父归属靠它）——真实回合里由同一批事件建立
    manager.handle_event(
        _trace(
            "tool_start",
            tool_call_id=KEY,
            tool_name="delegate",
            coara_id="coara-main",
            arguments={"subagent_type": "coaras", "description": TASK},
        )
    )
    manager.handle_event(
        _trace(
            "subagent_start",
            subagent_id="sa-coaras-1",
            child_coara_id="coara-sub",
            parent_tool_call_id=KEY,
            subagent_type="coaras",
            description=TASK,
        )
    )
    for call_id, path in (("call-read-1", "x"), ("call-read-2", "y")):
        manager.handle_event(
            _trace("tool_start", tool_call_id=call_id, tool_name="read", coara_id="coara-sub", arguments={"path": path})
        )
        manager.handle_event(_trace("tool_complete", tool_call_id=call_id, tool_name="read", coara_id="coara-sub"))

    assert spinner.flush_finished_tool_history() is False  # 折叠不算「落了盘」
    # 折叠块保真：相邻同类工具不归并（归并只是滚动区降噪手段）
    assert folded == [(KEY, "read(x)"), (KEY, "read(y)")]
    assert landed == []

    # 无主（主会话）的完成块仍走滚动区
    plain = BackgroundSpinner()
    plain.bind_subagent_spinner(
        SimpleNamespace(
            flush_finished_blocks=lambda **kwargs: [ToolCallBlock("call-x", "read", "read(x)", finished=True, depth=2)],
            delegate_owner_for_block=lambda block: "",
            subagent_tag_for_block=lambda block: "",
        )
    )
    plain.bind_subagent_fold(_Sink())
    assert plain.flush_finished_tool_history() is True
    assert len(landed) == 1


# ── delegate 自己的 ✓ 行不再上屏 ───────────────────────────────


def test_line_filter_hides_delegate_run_lines() -> None:
    filt = DelegateLineFilter()
    assert filt.feed("✓ delegate coaras: 排查持久化与调度竞态\n") == ""
    assert filt.feed("✓ delegate wait\n") == ""
    assert filt.feed("✓ delegate resume coaras: 继续\n") == ""
    # 不是一次运行的动作保持可见
    assert filt.feed("✓ delegate message sa-x: 提醒\n") == "✓ delegate message sa-x: 提醒\n"
    assert filt.feed("✓ delegate stop sa-x\n") == "✓ delegate stop sa-x\n"


def test_line_filter_passes_main_session_lines_and_streaming_text() -> None:
    filt = DelegateLineFilter()
    assert filt.feed("✓ read(D:/ws/a/src/a.py)\n") == "✓ read(D:/ws/a/src/a.py)\n"
    # 普通流式正文立即放行（pending 行不延迟）
    assert filt.feed("正在分析") == "正在分析"
    assert filt.feed("…\n") == "…\n"
    # 半行看起来像工具行 → 暂扣，成行后再判定
    assert filt.feed("✓ delegate coa") == ""
    assert filt.feed("ras: 任务\n") == ""
    assert filt.flush() == ""


def test_line_filter_flush_releases_plain_tail() -> None:
    filt = DelegateLineFilter()
    assert filt.feed("✓ read(x)") == ""
    assert filt.flush() == "✓ read(x)"


def test_cancelled_terminal_shows_cancelled_not_failed() -> None:
    """被中断的前台 delegate 走 subagent_failed(error=cancelled)：摘要行显示「已取消」。

    回归：内核中断前台 delegate 时发的仍是 subagent_failed，只在 error 里标 cancelled；
    折叠块若一律按 failed 处理，用户自己的中断会被报成故障。
    """
    store = SubagentFoldStore()
    store.note_start(tool_call_id=KEY, agent_type="coaras", task=TASK)
    block = store.finish(store.resolve(SUBAGENT_ID, KEY), failed=True, cancelled=True)
    assert block is not None
    assert block.status == "cancelled"
    line = format_summary_line(block)
    assert "已取消" in line
    assert "失败" not in line


def test_cancelled_terminal_detection() -> None:
    """终态事件判「取消」：只看 subagent_failed 的 error/message，真失败不误判。"""
    from src.cli.display_controller import _is_cancelled_terminal

    def _ev(event_type: str, message: str) -> SimpleNamespace:
        return SimpleNamespace(event_type=event_type, message=message, payload={})

    assert _is_cancelled_terminal(_ev("subagent_failed", "Subagent cancelled: coaras"), {"error": "cancelled"})
    assert _is_cancelled_terminal(_ev("subagent_failed", "Subagent failed: x"), {"error": "用户取消了操作"})
    assert not _is_cancelled_terminal(_ev("subagent_failed", "Subagent failed: x"), {"error": "boom"})
    assert not _is_cancelled_terminal(_ev("subagent_complete", "Subagent completed: x"), {})


def test_cancelled_result_lands_in_result_group() -> None:
    """取消/异常路径补发的终态文案要落进「最终结果」组（不再只剩一行摘要）。"""
    store = SubagentFoldStore()
    store.note_start(tool_call_id=KEY, agent_type="coaras", task=TASK)
    key = store.resolve(KEY)
    store.set_result(key, "（已取消：本次运行被中断，未产出最终报告）", source="frame")
    block = store.finish(key, failed=True, cancelled=True)
    assert block is not None
    items = detail_items(block)
    assert any("最终结果" in item.text for item in items)


def test_block_entries_are_bounded_but_count_stays_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """delegate 可能跑上千次工具：明细有界（只留前 N 条），项数保真并标注省略。"""
    monkeypatch.setattr("src.cli.subagent_fold.MAX_ENTRIES_PER_BLOCK", 3)
    store = SubagentFoldStore()
    store.note_start(tool_call_id=KEY, agent_type="coaras", task=TASK)
    for i in range(10):
        store.add_tool(KEY, FoldEntry(kind="tool", tool_call_id=f"c{i}", label=f"grep(x{i})"))
    block = store.ensure(KEY)
    assert block is not None
    assert len(block.entries) == 3
    assert block.tool_count == 10
    assert any("已省略 7 项" in item.text for item in detail_items(block))


def test_block_diff_bytes_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """diff 帧未经裁剪：单块 diff 体积超上限后不再收，只计数。"""
    monkeypatch.setattr("src.cli.subagent_fold.MAX_DIFF_BYTES_PER_BLOCK", 100)
    store = SubagentFoldStore()
    store.note_start(tool_call_id=KEY, agent_type="coaras", task=TASK)
    payload = [SimpleNamespace(diff_lines=[f"line{i}" for i in range(5)])]  # 粗估 5*120+64 > 100
    assert store.add_diff(KEY, tool_call_id="c1", display_blocks=payload, tool_name="edit") is not None
    block = store.ensure(KEY)
    assert block is not None
    assert block.dropped_diffs == 1
    assert not [entry for entry in block.entries if entry.kind == "diff"]
