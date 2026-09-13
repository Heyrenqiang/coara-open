"""CLI basic Markdown → Rich Text."""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

import pytest

from src.cli.md_links import looks_like_fs_path, try_rich_basic_markdown


def test_fast_path_plain_returns_none() -> None:
    assert try_rich_basic_markdown("普通一段话没有标记") is None
    assert try_rich_basic_markdown("中文顿写 并行/扇入/顺序") is None


def test_non_three_kind_link_kept_literal_but_other_md_still_renders() -> None:
    # 仅非法链接、无其它标记 → None（不必建 Text）
    assert try_rich_basic_markdown("[说明](submit)") is None


def test_bold_code_header_list() -> None:
    text = "# 标题\n- 一项 **加粗** 与 `code`\n正文 *斜体*"
    rich = try_rich_basic_markdown(text)
    assert rich is not None
    plain = rich.plain
    assert "标题" in plain
    assert "• " in plain
    assert "加粗" in plain
    assert "code" in plain
    assert "斜体" in plain
    assert "**" not in plain
    assert "`code`" not in plain


def test_http_and_path_links() -> None:
    text = "见 [官网](https://example.com) 与 [main.py](D:/proj/src/main.py)"
    rich = try_rich_basic_markdown(text)
    assert rich is not None
    assert "官网" in rich.plain
    assert "https://example.com" not in rich.plain
    links = {span.style.link for span in rich.spans if span.style and span.style.link}
    assert any(u and u.startswith("https://") for u in links)
    # 普通路径优先 file:；脚本在检测到 Cursor 时用 cursor://file/，否则 coara-open:
    assert any(
        u
        and (
            u.startswith("file:")
            or u.startswith("coara-open:")
            or u.startswith("cursor://")
            or u.startswith("vscode://")
        )
        for u in links
    )
    from src.cli.theme import get_theme

    expected = get_theme().text.link.casefold()
    link_spans = [span for span in rich.spans if span.style and span.style.link]
    assert link_spans
    for span in link_spans:
        assert span.style.color is not None
        assert span.style.color.triplet is not None
        assert span.style.color.triplet.hex.casefold() == expected
        assert span.style.underline is True


def test_local_open_href_and_parse(monkeypatch, tmp_path) -> None:
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)  # 跳过注册
    monkeypatch.setattr(local_open, "_editor_exe_cache", None)
    script = tmp_path / "a.py"
    script.write_text("x", encoding="utf-8")
    doc = tmp_path / "readme.md"
    doc.write_text("hi", encoding="utf-8")
    html = tmp_path / "htmls" / "待办.html"
    html.parent.mkdir()
    html.write_text("<html></html>", encoding="utf-8")
    folder = tmp_path / "pkg"
    folder.mkdir()

    script_href = local_open.href_for_local_path(script)
    doc_href = local_open.href_for_local_path(doc)
    html_href = local_open.href_for_local_path(html)
    folder_href = local_open.href_for_local_path(folder)

    if sys.platform == "win32":
        # Windows：本地一律 coara-open（避开 file:// 中文路径静默失败）
        for href, path in (
            (script_href, script),
            (doc_href, doc),
            (html_href, html),
            (folder_href, folder),
        ):
            assert href.startswith("coara-open:"), href
            # URI 不得含原始反斜杠（ShellExecute/%1 会当转义吃掉）
            assert "\\" not in href, href
            # 非 ASCII 必须 b64，禁止 %XX（会被 %ENV% 展开撕碎）
            if not str(path).isascii():
                assert ":b64:" in href, href
                assert "%" not in href.split("b64:", 1)[-1]
            else:
                assert "b64:" not in href
            parsed = local_open.path_from_protocol_uri(href)
            assert parsed is not None
            assert parsed.resolve() == path.resolve()
    else:
        assert script_href.startswith("file:")
        assert doc_href.startswith("file:")
        assert html_href.startswith("file:")
        assert folder_href.startswith("file:")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows URI backslash escape")
