"""attached_chat_runner：RootShim 数据层消费点的适配验证。

不驱动完整 prompt_toolkit 界面（os._exit 收尾无法直接测），验证召回骨架
消费的替身接口在 RootShim 上成立：主题读取、命令透传、pending_report、
快照计数、会话落盘 no-op、界面骨架 root 契约消费。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.cli.attached_chat_runner import (
    _read_local_cli_theme_config,
    handle_attached_chat_command,
)
from src.cli.root_shim import RootShim


class MockTransport:
    def __init__(self) -> None:
        self.handlers: list = []
        self.sent: list[dict] = []
        self.closed = False
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    def register_handler(self, handler) -> None:
        if handler not in self.handlers:
            self.handlers.append(handler)

    async def close(self) -> None:
        self.closed = True

    def inject(self, frame: dict) -> None:
        for handler in list(self.handlers):
            handler(frame)


def _attached_frame() -> dict:
    """服务端 _build_attach_attached_frame 的扁平快照帧。"""
    return {
        "type": "attached",
        "workspace": "main",
        "workspace_id": "ws-1",
        "session": "sess-1",
        "session_id": "sess-1",
        "workspace_dir": "D:/ws/main",
        "coara_id": "root-1",
        "agent_name": "Coara",
        "provider_name": "deepseek",
        "model_name": "deepseek-chat",
        "is_plan_mode": False,
        "tools_count": 20,
        "skills_count": 3,
        "active_name": "main",
        "foreground_session_id": "ws-1",
        "workspaces": [{"id": "ws-1", "name": "main", "path": "D:/ws/main"}],
        "usage": {},
        "context_window": 131072,
        "turn_source": "",
    }


def _make_shim() -> tuple[MockTransport, RootShim]:
    transport = MockTransport()
    shim = RootShim(transport)
    transport.inject(_attached_frame())
    return transport, shim


class TestLocalThemeConfig:
    def test_reads_cli_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".coara" / "users" / "default"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "cli:\n  theme: light\nother: 1\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "src.core.coara_home.resolve_bootstrap_coara_home", lambda: None
        )
        raw = _read_local_cli_theme_config(tmp_path)
        assert raw == {"cli": {"theme": "light"}}

    def test_missing_config_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "src.core.coara_home.resolve_bootstrap_coara_home", lambda: None
        )
        assert _read_local_cli_theme_config(tmp_path) == {}


class TestAttachedCommandPassthrough:
    @pytest.mark.asyncio
    async def test_command_sent_and_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport, shim = _make_shim()

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject(
                {
                    "type": "command_result",
                    "result": {"output": "ok", "action": "none", "data": {}, "exit_session": False},
                }
            )

        rendered: list = []
        monkeypatch.setattr("src.cli.commands._render_result", lambda result: rendered.append(result))

        task = asyncio.create_task(handle_attached_chat_command(shim, "/help", Path.cwd()))
        await reply()
        should_exit = await task

        assert should_exit is False
        sent = transport.sent[-1]
        assert sent["type"] == "command"
        assert sent["text"] == "/help"
        assert sent["request_id"]  # 客户端生成 uuid 随帧发，服务端回显精确派发
        assert len(rendered) == 1
        assert rendered[0].output == "ok"

    @pytest.mark.asyncio
    async def test_exit_session_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport, shim = _make_shim()

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject(
                {
                    "type": "command_result",
                    "result": {"output": "bye", "action": "none", "data": {}, "exit_session": True},
                }
            )

        monkeypatch.setattr("src.cli.commands._render_result", lambda result: None)

        task = asyncio.create_task(handle_attached_chat_command(shim, "/exit", Path.cwd()))
        await reply()
        assert await task is True


class TestPendingReportPassthrough:
    @pytest.mark.asyncio
    async def test_not_consumed_returns_none(self) -> None:
        transport, shim = _make_shim()

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject({"type": "pending_report_result", "consumed": False})

        task = asyncio.create_task(shim.try_consume_pending_report("普通对话"))
        await reply()
        result = await task

        assert result is None
        assert transport.sent[-1] == {"type": "pending_report", "text": "普通对话"}

    @pytest.mark.asyncio
    async def test_consumed_returns_command_result(self) -> None:
        transport, shim = _make_shim()

        async def reply() -> None:
            await asyncio.sleep(0)
            transport.inject(
                {
                    "type": "pending_report_result",
                    "consumed": True,
                    "result": {"output": "已提交反馈", "action": "none", "data": {}, "exit_session": False},
                }
            )

        task = asyncio.create_task(shim.try_consume_pending_report("界面卡死"))
        await reply()
        result = await task

        assert result is not None
        assert result.output == "已提交反馈"


class TestSkeletonContract:
    """界面骨架（spinner / display_controller / session / 主循环）消费的
    root 契约在 RootShim 上成立。"""

    def test_snapshot_counts_for_ready_banner(self) -> None:
        _, shim = _make_shim()
        # 就绪行：Tools/Skills 计数来自 attached 快照镜像。
        assert shim.identity.tools_count == 20
        assert shim.identity.skills_count == 3

    def test_persist_session_to_disk_noop(self) -> None:
        _, shim = _make_shim()
        # Windows 关窗清理（make_minimal_sync_cleanup）会调到它——客户端 no-op。
        assert shim.foreground_coara.persist_session_to_disk() is None
        session = shim._sessions["ws-1"]
        assert session.coara.persist_session_to_disk() is None

    def test_sync_workspace_manager_to_foreground(self) -> None:
        _, shim = _make_shim()
        assert shim.sync_workspace_manager_to_foreground() is True

    def test_spinner_bind_root(self) -> None:
        from src.cli.spinner import BackgroundSpinner

        _, shim = _make_shim()
        spinner = BackgroundSpinner()
        spinner.bind_root(shim)
        # bottom_toolbar 数据源（usage 快照 / has_active_turn / foreground_active_name）
        # 全部可用——spinner 装配不炸即契约成立。
        assert spinner._root is shim

    def test_display_controller_wire(self) -> None:
        from rich.console import Console

        from src.cli.display_controller import CliDisplayController
        from src.cli.spinner import BackgroundSpinner, SubagentSpinnerManager

        _, shim = _make_shim()
        display = CliDisplayController(
            root=shim,
            console=Console(),
            subagent_spinner=SubagentSpinnerManager(),
            background_spinner=BackgroundSpinner(),
        )
        subs = display.wire(shim.event_bus)
        assert subs  # display.wire 的订阅全部注册成功
        for sub in subs:
            sub.unsubscribe()

    def test_no_remote_sync_display(self) -> None:
        """CLI 各端完全独立零同步：attached_chat_runner 不挂 wire_remote_sync_display。

        web/手机端对话绝不镜像到 CLI（remote_sync_display 是老 CLI 主前端逻辑已移除）。
        """
        import inspect

        import src.cli.attached_chat_runner as m

        src = inspect.getsource(m)
        assert "wire_remote_sync_display(" not in src.replace("# 不挂 wire_remote_sync_display", "")
        assert not hasattr(m, "wire_remote_sync_display")

    def test_workspace_catalog_picker_rows(self) -> None:
        """/ws 菜单（slash_pickers.workspace_options_from_root）的数据契约：
        list_workspaces 返回 WorkspaceEntry 形状（id/name/path/summary/status）。"""
        from src.cli.slash_pickers import workspace_options_from_root

        _, shim = _make_shim()
        rows = workspace_options_from_root(shim)
        assert rows
        assert rows[0].insert == "/ws switch main"
        assert "当前" in rows[0].meta


class TestSigintWiring:
    def test_make_sigterm_exit_request_schedules_async_interrupt(self) -> None:
        """RootShim 的 interrupt_current_turn 是协程：SIGTERM 处理（同步上下文）
        调度为 fire-and-forget 任务，不炸也不丢 RuntimeWarning。"""
        from src.cli.session import _PromptExit
        from src.cli.shutdown_signals import make_sigterm_exit_request

        transport, shim = _make_shim()

        async def _run() -> None:
            transport.inject(
                {
                    "type": "event",
                    "event_type": "llm_request_start",
                    "coara_id": "root-1",
                    "coara_name": "Coara",
                    "message": "",
                    "payload": {"workspace_id": "ws-1", "source": "cli-attached"},
                }
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert shim.foreground_coara.has_active_turn() is True

            queue: asyncio.Queue = asyncio.Queue()
            request = make_sigterm_exit_request(shim, queue, asyncio.get_running_loop())
            request()
            # fire-and-forget interrupt 任务跑完 + 退出哨兵入队
            for _ in range(5):
                await asyncio.sleep(0)
            assert any(f.get("type") == "interrupt" for f in transport.sent)
            assert isinstance(queue.get_nowait(), _PromptExit)

        asyncio.run(_run())

    def test_make_minimal_sync_cleanup_safe_with_shim(self) -> None:
        """关窗最小清理：shim 替身的各步都是安全 no-op，不抛。"""
        from src.cli.shutdown_signals import make_minimal_sync_cleanup

        _, shim = _make_shim()
        cleanup = make_minimal_sync_cleanup(shim)
        cleanup()  # 不得抛异常


class TestDetachedTurnContract:
    def test_foreground_workspace_matcher_uses_shim_field(self) -> None:
        """detached 自愈（iter_while_foreground）的前台判定读 root._foreground_session_id。"""
        from src.coara.turn_detach import foreground_workspace_matcher

        _, shim = _make_shim()
        still = foreground_workspace_matcher(shim, shim._foreground_session_id)
        assert still() is True
        still_other = foreground_workspace_matcher(shim, "ws-other")
        assert still_other() is False

    def test_workspace_display_name_resolves(self) -> None:
        from src.coara.turn_detach import workspace_display_name

        _, shim = _make_shim()
        assert workspace_display_name(shim, "D:/ws/main") == "main"
        # 未登记路径回退目录名
        assert workspace_display_name(shim, "D:/nowhere/else") == "else"
