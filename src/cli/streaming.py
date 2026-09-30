"""Simplified streaming output for the Coara CLI"""

from __future__ import annotations

from dataclasses import dataclass

from src.cli.scrollback import CliScrollback
from src.coara.display import TOOL_ERROR_INVISIBLE

# 使其块尾空行能充当后续文本块的块首空行，避免叠加成两个空行。
_active_block: StreamingBlock | None = None


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """回合流片段；``is_error`` 供工具行标红（attach 帧字段，不靠隐式字符）。"""

    text: str
    is_error: bool = False


def coerce_stream_piece(chunk: str | StreamChunk) -> tuple[str, bool]:
    """统一 ``process_message`` 产出：正文 str 或带失败位的 StreamChunk。"""
    if isinstance(chunk, StreamChunk):
        return chunk.text, bool(chunk.is_error)
    return str(chunk), False


def notify_block_rendered_after_tool_line() -> None:
    """块外渲染（diff）已输出并自带块尾空行：同步活跃 StreamingBlock 状态。"""
    if _active_block is not None:
        _active_block.mark_external_block_end()


def write_tool_history_html(html: str) -> None:
    """子智能体/后台工具行：有活跃流式块则走间距裁判，否则直写"""
    if _active_block is not None:
        _active_block.write_tool_html(html)
        return
    CliScrollback.write_html(html)


