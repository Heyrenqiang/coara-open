"""Tests for the attach thin client (src/cli/attach_client.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
import yaml

from src.cli import attach_client
from src.cli.attach_client import (
    AttachError,
    build_attach_url,
    build_chat_message,
    build_command_message,
    build_handshake_message,
    decode_message,
    encode_message,
    is_slash_command,
    load_attach_token,
    resolve_web_host_port,
    run_attach_command,
)
from src.ui.dashboard_tokens import load_or_create_dashboard_token


class TestMessageCodec:
    def test_handshake_message(self) -> None:
        assert build_handshake_message("my-app") == {"type": "attach", "workspace": "my-app"}

    def test_chat_message(self) -> None:
        assert build_chat_message("你好") == {"type": "chat", "text": "你好"}

    def test_command_message(self) -> None:
        assert build_command_message("/model deepseek") == {"type": "command", "text": "/model deepseek"}

    def test_is_slash_command(self) -> None:
        assert is_slash_command("/model deepseek") is True
        assert is_slash_command("/new") is True
        assert is_slash_command("普通对话") is False
        assert is_slash_command("不是/开头") is False

    def test_encode_decode_roundtrip(self) -> None:
        raw = encode_message({"type": "chat", "text": "你好 coara"})
        assert json.loads(raw) == {"type": "chat", "text": "你好 coara"}
        assert decode_message(raw)["text"] == "你好 coara"

    def test_decode_rejects_non_dict(self) -> None:
        with pytest.raises(AttachError):
            decode_message('["not", "a", "dict"]')

    def test_decode_rejects_invalid_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            decode_message("{oops")

    def test_attach_url(self) -> None:
        url = build_attach_url("127.0.0.1", 8080, "tok123")
        assert url == "ws://127.0.0.1:8080/ws/attach?token=tok123"


class TestHostPortResolution:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_WEB_PORT", raising=False)
        assert resolve_web_host_port() == ("127.0.0.1", 8080)

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COARA_WEB_PORT", "9123")
        assert resolve_web_host_port() == ("127.0.0.1", 9123)

    def test_invalid_port_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COARA_WEB_PORT", "not-a-number")
        assert resolve_web_host_port() == ("127.0.0.1", 8080)


class TestTokenResolution:
    def test_reads_home_level_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        token = load_attach_token(workspace)
        token_file = workspace / ".coara" / "system" / "dashboard_token"
        assert token_file.is_file()
        assert token == token_file.read_text(encoding="utf-8").strip()

    def test_matches_server_token_for_same_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        client_token = load_attach_token(workspace)
        server_token = load_or_create_dashboard_token(workspace, workspace / ".coara")
        assert client_token == server_token

    def test_respects_configured_coara_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COARA_HOME", raising=False)
        home = tmp_path / "shared-home"
        (home / "users" / "default").mkdir(parents=True)
        (home / "users" / "default" / "config.yaml").write_text(
            yaml.safe_dump({"coara_home": str(home)}), encoding="utf-8"
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.setenv("COARA_HOME", str(home))
        token = load_attach_token(workspace)
        assert (home / "system" / "dashboard_token").is_file()
        assert token == (home / "system" / "dashboard_token").read_text(encoding="utf-8").strip()


class FakeWS:
    def __init__(self, frames: list[dict], close_code: int | None = None) -> None:
        self.sent: list[str] = []
        self._frames = list(frames)
        self.close_code = close_code

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def receive(self) -> aiohttp.WSMessage:
        if self._frames:
            return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(self._frames.pop(0)), None)
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)


class FakeUI:
    """记录 UI 调用的测试替身（receive_loop 按帧类型驱动它）。"""

    def __init__(self, inputs: list[str] | None = None) -> None:
        self.calls: list[tuple] = []
        self.turn_active = False
        self._inputs = list(inputs or [])

    def print_chunk(self, text: str) -> None:
        self.calls.append(("chunk", text))

    def print_tool(self, text: str, ok: bool) -> None:
        self.calls.append(("tool", text, ok))

    def print_error(self, message: str, *, occupied: bool = False) -> None:
        self.calls.append(("error", message, occupied))

    def print_command_result(self, output: str) -> None:
        self.calls.append(("command_result", output))

    def begin_turn(self) -> None:
        self.turn_active = True
        self.calls.append(("begin",))

    def end_turn(self) -> None:
        self.turn_active = False
        self.calls.append(("end",))

    def build_session(self):
        return object()

    async def prompt(self, session) -> str | None:
        if self._inputs:
            return self._inputs.pop(0)
        return None


class TestReceiveLoop:
    async def test_frames_drive_ui_and_turn_end(self) -> None:
        import asyncio

        ws = FakeWS(
            [
                {"type": "turn_start"},
                {"type": "chunk", "text": "你好"},
                {"type": "chunk", "text": "，世界"},
                {"type": "tool", "text": "✓ read", "ok": True},
                {"type": "turn_end", "reason": "complete"},
            ]
        )
        ui = FakeUI()
        turn_done = asyncio.Event()
        task = asyncio.create_task(attach_client._receive_loop(ws, ui, turn_done))
        await asyncio.sleep(0.05)
        assert turn_done.is_set()
        task.cancel()
        assert ("begin",) in ui.calls
        assert ("chunk", "你好") in ui.calls
        assert ("chunk", "，世界") in ui.calls
        assert ("tool", "✓ read", True) in ui.calls
        assert ("end",) in ui.calls
        assert ui.turn_active is False

    async def test_close_code_4001_raises_auth(self) -> None:
        ws = FakeWS([], close_code=4001)
        turn_done = asyncio.Event()
        with pytest.raises(attach_client.AttachAuthError):
            await attach_client._receive_loop(ws, FakeUI(), turn_done)

    async def test_error_frame_marks_occupied(self) -> None:
        import asyncio

        ws = FakeWS(
            [
                {"type": "error", "message": "工作空间正被占用", "reason": "busy"},
                {"type": "turn_end", "reason": "error"},
            ]
        )
        ui = FakeUI()
        turn_done = asyncio.Event()
        task = asyncio.create_task(attach_client._receive_loop(ws, ui, turn_done))
        await asyncio.sleep(0.05)
        task.cancel()
        assert ("error", "工作空间正被占用", True) in ui.calls
        assert turn_done.is_set()


class TestInputLoop:
    async def test_sends_chat_and_waits_for_turn(self) -> None:
        import asyncio

        ws = FakeWS([])
        turn_done = asyncio.Event()
        ui = FakeUI(inputs=["你好", "exit"])

        async def mark_done() -> None:
            while not ws.sent:
                await asyncio.sleep(0.01)
            turn_done.set()

        waiter = asyncio.create_task(mark_done())
        await attach_client._input_loop(ws, ui, turn_done)
        await waiter
        assert json.loads(ws.sent[0]) == {"type": "chat", "text": "你好"}
        # 用户行不再二次回显（与主 CLI 一致，由 ptk 提交留屏）
        assert not any(c[0] == "user" for c in ui.calls)

    async def test_slash_goes_to_command_channel(self) -> None:
        ws = FakeWS([])
        turn_done = asyncio.Event()
        ui = FakeUI(inputs=["/model deepseek", "exit"])
        await attach_client._input_loop(ws, ui, turn_done)
        assert json.loads(ws.sent[0]) == {"type": "command", "text": "/model deepseek"}


class TestRunAttachCommand:
    def test_connect_failure_friendly_error(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        # Nothing is listening on this port → friendly message, no traceback.
        code = run_attach_command(
            "my-app",
            workspace_dir=tmp_path,
            host="127.0.0.1",
            port=1,
            token="tok",
        )
        assert code == 1
        out = capsys.readouterr().out
        assert "主进程未启动" in out

    def test_auth_failure_handshake_401(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        class FakeSession:
            def __init__(self, *args, **kwargs) -> None: ...
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False

            def ws_connect(self, url: str, **kwargs):
                raise aiohttp.WSServerHandshakeError(None, (), status=401, message="Unauthorized")

        monkeypatch.setattr(attach_client, "ClientSession", FakeSession)
        code = run_attach_command("ws", workspace_dir=tmp_path, host="127.0.0.1", port=8080, token="bad")
        assert code == 2
        assert "token" in capsys.readouterr().out
