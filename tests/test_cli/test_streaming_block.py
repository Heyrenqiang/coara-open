"""StreamingBlock behavior tests — lock the commit semantics after the
O(n) tail-based rewrite (was O(total) per chunk), plus the blank-line
breathing around text blocks."""

from __future__ import annotations

from src.cli.streaming import StreamingBlock


class _Recorder:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    def write(self, text: str, *, style: str = "", end: str = "\n") -> None:
        self.writes.append((text, style, end))


def _block_with_recorder(monkeypatch, style: str = "assistant"):
    rec = _Recorder()
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write", rec.write)
    return StreamingBlock(text_style=style), rec


def _text_writes(rec: _Recorder) -> list[tuple[str, str, str]]:
    """过滤掉空行（呼吸空行），只留文本提交。"""
    return [w for w in rec.writes if w[0] != ""]


def test_line_based_commit(monkeypatch):
    block, rec = _block_with_recorder(monkeypatch)
    block.append("Hello")
    # 首个非空文本触发块开始空行；无换行则文本仍 pending
    assert rec.writes == [("", "", "\n")]
    assert block.pending_line == "Hello"

    block.append(", world.\nSecond line")
    # 一行完整提交；remainder 保持 pending
    assert rec.writes == [("", "", "\n"), ("Hello, world.\n", "assistant", "")]
    assert block.pending_line == "Second line"

    block.append(" here.\n")
    assert rec.writes[-1] == ("Second line here.\n", "assistant", "")
    assert block.pending_line == ""


def test_flush_commits_remainder_with_default_end(monkeypatch):
    block, rec = _block_with_recorder(monkeypatch)
    block.append("no newline yet")
    block.flush()
    # 开始空行 + 文本（不再写结束空行）
    assert rec.writes == [("", "", "\n"), ("no newline yet", "assistant", "\n")]
    assert block.pending_line == ""


def test_flush_after_full_commit_is_noop_text(monkeypatch):
    block, rec = _block_with_recorder(monkeypatch)
    block.append("done.\n")
    block.flush()
    # 开始空行 + 文本（以 \n 提交，不再写结束空行）
    assert rec.writes == [("", "", "\n"), ("done.\n", "assistant", "")]


def _patch_markup(monkeypatch) -> list[str]:
    markup_writes: list[str] = []
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write_markup", lambda f: markup_writes.append(f))
    return markup_writes


def test_echo_lines_spacing_between_tool_groups(monkeypatch):
    """回显（接续输入）：工具组 → 空行 回显 空行 → 后续工具行同组不再补空行。"""
    block, rec = _block_with_recorder(monkeypatch)
    markup = _patch_markup(monkeypatch)

    block.append("✓ shell(a)\n")
    block.write_styled_lines(["[cyan]你：[/cyan] 继续"])
    block.append("✓ shell(b)\n")

    # 块首工具行前空一行（与上方用户输入分隔）→ 工具组 → 回显 → 后续工具同组
    assert [w[0] for w in rec.writes] == ["", "✓ shell(a)\n", "", "", "✓ shell(b)\n"]
    assert markup == ["[cyan]你：[/cyan] 继续"]


def test_echo_then_text_no_double_blank(monkeypatch):
    """回显尾行空行已充当后续文本块的块首空行 不重复。"""
    block, rec = _block_with_recorder(monkeypatch)
    _patch_markup(monkeypatch)

    block.append("✓ shell(a)\n")
    block.write_styled_lines(["[cyan]你：[/cyan] 继续"])
    block.append("好的 我来处理\n")

    assert [w[0] for w in rec.writes] == ["", "✓ shell(a)\n", "", "", "好的 我来处理\n"]


def test_echo_tool_then_text_keeps_single_blank(monkeypatch):
    """回显 → 工具行 → 文本：工具行消费标记后 文本与工具行之间仍恰好一空行。"""
    block, rec = _block_with_recorder(monkeypatch)
    _patch_markup(monkeypatch)

    block.append("✓ shell(a)\n")
    block.write_styled_lines(["[cyan]你：[/cyan] 继续"])
    block.append("✓ shell(b)\n")
    block.append("处理结果如下\n")

    assert [w[0] for w in rec.writes] == ["", "✓ shell(a)\n", "", "", "✓ shell(b)\n", "", "处理结果如下\n"]


