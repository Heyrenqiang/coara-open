"""Attach 端完整 CLI 样子——协议仍是 /ws/attach，显示栈对齐主 CLI。

复用 StreamingBlock / CliScrollback / 主题令牌 / prompt_toolkit 底栏与
「Thinking… + ╌ input ╌ + 你：」布局；不持 Root、不订 event_bus。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.spinner import SPINNERS

from src.cli.scrollback import CliScrollback
from src.cli.streaming import StreamingBlock
from src.cli.terminal_width import fit_three_column
from src.cli.theme import fg as _fg
from src.cli.theme import get_assistant_text_style, get_prompt_style, get_tool_error_style, get_tool_text_style

# attach 端支持的斜杠命令（与服务端 _ATTACH_COMMAND_WHITELIST 对齐）
_ATTACH_COMMANDS: dict[str, str] = {
    "/model": "列出或切换 LLM 模型",
    "/new": "开始新会话",
    "/stop": "中断当前回合",
    "/compact": "手动压缩当前会话历史",
    "/detail": "回看某个子智能体折叠块明细（/detail [关键词]，带补全）",
    "/report": "向开发者提交问题报告",
    "/help": "显示可用命令",
}


def _esc(text: str) -> str:
    import html

    return html.escape(str(text), quote=False)


def _apply_input_tweaks(session: PromptSession[str]) -> None:
    """对齐主 CLI 的输入体验修正（Windows IME / 快速输入）。

    Windows 无真正的 bracketed-paste：prompt_toolkit 会把成串到达的字符误判为
    粘贴，IME 上屏/快速输入常被当作 paste 走另一条渲染路径，表现为「输入无文字」。
    关掉 paste 识别 + 限速重绘（对齐主 CLI apply_prompt_session_input_tweaks）。
    """
    import sys

    app = session.app
    if getattr(app, "min_redraw_interval", None) in (None, 0):
        app.min_redraw_interval = 0.05
    if sys.platform != "win32":
        return
    reader = getattr(app.input, "console_input_reader", None)
    if reader is not None and hasattr(reader, "recognize_paste"):
        reader.recognize_paste = False


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f" {total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f" {minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f" {hours}h{minutes:02d}m"


class AttachSlashCompleter(Completer):
    """attach 端斜杠命令补全（仅支持的 6 个命令）。"""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if document.text_after_cursor.strip():
            return
        if not text.strip() or not text.startswith("/"):
            return
        text_l = text.lower()
        for cmd, meta in _ATTACH_COMMANDS.items():
            if cmd.startswith(text_l):
                yield Completion(cmd, start_position=-len(text), display=cmd, display_meta=meta)


class AttachUI:
    """完整 CLI 样子的 attach 对话面：滚动区 + Thinking 行 + 底栏。"""

    _DEFAULT_TERMINAL_TITLE = "coara 考拉"

    def __init__(self, workspace: str, provider: str, model: str) -> None:
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.turn_active = False
        self.history = InMemoryHistory()
        self._streaming_block: StreamingBlock | None = None
        self._spinner_frames = SPINNERS["dots"]["frames"]
        self._spinner_interval = 0.08
        self._turn_started_at: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    def _write_terminal_title(self, title: str) -> None:
        app = get_app_or_none()
        if app is None:
            return
        try:
            safe = title.replace("\x1b", "").replace("\x07", "").replace("\n", " ")[:120]
            app.output.write_raw(f"\x1b]0;{safe}\x07")
            app.output.flush()
        except Exception:
            pass

    def _refresh_title_spinner(self) -> None:
        if not self.turn_active:
            return
        started = self._turn_started_at or time.monotonic()
        elapsed = _format_elapsed(time.monotonic() - started).strip()
        frame = self._get_frame()
        title = f"{frame} Thinking…"
        if elapsed:
            title = f"{title} {elapsed}"
        self._write_terminal_title(title)

    def _restore_terminal_title(self) -> None:
        self._write_terminal_title(self._DEFAULT_TERMINAL_TITLE)

    # ── 回合 / 流式 ─────────────────────────────────────────
    def begin_turn(self) -> None:
        import src.cli.streaming as streaming_mod

        self.turn_active = True
        self._turn_started_at = time.monotonic()
        self._streaming_block = StreamingBlock(
            text_style=get_assistant_text_style(),
            tool_style=get_tool_text_style(),
            error_style=get_tool_error_style(),
        )
        streaming_mod._active_block = self._streaming_block
        self._refresh_title_spinner()
        self._invalidate()

    def end_turn(self) -> None:
        import src.cli.streaming as streaming_mod

        block = self._streaming_block
        pending = bool(block is not None and block.pending_line)
        self._streaming_block = None
        if streaming_mod._active_block is block:
            streaming_mod._active_block = None
        self.turn_active = False
        self._turn_started_at = None
        self._restore_terminal_title()
        if block is not None:
            block.flush()
        if not pending:
            self._invalidate()

    def print_chunk(self, text: str) -> None:
        if self._streaming_block is None:
            self.begin_turn()
        assert self._streaming_block is not None
        self._streaming_block.append(text)
        self._invalidate()

    def print_tool(self, text: str, ok: bool) -> None:
        # 服务端已拆 tool 帧；仍走 StreamingBlock 组间距（与主 CLI 一致）
        del ok  # 样式由 ✓/✗ 前缀决定
        line = text if text.endswith("\n") else text + "\n"
        self.print_chunk(line)

    def print_error(self, message: str, *, occupied: bool = False) -> None:
        self.end_turn()
        label = "占用" if occupied else "错误"
        CliScrollback.write_html(
            f'<style fg="{_fg("status.error")}">[{label}] {_esc(message)}</style>'
        )

    def print_command_result(self, output: str) -> None:
        self.end_turn()
        if output.strip():
            CliScrollback.write(output.rstrip("\n"), style=get_assistant_text_style())

    # ── prompt chrome（对齐主 CLI spinner.__call__）──────────
    def _get_frame(self) -> str:
        idx = int(time.monotonic() / self._spinner_interval) % len(self._spinner_frames)
        return self._spinner_frames[idx]

    def _prompt_message(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80
        fragments = FormattedText()

        if self._streaming_block is not None:
            pending = self._streaming_block.pending_line
            if pending:
                from src.cli.terminal_width import tail_to_width

                # 钉死一行：满宽折行使输入区行高在 chunk 边界 1↔2 跳变
                pending = tail_to_width(pending, max(10, columns - 1))
                fragments.append((get_assistant_text_style(), pending + "\n"))

        if self.turn_active:
            started = self._turn_started_at or time.monotonic()
            elapsed = _format_elapsed(time.monotonic() - started)
            frame = self._get_frame()
            fragments.append(("class:prompt.thinking", f"{frame} Thinking…{elapsed}\n"))
        else:
            # Thinking 槽空闲空白占位，避免回合结束跳变
            fragments.append(("", "\n"))

        from src.cli.terminal_width import format_input_separator

        input_border = format_input_separator(columns)
        fragments.append(("class:running-prompt-separator", input_border))
        fragments.append(("", "\n"))
        fragments.append((_fg("text.user"), "你："))
        return fragments

    def bottom_toolbar(self) -> FormattedText:
        """底栏：provider·model | workspace（attach 无 root 用量，不画 cache）。"""
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80

        left_parts: list[tuple[str, str]] = []
        provider = self.provider or "?"
        model = self.model or "?"
        left_parts.append(("class:toolbar.workdir", f" {provider}"))
        left_parts.append(("class:toolbar.separator", "·"))
        left_parts.append(("class:toolbar.workdir", f"{model} "))
        left_text = "".join(text for _, text in left_parts)
        mid_text = f" {self.workspace} " if self.workspace else ""
        right_text = " 工作中 " if self.turn_active else ""

        left_text, mid_text, right_text, left_pad, right_pad = fit_three_column(
            columns=columns,
            left=left_text,
            mid=mid_text,
            right=right_text,
        )

        parts: list[tuple[str, str]] = [
            ("class:toolbar.separator", "─" * columns),
            ("", "\n"),
        ]
        parts.extend(left_parts)
        if left_pad:
            parts.append(("class:toolbar.separator", " " * left_pad))
        if mid_text:
            parts.append(("class:toolbar.workdir", mid_text))
        if right_pad:
            parts.append(("class:toolbar.separator", " " * right_pad))
        if right_text:
            parts.append(("class:toolbar.workdir", right_text))
        return FormattedText(parts)

    def _invalidate(self) -> None:
        app = get_app_or_none()
        if app is not None:
            app.invalidate()

    async def _refresh_loop(self) -> None:
        """回合进行中刷新 Thinking 帧（与主 CLI spinner 同频）。"""
        try:
            while True:
                await asyncio.sleep(self._spinner_interval)
                if self.turn_active:
                    self._refresh_title_spinner()
                    self._invalidate()
        except asyncio.CancelledError:
            return

    # ── 输入 ───────────────────────────────────────────────
    def build_session(self) -> PromptSession[str]:
        session = PromptSession(
            history=self.history,
            completer=AttachSlashCompleter(),
            bottom_toolbar=self.bottom_toolbar,
            style=get_prompt_style(),
            message=self._prompt_message,
        )
        _apply_input_tweaks(session)
        return session

    async def prompt(self, session: PromptSession[str]) -> str | None:
        """读一行输入；EOF/Ctrl+C 返回 None。patch_stdout 让流式不糊输入框。"""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        with patch_stdout(raw=True):
            try:
                return (await session.prompt_async()).strip()
            except (EOFError, KeyboardInterrupt):
                return None

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        self.end_turn()


def make_attach_ui(attached_frame: dict[str, Any]) -> AttachUI:
    """从服务端 attached 帧构建 UI。"""
    return AttachUI(
        workspace=str(attached_frame.get("workspace") or "?"),
        provider=str(attached_frame.get("provider") or ""),
        model=str(attached_frame.get("model") or ""),
    )
