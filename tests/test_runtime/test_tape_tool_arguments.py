"""录像带 tool 帧参数：比用量摘要多留 write/edit 正文。"""

from __future__ import annotations

from src.runtime.usage_args import compact_tool_usage_args, tape_tool_arguments


def test_write_tape_args_include_contents() -> None:
    args = {"path": "D:/ws/a.py", "contents": "hello\n" * 20}
    compact = compact_tool_usage_args("write", args)
    tape = tape_tool_arguments("write", args)
    assert compact == {"path": "D:/ws/a.py"}
    assert tape["path"] == "D:/ws/a.py"
    assert "hello" in str(tape["contents"])


def test_edit_tape_args_include_strings() -> None:
    args = {"path": "D:/ws/a.py", "old_string": "aaa", "new_string": "bbb"}
    tape = tape_tool_arguments("edit", args)
    assert tape["path"] == "D:/ws/a.py"
    assert tape["old_string"] == "aaa"
    assert tape["new_string"] == "bbb"


def test_write_tape_args_accept_content_alias() -> None:
    tape = tape_tool_arguments("write", {"path": "/tmp/x", "content": "body"})
    assert tape["contents"] == "body"