def test_tool_first_then_text_single_blank(monkeypatch):
    """以工具行开头（前置无文本）→ 文本：工具组与文本之间恰好一空行 不叠加。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("✓ web_fetch(u1)\n")
    block.append("✓ web_fetch(u2)\n")
    block.append("记起来了。\n")
    block.flush()

    # 块首工具行前空一行；连续工具行紧贴无空行；工具→文本恰好一个空行；底部无结束空行
    assert [w[0] for w in rec.writes] == [
        "",
        "✓ web_fetch(u1)\n",
        "✓ web_fetch(u2)\n",
        "",
        "记起来了。\n",
    ]


def test_consecutive_echoes_share_single_blank(monkeypatch):
    """连续两条回显之间恰好一个空行 不叠加。"""
    block, rec = _block_with_recorder(monkeypatch)
    markup = _patch_markup(monkeypatch)

    block.append("✓ shell(a)\n")
    block.write_styled_lines(["[cyan]你：[/cyan] 第一条"])
    block.write_styled_lines(["[cyan]你：[/cyan] 第二条"])

    assert [w[0] for w in rec.writes] == ["", "✓ shell(a)\n", "", "", ""]
    assert markup == ["[cyan]你：[/cyan] 第一条", "[cyan]你：[/cyan] 第二条"]


def test_multi_newline_chunk(monkeypatch):
    block, rec = _block_with_recorder(monkeypatch)
    block.append("a\nb\nc")
    assert rec.writes == [("", "", "\n"), ("a\nb\n", "assistant", "")]
    assert block.pending_line == "c"


def test_empty_append_is_noop(monkeypatch):
    block, rec = _block_with_recorder(monkeypatch)
    block.append("")
    assert rec.writes == []
    assert block.pending_line == ""


def test_no_blank_lines_without_text(monkeypatch):
    """纯工具回合（无文本）：不产生任何空行。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.flush()
    assert rec.writes == []
    block.append("")
    block.flush()
    assert rec.writes == []


def test_blank_line_only_after_real_text(monkeypatch):
    """空白 chunk（如纯换行）不触发块开始空行。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("   \n")
    # 内容本身提交，但不产生开始空行
    assert rec.writes == [("   \n", "assistant", "")]
    block.append("real\n")
    # 首个非空文本触发开始空行（排在已提交内容之后）
    assert ("", "", "\n") in rec.writes[:2]
    block.flush()
    # 底部无结束空行，最后一条是已提交文本
    assert rec.writes[-1] == ("real\n", "assistant", "")


def test_long_turn_stays_correct(monkeypatch):
    """Many small chunks: committed output must equal the concatenated input."""
    block, rec = _block_with_recorder(monkeypatch)
    pieces: list[str] = []
    for i in range(500):
        piece = f"line {i} of a very long answer.\n"
        pieces.append(piece)
        block.append(piece)
    block.append("tail without newline")
    pieces.append("tail without newline")
    block.flush()
    committed = "".join(w[0] for w in _text_writes(rec))
    assert committed == "".join(pieces)


def test_text_tool_alternation_gets_blank_lines(monkeypatch):
    """LLM 中间文本与工具行组交替时，组间各空一行（呼吸感）。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("我先调研一下。\n")  # 文本组开始
    block.append("✓ shell(ls)\n")  # 工具组
    block.append("✓ read(a.py)\n")  # 连续工具：不额外空行
    block.append("看到结果了。\n")  # 中间文本
    block.append("✓ grep(pattern)\n")  # 再一个工具组
    block.append("最终结论。\n")  # 最终文本
    block.flush()

    blank_lines = [w for w in rec.writes if w[0] == ""]
    # 开始空行 + 文本→工具 1 次 + 工具→文本 1 次 + 文本→工具 1 次 + 工具→文本 1 次（无结束空行）
    assert len(blank_lines) == 5, rec.writes
    # 工具行之间无空行
    tool_rows = [w[0] for w in rec.writes if w[0].startswith("✓")]
    assert tool_rows == ["✓ shell(ls)\n", "✓ read(a.py)\n", "✓ grep(pattern)\n"]
    # 文本顺序正确
    text_rows = [w[0] for w in rec.writes if w[0].startswith(("我", "看", "最"))]
    assert text_rows == ["我先调研一下。\n", "看到结果了。\n", "最终结论。\n"]


