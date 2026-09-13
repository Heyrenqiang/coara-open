"""Matrix response stream: typing stays on for the whole turn + tool summaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.matrix_client.response_stream import stream_coara_reply_to_matrix


class _FakeRoot:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.event_bus = SimpleNamespace(publish=lambda *_a, **_k: None)
        self.identity = SimpleNamespace(coara_id="c1", name="考拉")
        # stream_coara_reply_to_matrix binds the turn to the foreground coara.
        self._foreground_session_id = None
        self.foreground_coara = self
        self.session_id = "s1"
        self.workspace_dir = ""

    async def process_message(self, *_args, **_kwargs):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_stream_forwards_all_assistant_chunks() -> None:
    """Regression: all assistant chunks are forwarded as room messages."""
    root = _FakeRoot(["第一段回复\n", "第二段继续\n"])
    sent: list[str] = []

    async def send_chunk(_room: str, body: str) -> None:
        sent.append(body)

    await stream_coara_reply_to_matrix(
        root,
        "hello",
        room_id="!r",
        trust_level="owner",
        send_chunk=send_chunk,
    )

    assert sent == ["第一段回复\n", "第二段继续\n"]


@pytest.mark.asyncio
async def test_detached_turn_chunks_sent_with_workspace_prefix() -> None:
    """Mid-turn workspace switch: remaining chunks still go to the room,
    prefixed with the origin workspace name."""
    root = _FakeRoot(["切前\n", "切后一\n", "✓ shell(ls)\n", "切后二\n"])
    root._foreground_session_id = "ws-a"
    root.workspace_dir = "D:/ws/shop"
    sent: list[str] = []

    async def send_chunk(_room: str, body: str) -> None:
        sent.append(body)
        if len(sent) == 1:
            # User switches to another workspace after the first chunk.
            root._foreground_session_id = "ws-b"

    await stream_coara_reply_to_matrix(
        root,
        "hello",
        room_id="!r",
        trust_level="owner",
        send_chunk=send_chunk,
    )

    # Tool-summary chunks are still not sent remotely; detached text chunks
    # carry the workspace tag.
    assert sent == ["切前\n", "[shop] 切后一\n", "[shop] 切后二\n"]


def test_tool_summary_should_send_to_matrix_removed() -> None:
    """白名单函数已随「工具摘要不进 Matrix 房间」移除。"""
    import src.matrix_client.response_stream as mod

    assert not hasattr(mod, "tool_summary_should_send_to_matrix")
    assert not hasattr(mod, "MATRIX_TOOL_SUMMARY_ALLOWLIST")


@pytest.mark.asyncio
async def test_tool_summaries_never_sent_to_room() -> None:
    """工具摘要一条都不进 Matrix 房间；本地回显仍可用。"""
    root = _FakeRoot(
        [
            "✓ read(a)\n",
            "✓ web_search(q)\n",
            "✓ grep(b)\n",
            "回复文本\n",
            "✗ shell 报错\n",
            "✓ skill(activate)\n",
        ],
    )
    sent: list[str] = []
    echoed: list[str] = []

    async def send_chunk(_room: str, body: str) -> None:
        sent.append(body)

    async def echo_local(body: str) -> None:
        echoed.append(body)

    await stream_coara_reply_to_matrix(
        root,
        "hi",
        room_id="!r",
        trust_level="owner",
        send_chunk=send_chunk,
        echo_tool_summary_local=echo_local,
    )

    # 房间只收到正常回复文本；工具摘要全部只在本地终端回显
    assert sent == ["回复文本\n"]
    assert echoed == [
        "✓ read(a)\n",
        "✓ web_search(q)\n",
        "✓ grep(b)\n",
        "✗ shell 报错\n",
        "✓ skill(activate)\n",
    ]