def test_win_href_survives_backslash_escape_segments(monkeypatch, tmp_path) -> None:
    """路径含 \\out \\bar 等段时，URI 正斜杠 round-trip 仍指向原文件。"""
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)
    # 故意造出容易被当转义的目录名
    media = tmp_path / "out" / "bar" / "clip.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    href = local_open.href_for_local_path(media)
    assert href.startswith("coara-open:")
    assert "\\" not in href
    assert "%5C" not in href  # 正斜杠而非编码反斜杠
    parsed = local_open.path_from_protocol_uri(href)
    assert parsed is not None
    assert parsed.resolve() == media.resolve()


def test_relative_html_markdown_link_resolves(monkeypatch, tmp_path) -> None:
    """相对路径 htmls/待办.html 应可链且 Windows 走 coara-open。"""
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "htmls" / "待办.html"
    target.parent.mkdir()
    target.write_text("<p>x</p>", encoding="utf-8")
    rich = try_rich_basic_markdown("见 [待办](htmls/待办.html)")
    assert rich is not None
    links = [span.style.link for span in rich.spans if span.style and span.style.link]
    assert links
    href = links[0]
    assert href is not None
    if sys.platform == "win32":
        assert href.startswith("coara-open:")
        assert ":b64:" in href  # 中文文件名
        assert "%" not in href.split("b64:", 1)[-1]
        parsed = local_open.path_from_protocol_uri(href)
        assert parsed is not None
        assert parsed.resolve() == target.resolve()
    else:
        assert href.startswith("file:")


def test_script_href_uses_coara_open_not_cursor(monkeypatch, tmp_path) -> None:
    """OSC 8 不用 cursor://（终端常无响应）；有编辑器时仍走 coara-open。"""
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)
    cursor = tmp_path / "Cursor.exe"
    cursor.write_text("", encoding="utf-8")
    monkeypatch.setattr(local_open, "_editor_exe_cache", cursor)
    script = tmp_path / "a.py"
    script.write_text("x", encoding="utf-8")
    href = local_open.href_for_local_path(script)
    if sys.platform == "win32":
        assert href.startswith("coara-open:")
        assert "cursor://" not in href
    else:
        assert href.startswith("file:")


def test_gui_exe_from_cursor_cmd_shim(tmp_path) -> None:
    from src.cli.local_open import _gui_exe_from_shim

    root = tmp_path / "cursor"
    bin_dir = root / "resources" / "app" / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "cursor.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    exe = root / "Cursor.exe"
    exe.write_text("", encoding="utf-8")
    assert _gui_exe_from_shim(shim) == exe.resolve()


def test_protocol_launcher_prefers_pythonw(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from src.cli import local_open

    py = tmp_path / "python.exe"
    pyw = tmp_path / "pythonw.exe"
    py.write_text("", encoding="utf-8")
    pyw.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(py))
    assert Path(local_open._protocol_launcher_exe()) == pyw


def test_patch_safe_uri_schemes() -> None:
    from src.cli.local_open import _patch_safe_uri_schemes

    bare = '{\n    "theme": "dark"\n}'
    patched = _patch_safe_uri_schemes(bare, "coara-open")
    assert patched is not None
    assert '"safeUriSchemes": [ "coara-open" ]' in patched

    existing = '{\n    "safeUriSchemes": ["vscode"]\n}'
    patched2 = _patch_safe_uri_schemes(existing, "coara-open")
    assert patched2 is not None
    assert "vscode" in patched2
    assert "coara-open" in patched2

    already = '{\n    "safeUriSchemes": ["coara-open"]\n}'
    assert _patch_safe_uri_schemes(already, "coara-open") == already


@pytest.mark.skipif(sys.platform != "win32", reason="Windows open strategy")
def test_win_open_file_prefers_shell_execute(monkeypatch, tmp_path) -> None:
    """图片等文件：优先 ShellExecuteW，避免 cmd start 撕中文路径。"""
    from src.cli import local_open

    png = tmp_path / "uploads" / "a.png"
    png.parent.mkdir(parents=True)
    png.write_bytes(b"x")
    opened: list[Path] = []

    def fake_shell(path: Path, *, operation: str = "open") -> bool:
        del operation
        opened.append(path)
        return True

    monkeypatch.setattr(local_open, "_win_shell_execute", fake_shell)
    monkeypatch.setattr(local_open, "_PROTOCOL_HOLD_S", 0)
    local_open.open_local_path(png)
    assert opened and opened[0].resolve() == png.resolve()