def test_pending_text_then_tool_does_not_glue(monkeypatch):
    """无换行半行正文后紧接 ✓：必须先落正文、空一行，再写工具——不得粘成一行。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("先刷只读层，用工具得出客观数据再谈删什么，")
    block.append("✓ shell(cd D:\\code_ws\\v8)\n")
    block.flush()

    assert [w[0] for w in rec.writes] == [
        "",
        "先刷只读层，用工具得出客观数据再谈删什么，",
        "",
        "✓ shell(cd D:\\code_ws\\v8)\n",
    ]
    # 正文与工具是两次提交，不是同一条字符串
    glued = [w[0] for w in rec.writes if "先刷" in w[0] and "✓" in w[0]]
    assert glued == [], glued


def test_mixed_text_and_tool_in_one_chunk_splits(monkeypatch):
    """单 chunk 内正文+工具行混排：按行拆开，交界恰好一空行。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("先说一句。\n✓ shell(a)\n再说一句。\n")
    block.flush()

    assert [w[0] for w in rec.writes] == [
        "",
        "先说一句。\n",
        "",
        "✓ shell(a)\n",
        "",
        "再说一句。\n",
    ]


def test_pure_tool_turn_blank_then_tight_tools(monkeypatch):
    """纯工具回合：块首工具行前空一行（与上方用户输入分隔），连续工具紧贴。"""
    block, rec = _block_with_recorder(monkeypatch)
    block.append("✓ read(a.py)\n")
    block.append("✓ shell(ls)\n")
    block.flush()
    assert [w[0] for w in rec.writes] == ["", "✓ read(a.py)\n", "✓ shell(ls)\n"]


def _patch_html(monkeypatch) -> list[str]:
    html_writes: list[str] = []
    monkeypatch.setattr("src.cli.streaming.CliScrollback.write_html", lambda f: html_writes.append(f))
    return html_writes


def test_write_tool_html_after_text_single_blank(monkeypatch):
    """通道 C 工具行在正文之后：恰好一空行；连续工具紧贴；后续正文再空一行。"""
    block, rec = _block_with_recorder(monkeypatch)
    html = _patch_html(monkeypatch)

    block.append("我先委派。\n")
    block.write_tool_html('<style fg="#888">✓ [coaras] read(a)</style>')
    block.write_tool_html('<style fg="#888">✓ [coaras] grep(b)</style>')
    block.append("✓ delegate(coaras)\n")
    block.append("结论如下。\n")
    block.flush()

    assert [w[0] for w in rec.writes] == [
        "",
        "我先委派。\n",
        "",
        "✓ delegate(coaras)\n",
        "",
        "结论如下。\n",
    ]
    assert html == [
        '<style fg="#888">✓ [coaras] read(a)</style>',
        '<style fg="#888">✓ [coaras] grep(b)</style>',
    ]


def test_write_tool_history_html_uses_active_block(monkeypatch):
    """write_tool_history_html 经 _active_block，与正文交界空一行。"""
    import src.cli.streaming as streaming_mod

    block, rec = _block_with_recorder(monkeypatch)
    html = _patch_html(monkeypatch)
    streaming_mod._active_block = block
    try:
        block.append("先说一句。\n")
        streaming_mod.write_tool_history_html("<b>✓ [coaras] read(x)</b>")
        block.append("再说一句。\n")
        block.flush()
    finally:
        streaming_mod._active_block = None

    assert [w[0] for w in rec.writes] == ["", "先说一句。\n", "", "", "再说一句。\n"]
    assert html == ["<b>✓ [coaras] read(x)</b>"]
