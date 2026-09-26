"""CLI 回合中 web 跟话：实时能看见的内容必须落进 web_views，刷新 hydrate 还能看见。

多端铁律：段切到 web 后正文走 EndRegistry → followup TurnStream（落带+广播）。
hydrate 只读 conversation.jsonl——广播成功而落带失败 = 关页再开内容消失。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.coara.end_registry import EndRegistry
from src.core.types import ContinuationInput
from src.ui.turn_stream import TurnStream
from src.ui.view_recorder import shared_view_store
from src.ui.web_server import WebServer
from src.ui.web_views import WebViewStore, resolve_web_view_path


def _permit_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.llm.model_catalog._enabled_provider_names", lambda _mgr: ["fake"])
    monkeypatch.setattr(
        "src.coara.commands.report.try_consume_pending_report_async",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_cli_active_web_followup_persists_user_and_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 在飞时 web 跟话：user_message + 后续 chunk 必须进 tape，flush 后 hydrate 可读。"""
    _permit_providers(monkeypatch)

    home = tmp_path / "home"
    home.mkdir()
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    sess = "sess-cli-web"
    turn_id = "turn-cli-1"

    class _Turn:
        pass

    active = _Turn()
    active.turn_id = turn_id

    class _FakeCoara:
        session_id = sess
        workspace_dir = str(ws_dir)
        _active_turn = active

        def has_active_turn(self) -> bool:
            return True

        def set_deferred_remote_ctx(self, *args, **kwargs) -> None:
            pass

        def submit_continuation_input(
            self, text: str, image_blocks=None, *, source: str = "", client_msg_id: str = ""
        ) -> None:
            pass

        def drain_continuation_inputs(self) -> list[ContinuationInput]:
            return []

    fake = _FakeCoara()
    registry = EndRegistry()
    # 模拟 CLI attach 已占用发起端通道（persist=None）——跟话不得把正文送回这条
    cli_stream = TurnStream(turn_id, "cli-attached", "root", server=MagicMock(), channel_id="cli-conn")
    cli_hits: list[dict] = []

    def _cli_sender(frame: dict) -> None:
        cli_hits.append(frame)

    registry.register("cli-attached", _cli_sender, sess)

    root = SimpleNamespace(
        foreground_coara=fake,
        end_registry=registry,
        _foreground_session_id=sess,
        web_view_workspace_id=None,
        event_bus=SimpleNamespace(subscribe=lambda **kwargs: SimpleNamespace()),
    )

    server = WebServer.__new__(WebServer)
    server.root = root
    server.interaction_channel = SimpleNamespace()
    server._turn_tails = {}
    server._module_roots = {}
    server._module_roots_lock = asyncio.Lock()
    server._web_followup_view_turns = set()
    server._turns[turn_id] = cli_stream
    server.workspace_dir = ws_dir
    server.coara_home = home
    server._view_store = shared_view_store()
    server.registry = SimpleNamespace(
        has_active=lambda: True,
        send_to_active=AsyncMock(return_value=True),
        send_to_active_nowait=lambda _msg: None,
    )
    server.attach_registry = SimpleNamespace(send_to_nowait=lambda *_a, **_k: None)
    server._view_coara = lambda: fake  # type: ignore[method-assign]

    ws = SimpleNamespace(send_str=AsyncMock())
    await server._handle_chat(
        {"text": "web跟话一句", "client_msg_id": "cmid-follow-1"},
        ws,
        "web-conn-1",
        subject="root",
    )

    # EndRegistry 上 web 通道必须已登记，且指向 followup 流
    web_sender = registry.sender_for("web", sess)
    assert web_sender is not None

    followups = [
        s
        for s in server._turns.values()
        if str(getattr(getattr(s, "route", None), "channel_id", "") or "") == "followup-web"
    ]
    assert len(followups) == 1
    follow = followups[0]
    # 落带不再靠端注入回调：_persist 为 None ⇒ 缺省走内核录制器。落带确实发生
    # 与否由下面读视图文件断言（不认「有没有回调」）。
    assert follow._persist is None

    # 段切到 web 后的正文：经 EndRegistry 投递（与内核 _route_chunk_to_current_end 同路）
    outcome = registry.deliver(
        "web",
        sess,
        {
            "kind": "chunk",
            "text": "跟话后的答复正文",
            "session_id": sess,
            "workspace_dir": str(ws_dir),
            "turn_id": turn_id,
        },
    )
    assert outcome.hit is True
    # 微批窗口：强制 flush 落盘
    follow._flush_pending()

    path = resolve_web_view_path(ws_dir, coara_home=home, subject="root", session_id=sess)
    assert server._view_store.flush(timeout=2.0) is True

    frames = WebViewStore.iter_frames(path)
    kinds = [f.get("kind") for f in frames]
    assert "user_message" in kinds
    assert "chunk" in kinds
    user = next(f for f in frames if f.get("kind") == "user_message")
    assert (user.get("payload") or {}).get("content") == "web跟话一句"
    # P0-B：跟话落带应带 client_msg_id，刷新后能认领乐观气泡
    assert (user.get("payload") or {}).get("client_msg_id") == "cmid-follow-1"
    chunk = next(f for f in frames if f.get("kind") == "chunk")
    assert (chunk.get("payload") or {}).get("text") == "跟话后的答复正文"
    assert int(chunk.get("view_seq") or 0) > 0

    messages, _total, latest = WebViewStore.build_messages(path, limit=50, merge_chunks=False)
    texts = [m.get("text") or m.get("content") or "" for m in messages]
    assert any("web跟话一句" in t for t in texts)
    assert any("跟话后的答复正文" in t for t in texts)
    assert latest > 0
    assert cli_hits == []  # 不得串回 cli-attached