def test_recover_nearby_same_name_in_media_gen(tmp_path) -> None:
    """链到工作空间根下的文件名，实际在 media_gen/ 时能找回。"""
    from src.cli.local_open import _recover_nearby_same_name

    real = tmp_path / "media_gen" / "欧拉公式_PPT.png"
    real.parent.mkdir()
    real.write_bytes(b"x")
    linked = tmp_path / "欧拉公式_PPT.png"
    assert not linked.exists()
    got = _recover_nearby_same_name(linked)
    assert got is not None
    assert got.resolve() == real.resolve()


def test_open_missing_without_nearby_opens_parent(monkeypatch, tmp_path) -> None:
    """虚路径且附近无同名文件：打开父目录，不对虚路径 start。"""
    from src.cli import local_open

    missing = tmp_path / "no_such.png"
    opened: list[Path] = []

    monkeypatch.setattr(local_open, "_open_dir_best_effort", lambda p: opened.append(p))
    monkeypatch.setattr(
        local_open,
        "_open_file_best_effort",
        lambda p: (_ for _ in ()).throw(AssertionError(f"must not open missing {p}")),
    )
    local_open.open_local_path(missing)
    assert opened == [tmp_path.resolve()]


def test_open_missing_recovers_media_gen(monkeypatch, tmp_path) -> None:
    from src.cli import local_open

    real = tmp_path / "media_gen" / "a.png"
    real.parent.mkdir()
    real.write_bytes(b"x")
    linked = tmp_path / "a.png"
    opened: list[Path] = []

    monkeypatch.setattr(
        local_open,
        "_win_shell_execute",
        lambda p, *, operation="open": opened.append(p) or True,
    )
    monkeypatch.setattr(local_open, "_PROTOCOL_HOLD_S", 0)
    if sys.platform != "win32":
        monkeypatch.setattr(local_open, "_open_file_best_effort", lambda p: opened.append(p))
    local_open.open_local_path(linked)
    assert opened and opened[0].resolve() == real.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows open strategy")
def test_win_open_dir_prefers_explorer(monkeypatch, tmp_path) -> None:
    from src.cli import local_open

    folder = tmp_path / "media"
    folder.mkdir()
    calls: list[list[str]] = []

    def fake_spawn(argv: list[str]) -> bool:
        calls.append(list(argv))
        return True

    monkeypatch.setattr(local_open, "_spawn_detached", fake_spawn)
    local_open.open_local_path(folder)
    assert calls and calls[0][0] == "explorer"
    assert Path(calls[0][1]).resolve() == folder.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows open strategy")
def test_win_href_image_under_uploads_roundtrip(monkeypatch, tmp_path) -> None:
    """``.coara/uploads`` 含 \\u 段：正斜杠 href 解析后仍指向原图。"""
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)
    img = tmp_path / ".coara" / "uploads" / "image.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"x")
    href = local_open.href_for_local_path(img)
    assert href.startswith("coara-open:")
    assert "\\" not in href
    parsed = local_open.path_from_protocol_uri(href)
    assert parsed is not None
    assert parsed.resolve() == img.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows %ENV% shreds percent-encoding")
def test_win_chinese_path_uses_b64_not_percent(monkeypatch, tmp_path) -> None:
    """中文路径必须 b64，不能 %E6%…（ShellExecute 会当环境变量展开）。"""
    from src.cli import local_open

    monkeypatch.setattr(local_open, "_registered", True)
    img = tmp_path / "media_gen" / "欧拉公式_PPT.png"
    img.parent.mkdir()
    img.write_bytes(b"x")
    href = local_open.href_for_local_path(img)
    assert href.startswith("coara-open:b64:")
    assert "%E6" not in href
    assert "%" not in href  # urlsafe b64 无 %
    parsed = local_open.path_from_protocol_uri(href)
    assert parsed is not None
    assert parsed.resolve() == img.resolve()
    # 旧版 percent-encoding 仍可解析（未被系统撕碎时）
    legacy = "coara-open:" + urllib.parse.quote(str(img.resolve()).replace("\\", "/"), safe=":/")
    legacy_parsed = local_open.path_from_protocol_uri(legacy)
    assert legacy_parsed is not None
    assert legacy_parsed.resolve() == img.resolve()


