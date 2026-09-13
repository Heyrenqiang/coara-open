"""后台结果唤醒后，LLM 回复回投到「上一条用户输入所在端」。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.coara.root import RootCoara


def test_resolve_reply_input_source_prefers_origin_then_last_user() -> None:
    root = RootCoara.__new__(RootCoara)
    target = SimpleNamespace(_last_user_input_source="matrix")
    assert root._resolve_reply_input_source(target, "web") == "web"
    assert root._resolve_reply_input_source(target, "") == "matrix"
    assert root._resolve_reply_input_source(target, "background") == "matrix"
    assert root._resolve_reply_input_source(SimpleNamespace(_last_user_input_source=""), "") == ""


@pytest.mark.asyncio
async def test_consume_awakened_turn_mirrors_to_matrix_via_end_registry() -> None:
    """matrix 发起的唤醒回合：注册 EndRegistry「background」通道，正文逐帧
    流回房间（与 matrix 正常回合同一机制，无收尾镜像特例）。"""
    from src.coara.end_registry import EndRegistry

    root = RootCoara.__new__(RootCoara)
    mirrored: list[str] = []

    async def _send_to_user(text: str) -> bool:
        mirrored.append(text)
        return True

    root.matrix_notify = SimpleNamespace(send_to_user=_send_to_user, send_text=AsyncMock())
    root.end_registry = EndRegistry()

    class _Target:
        session_id = "sess-awaken"

        def __init__(self) -> None:
            self._last_user_input_source = "matrix"
            self.message_history: list = []

        async def process_message(self, content, **kwargs):
            # 模拟 base 回合内按段路由：段 source=background → 注册的本通道。
            outcome = root.end_registry.deliver(
                "background", self.session_id, {"kind": "chunk", "text": "视频已生成好了"}
            )
            if hasattr(outcome.value, "__await__"):
                await outcome.value
            if False:
                yield ""

    target = _Target()
    await root._consume_awakened_turn(target, "vid-1", "后台完成", origin_source="matrix")
    assert mirrored == ["视频已生成好了"]
    # 回合结束通道注销，不残留
    assert root.end_registry.sender_for("background", "sess-awaken") is None


@pytest.mark.asyncio
async def test_consume_awakened_turn_sends_tool_envelope_to_matrix() -> None:
    """matrix 唤醒回合的工具帧包成 [COARA_TOOL]，不把裸 label 当正文。"""
    from src.coara.end_registry import EndRegistry
    from src.matrix_client.tool_bridge import TOOL_ENVELOPE_PREFIX

    root = RootCoara.__new__(RootCoara)
    mirrored: list[str] = []

    async def _send_to_user(text: str) -> bool:
        mirrored.append(text)
        return True

    root.matrix_notify = SimpleNamespace(send_to_user=_send_to_user, send_text=AsyncMock())
    root.end_registry = EndRegistry()

    class _Target:
        session_id = "sess-awaken-tool"

        def __init__(self) -> None:
            self._last_user_input_source = "matrix"
            self.message_history: list = []

        async def process_message(self, content, **kwargs):
            outcome = root.end_registry.deliver(
                "background",
                self.session_id,
                {
                    "kind": "tool",
                    "text": "read(a.py)",
                    "tool_name": "read",
                    "tool_call_id": "c1",
                    "is_error": False,
                    "duration_ms": 12,
                },
            )
            if hasattr(outcome.value, "__await__"):
                await outcome.value
            if False:
                yield ""

    await root._consume_awakened_turn(_Target(), "bg-tool", "后台完成", origin_source="matrix")
    assert len(mirrored) == 1
    assert mirrored[0].startswith(TOOL_ENVELOPE_PREFIX)
    assert "read(a.py)" in mirrored[0]
    assert root.end_registry.sender_for("background", "sess-awaken-tool") is None


@pytest.mark.asyncio
async def test_consume_awakened_turn_routes_web_origin_to_web_server() -> None:
    """web 发起的后台任务：唤醒回合交给 web_server 流式回投浏览器。"""
    root = RootCoara.__new__(RootCoara)
    streamed: list[tuple] = []

    async def _stream_awakened_turn(target, content, *, origin_source, task_id, workspace_dir=None):
        streamed.append((content, origin_source, task_id, workspace_dir))

    root._web_server = SimpleNamespace(stream_awakened_turn=_stream_awakened_turn)

    class _Target:
        def __init__(self) -> None:
            self._last_user_input_source = "web"
            self.workspace_dir = "D:/ws/launch"
            self.message_history: list = []

        async def process_message(self, content, **kwargs):
            if False:
                yield ""

    await root._consume_awakened_turn(_Target(), "bg-1", "后台完成", origin_source="web")
    # 唤醒回合视图落盘锚到发起空间（target.workspace_dir），不随当前视图漂移（P1-3b）
    assert streamed == [("后台完成", "web", "bg-1", "D:/ws/launch")]


@pytest.mark.asyncio
async def test_consume_awakened_turn_web_origin_no_server_falls_back_web_source() -> None:
    """web origin 但 web_server 缺席：source 用 web（非 background），保住回投路径。"""
    root = RootCoara.__new__(RootCoara)
    root._web_server = None
    seen_source: list[str] = []

    class _Target:
        def __init__(self) -> None:
            self._last_user_input_source = "web"
            self.message_history: list = []

        async def process_message(self, content, **kwargs):
            seen_source.append(str(kwargs.get("source") or ""))
            if False:
                yield ""

    await root._consume_awakened_turn(_Target(), "bg-2", "后台完成", origin_source="web")
    assert seen_source == ["web"]
