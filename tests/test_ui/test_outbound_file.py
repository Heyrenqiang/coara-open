"""Tests for outbound file routing (Matrix vs Web) — Matrix path must stay intact."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.builtin.integration.outbound_file import (
    OutboundFileRouter,
    ensure_outbound_file_router,
    register_send_file_on_root,
    resolve_outbound_targets,
)
from src.ui.web_file_bridge import WebFileBridge


def test_resolve_auto_with_room_id_forces_matrix() -> None:
    assert resolve_outbound_targets("auto", has_matrix=True, has_web=True, room_id="!room:hs") == ["matrix"]


def test_resolve_explicit_matrix_without_bridge_errors() -> None:
    with pytest.raises(ValueError, match="未连接 Matrix"):
        resolve_outbound_targets("matrix", has_matrix=False, has_web=True)


def test_resolve_explicit_web_without_bridge_errors() -> None:
    with pytest.raises(ValueError, match="Web"):
        resolve_outbound_targets("web", has_matrix=True, has_web=False)


def test_resolve_cli_local_end_has_no_delivery() -> None:
    # No remote channel context → 本地端（CLI）无文件接收能力，判不可用。
    with pytest.raises(ValueError, match="无文件接收能力"):
        resolve_outbound_targets("auto", has_matrix=True, has_web=True)


@pytest.mark.asyncio
async def test_resolve_auto_web_remote_turn_prefers_web() -> None:
    from src.coara.turn_context import turn
    from src.ui.web_interaction_channel import WebRemoteInteractionChannel
    from src.ui.web_socket_registry import WebSocketRegistry

    channel = WebRemoteInteractionChannel(WebSocketRegistry())
    async with turn("web", channel_id="ws-conn-1", interaction_channel=channel):
        assert resolve_outbound_targets("auto", has_matrix=True, has_web=True) == ["web"]


@pytest.mark.asyncio
async def test_resolve_auto_matrix_remote_turn_stays_matrix() -> None:
    """Non-Web remote channel (phone) must never divert to Web on auto."""
    from src.coara.turn_context import turn

    class _MatrixLikeChannel:
        async def send_text(self, body: str) -> bool:
            return True

    async with turn("matrix", channel_id="!room:hs", interaction_channel=_MatrixLikeChannel()):
        assert resolve_outbound_targets("auto", has_matrix=True, has_web=True) == ["matrix"]


@pytest.mark.asyncio
async def test_router_matrix_path_unchanged_kwargs(tmp_path: Path) -> None:
    """Matrix bridge still receives path/caption/room_id exactly as before."""
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"%PDF")

    matrix = MagicMock()
    matrix.send_file = AsyncMock(return_value={"filename": "report.pdf", "room_id": "!r:hs", "send_result": "ok"})
    web = MagicMock()
    web.send_file = AsyncMock()

    router = OutboundFileRouter()
    router.set_matrix(matrix)
    router.set_web(web)

    result = await router.send_file(sample, caption="你好", room_id="!r:hs", target="auto")

    matrix.send_file.assert_awaited_once_with(sample, caption="你好", room_id="!r:hs", owner=None)
    web.send_file.assert_not_awaited()
    assert result["channel"] == "matrix"
    assert result["filename"] == "report.pdf"


@pytest.mark.asyncio
async def test_router_web_target_never_calls_matrix(tmp_path: Path) -> None:
    sample = tmp_path / "pic.png"
    sample.write_bytes(b"\x89PNG")

    matrix = MagicMock()
    matrix.send_file = AsyncMock()
    web = MagicMock()
    web.send_file = AsyncMock(return_value={"filename": "pic.png", "file_id": "abc", "send_result": "ok"})

    router = OutboundFileRouter()
    router.set_matrix(matrix)
    router.set_web(web)

    result = await router.send_file(sample, caption="图", target="web")

    web.send_file.assert_awaited_once_with(sample, caption="图", room_id="", owner=None)
    matrix.send_file.assert_not_awaited()
    assert result["channel"] == "web"


@pytest.mark.asyncio
async def test_set_web_does_not_clear_matrix(tmp_path: Path) -> None:
    matrix = MagicMock()
    matrix.send_file = AsyncMock(return_value={"filename": "a.txt", "send_result": "ok"})
    web = MagicMock()
    web.send_file = AsyncMock(return_value={"filename": "a.txt", "send_result": "ok"})

    router = OutboundFileRouter()
    router.set_matrix(matrix)
    router.set_web(web)
    assert router.matrix is matrix
    assert router.web is web

    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")
    await router.send_file(sample, room_id="!keep:hs")
    matrix.send_file.assert_awaited()
    web.send_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbound_file_lands_in_owner_space_not_current_view(tmp_path: Path) -> None:
    """A 空间发起投递、此刻前台视图是 B：帧必须落 A 的线（B 线零帧），实时帧带 A 归属。

    实测事故形状：nx 会话请求的视频异步完成后，卡片被按「当时的 v8 视图」写进 v8 线。
    """
    from src.tools.builtin.integration.outbound_file import OutboundOwner
    from src.ui.handlers.files import FilesHandlers
    from src.ui.web_views import WebViewStore, resolve_web_view_path

    coara_home = tmp_path / "home"
    coara_home.mkdir()
    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    sample = tmp_path / "video.mp4"
    sample.write_bytes(b"\x00" * 16)

    recorded: list[dict[str, Any]] = []
    recorder_a = MagicMock()
    recorder_a.record_files = MagicMock(side_effect=lambda **kw: recorded.append(kw))
    coara_a = SimpleNamespace(session_id="sess-a", workspace_dir=ws_a, _session_log=recorder_a)
    view_b = SimpleNamespace(session_id="sess-b", workspace_dir=ws_b)

    class _Server(FilesHandlers):
        pass

    server = _Server()
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.workspace_dir = ws_b  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_coara = lambda: view_b  # type: ignore[method-assign]
    server.root = SimpleNamespace(_sessions={"a-id": SimpleNamespace(coara=coara_a)})  # type: ignore[attr-defined]

    registry = MagicMock()
    registry.has_active.return_value = True
    registry.send_to_active = AsyncMock(return_value=True)
    bridge = WebFileBridge(
        registry=registry,
        delivery_dir=tmp_path / "outbound",
        persist=server._persist_outbound_file,
        coara_home=coara_home,
    )
    owner = OutboundOwner(workspace_dir=str(ws_a), session_id="sess-a", turn_id="turn-a")

    result = await bridge.send_file(sample, caption="视频", owner=owner)
    server._view_store.flush(timeout=2.0)

    path_a = resolve_web_view_path(ws_a, coara_home=coara_home, subject="root", session_id="sess-a")
    path_b = resolve_web_view_path(ws_b, coara_home=coara_home, subject="root", session_id="sess-b")
    frames_a = WebViewStore.iter_frames(path_a)
    assert [f["kind"] for f in frames_a] == ["files"], "卡片必须落发起者（A）的线"
    assert frames_a[0]["session_id"] == "sess-a"
    assert frames_a[0]["turn_id"] == "turn-a"
    assert frames_a[0]["payload"]["files"][0]["file_id"] == result["file_id"]
    assert not path_b.exists() or WebViewStore.iter_frames(path_b) == [], "B 线必须零帧"
    # 录像带用发起者自己的 recorder
    assert recorded and recorded[0]["turn_id"] == "turn-a"
    # 实时帧带发起者归属（端上据此丢弃/缓冲不匹配的帧）
    body = registry.send_to_active.await_args.args[0]
    assert body["workspace_dir"] == str(ws_a)
    assert body["session_id"] == "sess-a"
    assert body["turn_id"] == "turn-a"
    server._view_store.close()


@pytest.mark.asyncio
async def test_outbound_file_without_owner_is_not_persisted(tmp_path: Path) -> None:
    """找不到发起者归属时宁可不落带（跨空间落带是病根，不拿当前视图兜底）。"""
    from src.ui.handlers.files import FilesHandlers
    from src.ui.web_views import WebViewStore, resolve_web_view_path

    coara_home = tmp_path / "home"
    coara_home.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    sample = tmp_path / "pic.png"
    sample.write_bytes(b"\x89PNG")

    server = FilesHandlers()
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.workspace_dir = ws_b  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_coara = lambda: SimpleNamespace(session_id="sess-b", workspace_dir=ws_b)  # type: ignore[method-assign]
    server.root = SimpleNamespace(_sessions={})  # type: ignore[attr-defined]

    registry = MagicMock()
    registry.has_active.return_value = True
    registry.send_to_active = AsyncMock(return_value=True)
    bridge = WebFileBridge(
        registry=registry,
        delivery_dir=tmp_path / "outbound",
        persist=server._persist_outbound_file,
        coara_home=coara_home,
    )

    await bridge.send_file(sample, caption="图")  # 无 owner
    server._view_store.flush(timeout=2.0)

    path_b = resolve_web_view_path(ws_b, coara_home=coara_home, subject="root", session_id="sess-b")
    assert not path_b.exists() or WebViewStore.iter_frames(path_b) == []
    server._view_store.close()


def test_send_file_invocation_owner_from_bound_context(tmp_path: Path) -> None:
    """send_file 的发起者归属来自 bind_runtime_context（空间/会话/回合），不读端视图。"""
    from src.tools.builtin.integration.outbound_file import OutboundFileRouter
    from src.tools.builtin.integration.send_file import SendFileTool

    sample = tmp_path / "a.txt"
    sample.write_text("hi", encoding="utf-8")
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()

    tool = SendFileTool(bridge=OutboundFileRouter(), workspace_root=ws_dir)
    invocation = tool.create_invocation({"path": str(sample)})
    invocation.bind_runtime_context(
        tool_call_id="call-1",
        session_id="sess-a",
        coara_id="c-a",
        origin_source="web",
        workspace_dir=str(ws_dir),
        turn_id="turn-a",
    )

    owner = invocation._owner()
    assert (owner.workspace_dir, owner.session_id, owner.turn_id) == (str(ws_dir), "sess-a", "turn-a")
    assert owner.complete is True
    # 未绑定时（无会话）归属不完整：调用方据此拒绝落带
    bare = tool.create_invocation({"path": str(sample)})
    assert bare._owner().workspace_dir == str(ws_dir)
    assert bare._owner().complete is False


@pytest.mark.asyncio
async def test_web_file_bridge_copies_and_pushes(tmp_path: Path) -> None:
    sample = tmp_path / "shot.png"
    sample.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    delivery = tmp_path / "outbound"
    registry = MagicMock()
    registry.has_active.return_value = True
    registry.send_to_active = AsyncMock(return_value=True)
    persisted: list[dict[str, Any]] = []

    bridge = WebFileBridge(
        registry=registry,
        delivery_dir=delivery,
        persist=lambda attachment, owner=None: persisted.append(attachment),
    )
    result = await bridge.send_file(sample, caption="截图")

    assert result["channel"] == "web"
    assert result["filename"] == "shot.png"
    file_id = result["file_id"]
    resolved = bridge.resolve_file(file_id)
    assert resolved is not None and resolved.is_file()
    payload = registry.send_to_active.await_args.args[0]
    assert payload["type"] == "file"
    assert payload["is_image"] is True
    assert payload["url"] == f"/api/outbound-files/{file_id}"
    assert len(persisted) == 1
    assert persisted[0]["file_id"] == file_id


@pytest.mark.asyncio
async def test_web_file_bridge_resolve_from_disk_without_memory(tmp_path: Path) -> None:
    delivery = tmp_path / "outbound"
    delivery.mkdir()
    file_id = "a" * 32
    on_disk = delivery / f"{file_id}.png"
    on_disk.write_bytes(b"\x89PNG")
    bridge = WebFileBridge(registry=MagicMock(), delivery_dir=delivery)
    # Empty in-memory index — must find via disk scan (hydrate / restart).
    resolved = bridge.resolve_file(file_id)
    assert resolved is not None
    assert resolved.name.startswith(file_id)
    assert resolved.read_bytes() == b"\x89PNG"


def test_outbound_files_persist_to_session_tape(tmp_path: Path) -> None:
    """Web 投递附件经 recorder 落 L1 assistant/files 事件，投影恢复为文件行。"""
    from src.session_log.conversation_projection import iter_conversation_rows
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_id="root",
        coara_name="coara",
        agent_kind="main",
    )
    recorder.record_files(
        files=[
            {
                "file_id": "b" * 32,
                "url": "/api/outbound-files/" + "b" * 32,
                "filename": "x.png",
                "mime": "image/png",
                "size": 12,
                "caption": "",
                "is_image": True,
                "is_video": False,
                "is_audio": False,
            }
        ],
        turn_id="t1",
        source="web",
    )
    rows = list(iter_conversation_rows(resolve_session_log_path(tmp_path), include_agent_kinds={"main"}))
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["source"] == "web"
    assert rows[0]["files"][0]["filename"] == "x.png"


@pytest.mark.asyncio
async def test_web_file_bridge_requires_active_client(tmp_path: Path) -> None:
    sample = tmp_path / "a.txt"
    sample.write_text("x", encoding="utf-8")
    registry = MagicMock()
    registry.has_active.return_value = False
    bridge = WebFileBridge(registry=registry, delivery_dir=tmp_path / "out")
    with pytest.raises(RuntimeError, match="Web 连接"):
        await bridge.send_file(sample)


@pytest.mark.asyncio
async def test_register_send_file_keeps_matrix_when_adding_web(tmp_path: Path) -> None:
    """Simulates Matrix wired first, then Web attaches — Matrix stays."""
    from src.tools.builtin.integration.send_file import SendFileTool

    class _Root:
        def __init__(self) -> None:
            self._tool_manager = MagicMock()
            self._tool_manager.tools = {}
            self._sessions: dict[str, Any] = {}
            self._outbound_file_router = None

        def register_tool(self, tool: Any, *, replace: bool = False) -> None:
            self._tool_manager.tools[tool.name] = tool

    root = _Root()
    matrix = MagicMock()
    router = ensure_outbound_file_router(root)
    router.set_matrix(matrix)
    register_send_file_on_root(root, workspace_root=tmp_path, router=router)

    web = MagicMock()
    router.set_web(web)
    register_send_file_on_root(root, workspace_root=tmp_path, router=router)

    tool = root._tool_manager.tools["send_file"]
    assert isinstance(tool, SendFileTool)
    assert tool._bridge.matrix is matrix
    assert tool._bridge.web is web


@pytest.mark.asyncio
async def test_send_file_tool_matrix_success_message(tmp_path: Path) -> None:
    from src.coara.turn_context import turn
    from src.tools.builtin.integration.send_file import SendFileTool

    sample = tmp_path / "doc.md"
    sample.write_text("# hi", encoding="utf-8")
    router = OutboundFileRouter()
    matrix = MagicMock()
    matrix.send_file = AsyncMock(return_value={"filename": "doc.md", "room_id": "!r:hs", "send_result": "ok"})
    router.set_matrix(matrix)
    tool = SendFileTool(bridge=router, workspace_root=tmp_path)
    inv = tool.create_invocation({"path": str(sample), "caption": "说明"})
    async with turn("matrix", channel_id="!r:hs"):
        result = await inv.execute()
    assert not result.is_error
    assert "手机端" in (result.content or "")
    matrix.send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_file_tool_rejected_on_no_remote_turn(tmp_path: Path) -> None:
    """无远端通道的回合（无端或 CLI）禁止 send_file。"""
    from src.coara.turn_context import turn
    from src.tools.builtin.integration.send_file import SendFileTool

    sample = tmp_path / "doc.md"
    sample.write_text("# hi", encoding="utf-8")
    router = OutboundFileRouter()
    matrix = MagicMock()
    matrix.send_file = AsyncMock(return_value={"filename": "doc.md"})
    router.set_matrix(matrix)
    tool = SendFileTool(bridge=router, workspace_root=tmp_path)
    inv = tool.create_invocation({"path": str(sample)})

    # 无端信息（默认无端回合）
    result = await inv.execute()
    assert result.is_error
    matrix.send_file.assert_not_awaited()

    # 显式 CLI 无端回合
    async with turn("cli-attached"):
        result = await inv.execute()
    assert result.is_error
    matrix.send_file.assert_not_awaited()
