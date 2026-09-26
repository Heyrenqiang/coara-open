"""注入信封不得进入任何显示出口（web / CLI）。

``<后台结果>`` / ``<系统提醒>`` / ``<系统消息>`` / ``<子智能体消息>`` / ``<途中消息>``
/ ``<情境>`` / ``<接续输入>`` 只给模型看：内核若把它漏投成主会话正文（或子智能体
正文），端侧出口必须拦掉——否则用户会在对话流里读到内核信封原文，实时冒出来、
刷新后又消失（因为写端本来就不落这一帧）。
"""

from __future__ import annotations

import pytest

from src.ui.attach_ws import _attach_output_frame
from src.ui.web_server import WebServer

_ENVELOPES = (
    "<后台结果>\n后台任务 sa-aide-1 成功\n输出: 报告",
    "<系统提醒>读一下日志</系统提醒>",
    "<系统消息>仅参考</系统消息>",
    "<子智能体消息>\n[aide] 中途汇报</子智能体消息>",
    "<途中消息>别改那个文件</途中消息>",
    "<情境>当前对话来自 web 端</情境>",
    "<接续输入>补充一句</接续输入>",
)


class _FakeStream:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def emit(self, kind: str, **payload) -> None:
        self.frames.append((kind, payload))


@pytest.mark.parametrize("text", _ENVELOPES)
def test_web_end_frame_drops_envelope_on_chunk(text: str) -> None:
    stream = _FakeStream()
    WebServer._emit_end_frame(stream, {"kind": "chunk", "text": text})
    assert stream.frames == []


@pytest.mark.parametrize("kind", ["subagent_chunk", "subagent_result"])
def test_web_end_frame_drops_envelope_on_subagent_channels(kind: str) -> None:
    stream = _FakeStream()
    WebServer._emit_end_frame(
        stream,
        {"kind": kind, "text": "<后台结果>\n任务成功", "tool_call_id": "call_1"},
    )
    assert stream.frames == []


def test_web_end_frame_keeps_normal_text() -> None:
    stream = _FakeStream()
    WebServer._emit_end_frame(stream, {"kind": "chunk", "text": "普通正文"})
    assert [kind for kind, _ in stream.frames] == ["chunk"]


@pytest.mark.parametrize("text", _ENVELOPES)
def test_attach_output_frame_drops_envelope(text: str) -> None:
    assert _attach_output_frame({"kind": "chunk", "text": text}) is None
    assert _attach_output_frame({"kind": "subagent_result", "text": text, "tool_call_id": "call_1"}) is None


def test_attach_output_frame_keeps_normal_text() -> None:
    assert _attach_output_frame({"kind": "chunk", "text": "普通正文"}) == (
        "chunk",
        {"text": "普通正文"},
    )
