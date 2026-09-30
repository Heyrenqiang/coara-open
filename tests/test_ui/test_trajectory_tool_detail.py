"""录像带 tool 详情：出站保留 arguments/tool_output；投影认 ok/is_error。"""

from __future__ import annotations

from src.ui.trajectory import _project_frame
from src.ui.web_server import WebServer


class _RecordingStream:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def emit(self, kind: str, **payload: object) -> None:
        self.frames.append((kind, payload))


def test_web_end_frame_forwards_tool_arguments_and_output() -> None:
    stream = _RecordingStream()
    WebServer._emit_end_frame(
        stream,
        {
            "kind": "tool",
            "text": "write - D:/ws/a.py",
            "is_error": False,
            "tool_name": "write",
            "tool_call_id": "c1",
            "duration_ms": 12.0,
            "arguments": {"path": "D:/ws/a.py", "contents": "x = 1\n"},
            "tool_output": "已向 D:/ws/a.py 写入 6 个字符（创建）",
            "tool_output_ref": "spill/abc",
            "tool_output_truncated": True,
        },
    )
    assert len(stream.frames) == 1
    kind, payload = stream.frames[0]
    assert kind == "tool"
    assert payload["arguments"]["contents"] == "x = 1\n"
    assert "已向" in str(payload["tool_output"])
    assert payload["ok"] is True
    assert payload["is_error"] is False
    assert payload["tool_output_ref"] == "spill/abc"
    assert payload["tool_output_truncated"] is True


def test_web_end_frame_caps_huge_tool_output() -> None:
    stream = _RecordingStream()
    WebServer._emit_end_frame(
        stream,
        {
            "kind": "tool",
            "text": "read - big",
            "is_error": False,
            "tool_name": "read",
            "tool_call_id": "c-big",
            "arguments": {"path": "/big"},
            "tool_output": "Z" * 50_000,
            "tool_output_ref": "spill/big",
        },
    )
    kind, payload = stream.frames[0]
    assert kind == "tool"
    assert len(str(payload["tool_output"])) < 50_000
    assert payload["tool_output_truncated"] is True
    assert payload["tool_output_ref"] == "spill/big"


def test_project_tool_row_reads_arguments_and_legacy_ok() -> None:
    row = _project_frame(
        {
            "view_seq": 1,
            "ts": 1.0,
            "source": "web",
            "turn_id": "t",
            "kind": "tool",
            "payload": {
                "text": "write - /tmp/x",
                "ok": True,
                "tool_name": "write",
                "tool_call_id": "c1",
                "arguments": {"path": "/tmp/x", "contents": "hi"},
                "tool_output": "ok",
                "tool_output_ref": "spill/x",
                "tool_output_truncated": True,
            },
        }
    )
    assert row is not None
    assert row["tool"]["is_error"] is False
    assert row["tool"]["arguments"]["contents"] == "hi"
    assert row["tool"]["output"] == "ok"
    assert row["tool"]["output_ref"] == "spill/x"
    assert row["tool"]["output_truncated"] is True


def test_project_frame_reads_desk_from_payload() -> None:
    """旧帧 desk 只在 payload：投影后仍要有 actor，刷新后外挂组才能折叠。"""
    row = _project_frame(
        {
            "view_seq": 9,
            "ts": 1.0,
            "source": "cli-attached",
            "turn_id": "t",
            "kind": "chunk",
            "payload": {"text": "维护完成", "desk": "janitor"},
        }
    )
    assert row is not None
    assert row["actor"] == "janitor"
    assert row["role"] == "assistant"
