"""Unified CLI scrollback output (Channel A + C)"""

from __future__ import annotations

import contextlib
import html as _html
from collections.abc import Iterator
from typing import Any

from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.output import Output
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console
from rich.text import Text

_scrollback_console = Console(legacy_windows=False)

# 供回合收尾在官方 in_terminal 块内使用（渲染已挂起）：一次 erase → 全部内容
_direct_output: Output | None = None


class _OutputFileAdapter:
    """把 prompt_toolkit Output 适配成 Rich 可用的文本 file-like"""

    __slots__ = ("_output",)

    def __init__(self, output: Output) -> None:
        self._output = output

    def write(self, data: str) -> int:
        # Rich 输出含 ANSI；ptk Output.write 会剥/转义控制序列（ESC→?），必须 write_raw。
        if data:
            self._output.write_raw(data)
        return len(data) if data else 0

    def flush(self) -> None:
        self._output.flush()

    @property
    def encoding(self) -> str:
        enc = getattr(self._output, "encoding", None)
        if callable(enc):
            with contextlib.suppress(Exception):
                enc = enc()
        return str(enc or "utf-8")

    def isatty(self) -> bool:
        return True


@contextlib.contextmanager
def direct_output(output: Output) -> Iterator[None]:
    """临时把所有 scrollback 通道重定向到同一 Output（同步直写）"""
    global _direct_output
    _direct_output = output
    prev_file = _scrollback_console.file
    _scrollback_console.file = _OutputFileAdapter(output)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            output.flush()
        _scrollback_console.file = prev_file
        _direct_output = None


class CliScrollback:
    """Single sink for lines that leave the dynamic prompt area."""

    @staticmethod
    def write(text: str, *, style: str = "", end: str = "\n") -> None:
        from src.cli.md_links import try_rich_basic_markdown

        # 无基础 Markdown 标记时立刻 None；有则轻量渲染（加粗/代码/链接/标题/列表/无竖线表格）
        # 空串/纯空白不进渲染器（回合起止的分隔空行）
        if not text.strip():
            if style:
                _scrollback_console.print(Text(text, style=style), end=end)
            else:
                _scrollback_console.print(text, end=end)
            return
        rendered = try_rich_basic_markdown(text, style=style)
        if rendered is not None:
            _scrollback_console.print(rendered, end=end)
            return
        if style:
            _scrollback_console.print(Text(text, style=style), end=end)
        else:
            _scrollback_console.print(text, end=end)

    @staticmethod
    def write_html(fragment: str) -> None:
        """Print an HTML fragment via prompt_toolkit."""
        if _direct_output is not None:
            print_formatted_text(HTML(fragment), output=_direct_output)
            return
        print_formatted_text(HTML(fragment))

    @staticmethod
    def write_markup(fragment: str) -> None:
        """Print a Rich-markup fragment（与流式块同通道 顺序严格 FIFO）。

        动态内容先经 ``rich.markup.escape`` 转义方括号。
        """
        from rich.text import Text as _Text

        _scrollback_console.print(_Text.from_markup(fragment))

    @staticmethod
    def write_renderable(renderable: Any) -> None:
        """Print a Rich renderable（工具 diff 面板等块外内容）。"""
        _scrollback_console.print(renderable)

    @staticmethod
    def esc(text: str) -> str:
        """Escape dynamic text so it is safe to embed inside an HTML fragment"""
        return _html.escape(str(text), quote=False)
