"""Attach CLI turn-end spinner / streaming teardown (flicker guards)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.cli.display_controller import CliDisplayController
from src.cli.spinner import BackgroundSpinner


def test_stop_streaming_soft_blanks_thinking_without_invalidate(monkeypatch):
    """等高退场：就地擦 Thinking 行，不 app.invalidate（避免整页闪）。"""
    spinner = BackgroundSpinner()
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(idle_prompt_line=lambda: "", count=0),
    )

    out = MagicMock()
    out.get_size.return_value = SimpleNamespace(columns=80)
    app = SimpleNamespace(is_running=True, output=out, invalidate=MagicMock())
    session = SimpleNamespace(app=app)
    spinner._session = session  # type: ignore[assignment]

    spinner.start_streaming()
    spinner.append_streaming_chunk("done\n")
    redraw.reset_mock()
    app.invalidate.reset_mock()

    spinner.stop_streaming()

    assert spinner._cli_turn_display_active is False
    out.cursor_up.assert_called_once_with(2)
    out.erase_end_of_line.assert_called_once()
    out.cursor_down.assert_called_once_with(2)
    redraw.assert_not_called()
    app.invalidate.assert_not_called()


def test_stop_streaming_falls_back_to_redraw_when_soft_clear_fails(monkeypatch):
    spinner = BackgroundSpinner()
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_soft_blank_thinking_line",
        lambda self, up: False,
    )
    spinner.start_streaming()
    spinner.append_streaming_chunk("done\n")
    redraw.reset_mock()
    spinner.stop_streaming()
    redraw.assert_called_once_with(force=True)


def test_stop_streaming_pending_flush_suppresses_patch_stdout_invalidate(monkeypatch):
    """未完成尾行 flush 时压制 patch_stdout 的 invalidate；落盘重绘已覆盖终帧。"""
    spinner = BackgroundSpinner()
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)

    invalidate = MagicMock()
    app = SimpleNamespace(is_running=True, output=MagicMock(), invalidate=invalidate)
    spinner._session = SimpleNamespace(app=app)  # type: ignore[assignment]

    spinner.start_streaming()
    spinner.append_streaming_chunk("no newline")
    redraw.reset_mock()
    invalidate.reset_mock()

    real_flush = spinner._streaming_block.flush if spinner._streaming_block else None

    def _flush_that_invalidates() -> None:
        if real_flush is not None:
            real_flush()
        app.invalidate()

    assert spinner._streaming_block is not None
    spinner._streaming_block.flush = _flush_that_invalidates  # type: ignore[method-assign]

    spinner.stop_streaming()
    # 收尾 flush 期间的 invalidate 被吞；tail 有实际写屏 → run_in_terminal 重绘
    # 已画空闲态，不再补 force（「Thinking 闪现重画」的收尾闪屏由此消除）
    assert invalidate.call_count == 0
    redraw.assert_not_called()


def test_reset_after_runtime_change_restores_title(monkeypatch):
    spinner = BackgroundSpinner()
    restore = MagicMock()
    spinner._restore_terminal_title = restore  # type: ignore[method-assign]
    spinner.request_redraw = MagicMock()  # type: ignore[method-assign]
    spinner._cli_turn_display_active = True
    spinner.reset_after_runtime_change()
    assert spinner._cli_turn_display_active is False
    restore.assert_called_once()


def test_turn_end_quiet_swallows_followup_redraw():
    spinner = BackgroundSpinner()
    spinner._session = None
    spinner._cli_turn_display_active = True
    spinner.stop_streaming()
    assert spinner._turn_end_quiet_until > time.monotonic()
    spinner._dirty = False
    spinner.request_redraw()
    assert spinner._dirty is False


def test_turn_end_quiet_allows_redraw_after_user_types():
    spinner = BackgroundSpinner()
    spinner._session = None
    spinner._begin_turn_end_quiet()
    spinner._last_buffer_activity = spinner._quiet_started_at + 0.01
    spinner._dirty = False
    spinner.request_redraw()
    assert spinner._dirty is True


def test_trace_turn_end_does_not_hide_pending_before_stop_streaming(monkeypatch):
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: False,
        foreground_coara=SimpleNamespace(_active_turn_source="cli-attached"),
    )
    spinner.bind_root(root)
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    spinner.start_streaming()
    spinner.append_streaming_chunk("still flushing")
    assert spinner._should_show_streaming_pending() is True
    assert spinner._foreground_cli_spinner_active() is True


def test_stop_streaming_clears_display_flag_without_block():
    spinner = BackgroundSpinner()
    spinner._cli_turn_display_active = True
    spinner._streaming_block = None
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]
    # 无 session → soft 失败 → force redraw
    spinner.stop_streaming()
    assert spinner._cli_turn_display_active is False
    redraw.assert_called_once_with(force=True)


def test_local_begin_turn_spins_before_kernel_source(monkeypatch):
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: False,
        foreground_coara=SimpleNamespace(_active_turn_source=""),
    )
    spinner.bind_root(root)
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    assert spinner._foreground_cli_spinner_active() is False
    spinner.start_streaming()
    assert spinner._foreground_cli_spinner_active() is True


def test_pending_hidden_after_stop_streaming(monkeypatch):
    spinner = BackgroundSpinner()
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    spinner.start_streaming()
    spinner.append_streaming_chunk("tail")
    spinner.stop_streaming()
    assert spinner._should_show_streaming_pending() is False


@pytest.mark.asyncio
async def test_finish_turn_async_ordered_atomic_exit(monkeypatch):
    """收尾链：先撤 UI 态（clear → retire），内容经 run_in_terminal_flush 落盘，终帧 settle。"""
    spinner = BackgroundSpinner()
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]
    order: list[str] = []
    spinner.retire_turn_display = MagicMock(return_value=False, side_effect=lambda: order.append("retire"))  # type: ignore[method-assign]
    spinner.flush_streaming_tail = MagicMock(return_value=False, side_effect=lambda: order.append("tail"))  # type: ignore[method-assign]
    spinner.finish_turn_settle = MagicMock(side_effect=lambda **_: order.append("settle"))  # type: ignore[method-assign]
    spinner.clear_turn_timer = MagicMock(side_effect=lambda: order.append("timer"))  # type: ignore[method-assign]
    spinner.flush_finished_tool_history = MagicMock(return_value=False, side_effect=lambda: order.append("history"))  # type: ignore[method-assign]
    spinner.stop_streaming = MagicMock()  # type: ignore[method-assign]

    async def _flush(write) -> bool:
        order.append("flush_begin")
        result = bool(write())
        order.append("flush_end")
        return result

    spinner.run_in_terminal_flush = _flush  # type: ignore[method-assign]

    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(session_id="sess-1"),
        has_active_turn=lambda: False,
    )
    subagent = SimpleNamespace(
        clear_root_status=MagicMock(side_effect=lambda: order.append("clear")),
        bind_session=MagicMock(),
        handle_event=MagicMock(),
    )
    display = CliDisplayController(
        root=root,
        console=MagicMock(),
        background_spinner=spinner,
        subagent_spinner=subagent,
    )

    def _diffs(self) -> bool:
        order.append("diffs")
        return True

    monkeypatch.setattr(CliDisplayController, "flush_pending_fg_diffs", _diffs)
    await display.finish_turn_async()
    assert order == [
        "clear",
        "retire",
        "flush_begin",
        "diffs",
        "history",
        "tail",
        "flush_end",
        "settle",
        "timer",
    ]
    spinner.stop_streaming.assert_not_called()
    subagent.clear_root_status.assert_called_once()


@pytest.mark.asyncio
async def test_run_in_terminal_flush_uses_proxy_not_direct_app_output(monkeypatch):
    """收尾写屏走 write 回调本身（StdoutProxy），不再套 in_terminal+direct_output。"""
    spinner = BackgroundSpinner()
    calls: list[str] = []

    async def _drain(app, **kwargs):
        calls.append("drain")

    spinner._drain_patch_stdout = _drain  # type: ignore[method-assign]
    app = SimpleNamespace(_is_running=True, output=MagicMock())
    spinner._session = SimpleNamespace(app=app)

    # 若误走 direct_output，会碰 app.output
    def _write() -> bool:
        calls.append("write")
        return True

    flushed = await spinner.run_in_terminal_flush(_write)
    assert flushed is True
    assert calls == ["drain", "write", "drain"]
    app.output.write_raw.assert_not_called()
    app.output.write.assert_not_called()


@pytest.mark.asyncio
async def test_run_in_terminal_flush_without_app_still_writes():
    """无活动 UI：直接执行 write，不依赖 app.output。"""
    spinner = BackgroundSpinner()
    spinner._session = None
    calls: list[str] = []
    flushed = await spinner.run_in_terminal_flush(lambda: (calls.append("write"), True)[1])
    assert flushed is True
    assert calls == ["write"]


def test_finish_turn_settle_force_only_when_retire_needed_and_no_flush():
    """终帧 force 仅在「retire 未就地清理且无落盘重绘」时补：有写屏则不补帧。"""
    spinner = BackgroundSpinner()
    redraw = MagicMock()
    spinner.request_redraw = redraw  # type: ignore[method-assign]

    spinner.finish_turn_settle(force=False)
    redraw.assert_not_called()
    spinner.finish_turn_settle(force=True)
    redraw.assert_called_once_with(force=True)


def test_flush_streaming_tail_reports_actual_screen_write(monkeypatch):
    """tail 为空时 flush 无写屏 → 返回 False（让 settle 补 force）；有 tail → True。"""
    spinner = BackgroundSpinner()
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", lambda *a, **k: None)
    assert spinner.flush_streaming_tail() is False

    spinner.start_streaming()
    spinner.append_streaming_chunk("done\n")
    spinner.retire_turn_display()  # 无 session 时 soft 失败也无所谓，关心 tail 是否空
    assert spinner.flush_streaming_tail() is False

    spinner.start_streaming()
    spinner.append_streaming_chunk("tail")
    spinner.retire_turn_display()
    assert spinner.flush_streaming_tail() is True


def test_direct_output_rich_print_reaches_ptk_output():
    """回归：direct_output 必须让 Rich 正文写到 Output.write_raw（write 会剥 ANSI）。"""
    from src.cli.scrollback import CliScrollback, direct_output

    class _Rec:
        def __init__(self) -> None:
            self.buf: list[str] = []
            self.raw: list[str] = []

        def write(self, data: str) -> None:
            self.buf.append(data)

        def write_raw(self, data: str) -> None:
            self.raw.append(data)

        def flush(self) -> None:
            return None

        def encoding(self) -> str:
            return "utf-8"

    out = _Rec()
    with direct_output(out):  # type: ignore[arg-type]
        CliScrollback.write("hello-cli-tail")
    assert "hello-cli-tail" in "".join(out.raw)
    assert out.buf == []


def test_output_file_adapter_uses_write_raw_for_ansi():
    """ptk Output.write 会剥 ESC；adapter 必须走 write_raw 保留颜色序列。"""
    from src.cli.scrollback import _OutputFileAdapter

    class _Rec:
        def __init__(self) -> None:
            self.cooked: list[str] = []
            self.raw: list[str] = []

        def write(self, data: str) -> None:
            self.cooked.append(data)

        def write_raw(self, data: str) -> None:
            self.raw.append(data)

        def flush(self) -> None:
            return None

        def encoding(self) -> str:
            return "utf-8"

    out = _Rec()
    adapter = _OutputFileAdapter(out)  # type: ignore[arg-type]
    seq = "\x1b[38;2;195;205;219m你好\x1b[0m"
    adapter.write(seq)
    assert "".join(out.raw) == seq
    assert out.cooked == []


def test_direct_output_encoding_is_string_for_rich():
    from prompt_toolkit.output import DummyOutput

    from src.cli.scrollback import _OutputFileAdapter

    adapter = _OutputFileAdapter(DummyOutput())
    assert isinstance(adapter.encoding, str)
    assert adapter.encoding.lower() == adapter.encoding
