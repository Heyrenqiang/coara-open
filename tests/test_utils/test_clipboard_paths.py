"""Clipboard file-path formatting helpers."""

from __future__ import annotations

from src.utils.clipboard_paths import _uris_to_paths, clipboard_paths_payload, format_paths_for_input


def test_format_paths_quotes_spaces() -> None:
    assert format_paths_for_input([r"D:\code_ws\v8"]) == r"D:\code_ws\v8"
    assert format_paths_for_input([r"D:\my docs\a.txt"]) == r'"D:\my docs\a.txt"'
    assert format_paths_for_input([r"D:\a", r"D:\b c"]) == r'D:\a "D:\b c"'


def test_uris_to_paths_file_scheme() -> None:
    raw = "file:///C:/Users/A/file.txt\n# comment\nfile:///D:/ws/x"
    assert _uris_to_paths(raw) == [r"C:/Users/A/file.txt", r"D:/ws/x"]


def test_clipboard_paths_payload() -> None:
    assert clipboard_paths_payload([r"D:\a", r"D:\b c"]) == {
        "paths": [r"D:\a", r"D:\b c"],
        "text": r'D:\a "D:\b c"',
    }


def test_is_long_paste_text() -> None:
    from src.utils.clipboard_paths import LONG_TEXT_CHARS, is_long_paste_text

    assert is_long_paste_text("short") is False
    assert is_long_paste_text("x" * LONG_TEXT_CHARS) is True
    assert is_long_paste_text("\n".join(["line"] * 8)) is False
    assert is_long_paste_text("\n".join(["line"] * 12)) is True


def test_save_pasted_long_text(tmp_path) -> None:
    from src.utils.clipboard_paths import save_pasted_long_text

    path = save_pasted_long_text("hello\nworld", directory=tmp_path)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "hello\nworld"
    assert path.name.startswith("粘贴文本-")