def test_inline_code_uses_theme_code_color() -> None:
    from src.cli.theme import get_theme

    rich = try_rich_basic_markdown("见 `__pycache__/`")
    assert rich is not None
    expected = get_theme().text.code.casefold()
    code_spans = [
        span
        for span in rich.spans
        if span.style and span.style.color is not None and "__pycache__" in rich.plain[span.start : span.end]
    ]
    assert code_spans
    for span in code_spans:
        assert span.style.dim is not True
        assert span.style.color.triplet is not None
        assert span.style.color.triplet.hex.casefold() == expected


def test_tool_line_skipped() -> None:
    assert try_rich_basic_markdown("✓ read(D:/a/b.py)") is None
    assert try_rich_basic_markdown("✗ shell 报错: **boom**") is None


def test_fence_passthrough_dim() -> None:
    text = "前\n```\ncode **not** bold\n```\n后 **粗**"
    rich = try_rich_basic_markdown(text)
    assert rich is not None
    # 围栏内保留星号；围栏外加粗吃掉 **
    assert "code **not** bold" in rich.plain
    assert "粗" in rich.plain
    assert "后 **粗**" not in rich.plain


def test_table_cjk_columns_align() -> None:
    """中文按终端双宽对齐：第二列起点（display column）应一致。"""
    from prompt_toolkit.utils import get_cwidth

    from src.cli.terminal_width import display_width

    text = "| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n| 很长名字 | 22 |\n"
    rich = try_rich_basic_markdown(text)
    assert rich is not None
    lines = [ln for ln in rich.plain.splitlines() if ln.strip()]

    def col2_display_start(line: str) -> int:
        i = 0
        while i < len(line) and line[i] != " ":
            i += 1
        while i < len(line) and line[i] == " ":
            i += 1
        return sum(get_cwidth(ch) for ch in line[:i])

    starts = [col2_display_start(ln) for ln in lines]
    assert len(set(starts)) == 1, starts
    assert starts[0] == display_width("很长名字") + 2


def test_split_commit_holding_open_table() -> None:
    from src.cli.md_links import split_commit_holding_open_table

    ready, hold = split_commit_holding_open_table("| a | b |\n")
    assert ready == ""
    assert hold.startswith("| a |")

    ready, hold = split_commit_holding_open_table("| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    assert ready == ""
    assert "| --- |" in hold

    ready, hold = split_commit_holding_open_table("| a | b |\n| --- | --- |\n| 1 | 2 |\n正文\n")
    assert hold == ""
    assert "正文" in ready
    assert "| a | b |" in ready


def test_streaming_holds_table_until_closed() -> None:
    import src.cli.scrollback as sb
    from src.cli.streaming import StreamingBlock

    writes: list[str] = []

    def _capture(text: str, *, style: str = "", end: str = "\n") -> None:
        writes.append(text)

    block = StreamingBlock()
    orig = sb.CliScrollback.write
    sb.CliScrollback.write = staticmethod(_capture)  # type: ignore[method-assign]
    try:
        block.append("| 名称 | 值 |\n")
        # 块首可能先写分隔空行；表体本身应仍暂扣
        assert not any("|" in w for w in writes)
        block.append("| --- | --- |\n")
        assert not any("|" in w for w in writes)
        block.append("| a | 1 |\n")
        assert not any("|" in w for w in writes)
        block.append("\n")
        body = [w for w in writes if "|" in w]
        assert len(body) == 1
        assert "| --- |" in body[0]
        assert "名称" in body[0]
    finally:
        sb.CliScrollback.write = orig  # type: ignore[method-assign]


def test_looks_like_fs_path() -> None:
    assert looks_like_fs_path(r"D:\proj\src\main.py")
    assert looks_like_fs_path("src/coara")
    assert looks_like_fs_path("文档/说明.md")
    assert looks_like_fs_path("D:/项目/src")
    assert not looks_like_fs_path("submit")
    assert not looks_like_fs_path("/new")
    assert not looks_like_fs_path("扇入/顺序")
    assert not looks_like_fs_path("范围/方法/规则")