class StreamingBlock:
    """Accumulates streaming text and commits completed lines to terminal. 块分隔模型：
    文本组与工具行组交替时自动空一行（呼吸感）"""

    def mark_external_block_end(self) -> None:
        """块外内容（diff）刚渲染完且尾部已带空行：等价于 write_styled_lines
        的收尾——块尾空行充当后续文本的块首空行（消费一次），后续工具行同组 不再空行"""
        self._started = True
        self._last_was_tool = True
        self._text_break_consumed = True

    def __init__(self, text_style: str = "", tool_style: str = "", error_style: str = "") -> None:
        self._tail = ""
        self._text_style = text_style
        # 工具行（✓/✗）用独立暗色：与 assistant 正文区分，弱化为次要信息
        self._tool_style = tool_style or text_style
        # ✗ 错误行用淡红：能一眼扫出失败，但不刺眼（区别于普通工具行的暗灰）
        self._error_style = error_style or self._tool_style
        # 已写块开始空行：只在确实输出过文本时置 True（纯工具回合无空行）
        self._started = False
        # 上一个提交的块类型（工具行组），用于文本↔工具交替时的分隔空行
        self._last_was_tool = False
        # 当前累积段是否错误行（决定 _flush_lines 用哪个样式）
        self._tail_is_error = False
        # 当前累积段是否工具行（决定 _flush_lines 用哪个样式）
        self._tail_is_tool = False
        # 回显尾行空行已充当后续文本块的块首空行（write_html_lines 置位 消费一次）
        self._text_break_consumed = False

    @property
    def text_style(self) -> str:
        return self._text_style

    # 工具行行首标记：• 为现行；∙/·/✓/✗/× 为旧前缀兼容（重放/历史）
    _TOOL_LINE_MARKS = ("•", "∙", "·", "×", "✓", "✗")

    @staticmethod
    def _is_tool_line(content: str) -> bool:
        stripped = content.lstrip().lstrip(TOOL_ERROR_INVISIBLE)
        return stripped.startswith(StreamingBlock._TOOL_LINE_MARKS)

    @staticmethod
    def _is_error_line(content: str) -> bool:
        """历史失败判据：•+隐式标记 / ×/✗ / ` 报错: `。现行优先用 append(is_error=)。"""
        stripped = content.lstrip()
        if stripped.startswith(f"•{TOOL_ERROR_INVISIBLE}"):
            return True
        if stripped.startswith(("×", "✗")):
            return True
        return " 报错: " in stripped

    @staticmethod
    def _display_text(content: str) -> str:
        """写出前剥掉历史失败隐式标记，屏幕上仍是普通 •。"""
        return content.replace(TOOL_ERROR_INVISIBLE, "")

    def _tail_style(self) -> str:
        if self._tail_is_error:
            return self._error_style
        if self._tail_is_tool:
            return self._tool_style
        return self._text_style

    def append(self, content: str, *, is_error: bool | None = None) -> None:
        if not content:
            return
        # 一块里若正文行与 ✓/✗ 行混在一起，按行切开走交界空行（否则整块被当成
        # 正文，工具行贴在上一句后面、中间没空行）。
        if self._needs_line_split(content):
            head, _, rest = content.partition("\n")
            self._append_piece(head + "\n", is_error=is_error)
            if rest:
                self.append(rest, is_error=is_error)
            return
        self._append_piece(content, is_error=is_error)

    @classmethod
    def _needs_line_split(cls, content: str) -> bool:
        """一块里跨多行且行类型不一致时需要拆开。"""
        if "\n" not in content:
            return False
        if cls._is_tool_line(content):
            # ✓ 开头但后面还有下一行（正文或另一工具）→ 拆
            _first, _sep, rest = content.partition("\n")
            return bool(rest)
        # 正文开头：已闭合行里出现工具行
        complete, _sep, _tail = content.rpartition("\n")
        return any(line.strip() and cls._is_tool_line(line) for line in complete.split("\n"))

    def _append_piece(self, content: str, *, is_error: bool | None = None) -> None:
        if not content:
            return
        if self._is_tool_line(content):
            # 工具行：先强制提交残余正文（含无换行半行），再写工具组前空行。
            # 只用 _flush_lines 会把半行正文留在 _tail，再与 ✓ 拼成同一行提交。
            if self._tail:
                self._commit_tail()
            if not self._last_was_tool:
                CliScrollback.write("")
                self._started = True
            self._last_was_tool = True
            self._tail_is_tool = True
            self._tail_is_error = bool(is_error) if is_error is not None else self._is_error_line(content)
            # 回显尾行空行已充当本工具行上方空行 标记消费（防残留到后续文本）
            self._text_break_consumed = False
            # 无换行的工具行会一直扣在 _tail，下一条再来就拼成一段——强制成行
            if not content.endswith("\n"):
                content = f"{content}\n"
        else:
            if self._last_was_tool:
                if not self._text_break_consumed:
                    CliScrollback.write("")
                    # 该分隔空行已充当文本块开始空行（工具行前置无文本时）
                    # 置 _started 避免与下方块首空行叠加成两个空行
                    self._started = True
                self._text_break_consumed = False
                self._last_was_tool = False
            if not self._started and content.strip():
                # 文本块开始空行（与上方内容分隔）
                CliScrollback.write("")
                self._started = True
            self._tail_is_tool = False
            self._tail_is_error = False
        self._tail += content
        if "\n" in content:
            self._flush_lines()

    def _flush_lines(self) -> None:
        last_nl = self._tail.rfind("\n")
        if last_nl == -1:
            return
        to_commit = self._tail[: last_nl + 1]
        self._tail = self._tail[last_nl + 1 :]
        # 末尾未闭合的 GFM 表暂扣，等整表或回合结束再写（对齐依赖整块）
        if not self._tail_is_tool and "|" in to_commit:
            from src.cli.md_links import split_commit_holding_open_table

            ready, hold = split_commit_holding_open_table(to_commit)
            if hold:
                self._tail = hold + self._tail
            to_commit = ready
        if to_commit:
            CliScrollback.write(self._display_text(to_commit), style=self._tail_style(), end="")

    def _commit_tail(self) -> None:
        """提交残余文本（不写块尾空行）。回合结束：含暂扣的完整表，不再 hold。"""
        if self._tail:
            CliScrollback.write(self._display_text(self._tail), style=self._tail_style())
            self._tail = ""

    def write_styled_lines(self, lines: list[str]) -> None:
        """带样式行（接续输入/子智能体结果回显）：与流式块同一套间距语义"""
        self._commit_tail()
        if (self._started or self._last_was_tool) and not self._text_break_consumed:
            CliScrollback.write("")
        for line in lines:
            CliScrollback.write_markup(line)
        CliScrollback.write("")
        self._started = True
        self._last_was_tool = True
        self._text_break_consumed = True

    def write_tool_html(self, html: str) -> None:
        """通道 C 工具行（HTML 染色）：与 ``append(✓/✗)`` 同一套组间空行语义"""
        self._commit_tail()
        if not self._last_was_tool:
            CliScrollback.write("")
            self._started = True
        CliScrollback.write_html(html)
        self._last_was_tool = True
        self._text_break_consumed = False

    def flush(self) -> None:
        """Commit any remaining text (called when streaming ends)."""
        self._commit_tail()
        # 回合最终输出底部不留多余空行（块间分隔由 append 的块交界逻辑负责）
        self._started = False
        self._last_was_tool = False

    @property
    def pending_line(self) -> str:
        """The current incomplete line (after the last newline)."""
        return self._tail.rstrip("\n")
