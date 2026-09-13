"""Tests for the simplified ws tool (list/add/remove/rename/switch)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.coara.root import RootCoara
from src.llm.provider import LLMProvider
from src.llm.registry import provider_registry
from src.tools.builtin.ws.ws import WsInvocation, WsTool, _retract_switch_ui_transcript
from src.workspace.catalog import format_workspace_catalog
from src.workspace.manager import WorkspaceManager


def test_retract_switch_ui_transcript_payload_carries_source() -> None:
    """P0-1：retract 事件 payload 必带 source（端门禁严格模式无 source 丢弃）。"""
    emitted: list[dict] = []

    coara = SimpleNamespace(
        _active_turn=SimpleNamespace(turn_id="t-1"),
        session_id="s-1",
        _active_turn_source="web",
        _session_log=None,
        _emit_trace=lambda et, msg, payload=None: emitted.append({"type": et, "payload": payload}),
    )
    _retract_switch_ui_transcript(coara)

    assert len(emitted) == 1
    payload = emitted[0]["payload"]
    assert emitted[0]["type"] == "chat_turn_retracted"
    assert payload["turn_id"] == "t-1"
    assert payload["session_id"] == "s-1"
    assert payload["source"] == "web"


@pytest.mark.asyncio
async def test_workspace_list_requires_root() -> None:
    inv = WsInvocation({"action": "list"}, None)
    result = await inv.execute()
    assert result.is_error
    assert "root" in str(result.content).lower()


@pytest.mark.asyncio
async def test_workspace_list_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(workspace, name="demo", summary="演示工作空间")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager

        def sync_workspace_manager_to_foreground(self) -> None:
            pass

    tool = WsTool(parent_coara=FakeRoot())
    inv = tool.create_invocation({"action": "list"})
    assert WsTool.requires_approval({"action": "list"}) is False
    result = await inv.execute()
    assert not result.is_error
    text = str(result.content)
    assert "demo" in text
    assert "演示工作空间" in text
    assert "路径" in text


@pytest.mark.asyncio
async def test_workspace_add_creates_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()

    class FakeRoot:
        workspace_manager = manager

    inv = WsInvocation(
        {
            "action": "add",
            "name": "demo",
            "summary": "测试工作空间",
        },
        FakeRoot(),
    )
    result = await inv.execute()
    assert not result.is_error, result.content

    workspace_path = coara_home / "workspaces" / "demo"
    assert workspace_path.is_dir()

    entry = manager.registry.resolve_name_or_id("demo")
    assert entry is not None
    assert entry.summary == "测试工作空间"


@pytest.mark.asyncio
async def test_workspace_add_custom_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    custom_path = tmp_path / "my-workspace"
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()

    class FakeRoot:
        workspace_manager = manager

    inv = WsInvocation(
        {
            "action": "add",
            "name": "my",
            "path": str(custom_path),
        },
        FakeRoot(),
    )
    result = await inv.execute()
    assert not result.is_error, result.content
    assert custom_path.is_dir()
    assert manager.registry.resolve_name_or_id("my") is not None


@pytest.mark.asyncio
async def test_workspace_use_switches_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry_a = manager.registry.ensure_workspace(workspace_a, name="a")
    manager.registry.ensure_workspace(workspace_b, name="b")
    manager.registry.save()
    manager._active_id = entry_a.id

    class FakeRoot:
        workspace_manager = manager
        foreground_coara = SimpleNamespace(_inside_turn=False, message_history=[])

        async def switch_workspace(self, name, **kwargs):
            return manager.switch(name)

    inv = WsInvocation({"action": "switch", "name": "b"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error, result.content
    assert manager.active_name == "b"
    assert "b" in str(result.content)


@pytest.mark.asyncio
async def test_workspace_rename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "android-app"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry = manager.registry.ensure_workspace(workspace, name="android-app")
    manager.registry.save()
    old_id = entry.id

    # 空间自治布局：收件箱在 <工作空间>/.coara/inbox，按路径而非登记名定位，
    # rename（只改登记名、不动磁盘）无需迁移，内容原样保留
    inbox = workspace / ".coara" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "msg.json").write_text("{}", encoding="utf-8")

    class FakeRoot:
        workspace_manager = manager

    inv = WsInvocation(
        {"action": "rename", "name": "android-app", "new_name": "coara-app-实验版"},
        FakeRoot(),
    )
    result = await inv.execute()
    assert not result.is_error, result.content
    assert manager.registry.resolve_name_or_id("android-app") is None
    renamed = manager.registry.resolve_name_or_id("coara-app-实验版")
    assert renamed is not None
    assert renamed.id == old_id
    assert renamed.resolved_path() == workspace.resolve()
    assert "重命名" in str(result.content)
    assert (inbox / "msg.json").is_file()
    # 新名字仍能解析到同一个收件箱
    from src.workspace.updates.store import WorkspaceUpdatesStore

    store = WorkspaceUpdatesStore(coara_home, registry=manager.registry)
    assert store._workspace_inbox_path("coara-app-实验版") == inbox.resolve()


@pytest.mark.asyncio
async def test_workspace_remove_blocks_active_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry = manager.registry.ensure_workspace(tmp_path, name="active")
    manager.registry.save()
    manager._active_id = entry.id

    class FakeRoot:
        workspace_manager = manager

    inv = WsInvocation({"action": "remove", "name": "active"}, FakeRoot())
    result = await inv.execute()
    assert result.is_error
    assert "active" in str(result.content).lower()


@pytest.mark.asyncio
async def test_workspace_remove_non_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "to-remove"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(workspace, name="to-remove")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager
        _sessions: dict = {}

    inv = WsInvocation({"action": "remove", "name": "to-remove"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error, result.content
    assert manager.registry.resolve_name_or_id("to-remove") is None
    assert workspace.is_dir()  # disk directory preserved


@pytest.mark.asyncio
async def test_workspace_remove_delete_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "to-purge"
    workspace.mkdir()
    (workspace / "note.txt").write_text("x", encoding="utf-8")
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(workspace, name="to-purge")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager
        _sessions: dict = {}

    inv = WsInvocation(
        {"action": "remove", "name": "to-purge", "delete_disk": True},
        FakeRoot(),
    )
    result = await inv.execute()
    assert not result.is_error, result.content
    assert manager.registry.resolve_name_or_id("to-purge") is None
    assert not workspace.exists()
    assert "删除磁盘" in result.content


@pytest.mark.asyncio
async def test_ws_remove_requires_ask() -> None:
    assert WsTool.requires_approval({"action": "remove", "name": "shop"}) is True


def _fake_session(*, busy: bool) -> object:
    """Minimal cached WorkspaceSession stand-in for remove guards."""

    class FakeCoara:
        _inside_turn = busy

        def has_active_turn(self) -> bool:
            return busy

        def is_turn_busy(self) -> bool:
            return busy

    class FakeSession:
        coara = FakeCoara()

    return FakeSession()


@pytest.mark.asyncio
async def test_workspace_remove_blocks_busy_cached_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "busy-ws"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry = manager.registry.ensure_workspace(workspace, name="busy-ws")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager
        _sessions = {entry.id: _fake_session(busy=True)}

    inv = WsInvocation({"action": "remove", "name": "busy-ws"}, FakeRoot())
    result = await inv.execute()
    assert result.is_error
    assert "正在进行" in str(result.content)
    # Registry untouched.
    assert manager.registry.resolve_name_or_id("busy-ws") is not None


@pytest.mark.asyncio
async def test_workspace_remove_delete_disk_blocks_cached_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "cached-ws"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry = manager.registry.ensure_workspace(workspace, name="cached-ws")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager
        _sessions = {entry.id: _fake_session(busy=False)}

    inv = WsInvocation(
        {"action": "remove", "name": "cached-ws", "delete_disk": True},
        FakeRoot(),
    )
    result = await inv.execute()
    assert result.is_error
    assert "缓存会话" in str(result.content)
    # Registry and disk both untouched.
    assert manager.registry.resolve_name_or_id("cached-ws") is not None
    assert workspace.is_dir()


@pytest.mark.asyncio
async def test_workspace_remove_allows_idle_cached_session_without_delete(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace = tmp_path / "idle-ws"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    entry = manager.registry.ensure_workspace(workspace, name="idle-ws")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager
        _sessions = {entry.id: _fake_session(busy=False)}

    inv = WsInvocation({"action": "remove", "name": "idle-ws"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error, result.content
    assert manager.registry.resolve_name_or_id("idle-ws") is None
    assert workspace.is_dir()


@pytest.mark.asyncio
async def test_ws_add_requires_no_ask() -> None:
    assert WsTool.requires_approval({"action": "add", "name": "shop"}) is False


def test_format_catalog_empty() -> None:
    coara_home = Path("/tmp/unused-home-for-catalog")
    manager = WorkspaceManager(Path.cwd(), coara_home=coara_home)
    manager.registry.document.workspaces.clear()
    text = format_workspace_catalog(manager)
    assert "已登记工作空间" in text
    assert "暂无" in text


@pytest.mark.asyncio
async def test_switch_workspace_updates_cli_view_without_chdir(tmp_path: Path, monkeypatch) -> None:
    """切空间只改 cli view / root.workspace_dir；不再 os.chdir（工具用 session.workspace_dir）。"""
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    class CwdTestProvider(LLMProvider):
        async def complete(self, *args, **kwargs):
            from src.llm.provider import LLMResponse

            return LLMResponse(content="")

        async def stream_complete(self, *args, **kwargs):
            from src.llm.provider import StreamChunk

            yield StreamChunk()

        def get_context_window(self, model: str | None = None) -> int:
            return 32_000

        def abort(self) -> None:
            pass

        async def close(self) -> None:
            pass

    provider_registry.register("cwd-test", CwdTestProvider(name="cwd-test", api_key="x", default_model="m"))

    root = RootCoara(workspace_dir=workspace_a, provider_name="cwd-test")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    root._cli_view_workspace_id = entry_a.id
    root._web_view_workspace_id = entry_a.id
    root._matrix_view_workspace_id = entry_a.id
    root.event_bus = getattr(root, "event_bus", None) or __import__(
        "src.coara.event_bus", fromlist=["EventBus"]
    ).EventBus()

    cwd_before = Path.cwd()
    try:
        success = await root.switch_workspace("b")
        assert success
        # 进程 cwd 不变；cli view / 宿主镜像路径指向 b
        assert Path.cwd() == cwd_before
        assert root.view_workspace_id("cli") == root.workspace_manager.registry.resolve_name_or_id("b").id
        assert root.foreground_coara is not root
        assert root.foreground_coara.workspace_dir == workspace_b
        assert root.foreground_coara.identity.workspace_dir == workspace_b
        assert root.workspace_dir == workspace_b

        success = await root.switch_workspace("a")
        assert success
        assert Path.cwd() == cwd_before
        assert root._foreground_session_id == entry_a.id
        assert root.view_workspace_id("cli") == entry_a.id
        assert root.foreground_coara.workspace_dir == workspace_a
        assert root.workspace_dir == workspace_a
        assert root.identity.workspace_dir == workspace_a
        # web/matrix view 未被 cli 切换拖走（启动时已 pin）
        assert root.view_workspace_id("web") == entry_a.id
        assert root.view_workspace_id("matrix") == entry_a.id
    finally:
        pass


@pytest.mark.asyncio
async def test_switch_workspace_preserves_message_history(tmp_path: Path, monkeypatch) -> None:
    """每个工作空间的历史由各自 session 实例持有：切走不丢失，切回即恢复。"""
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    class CwdTestProvider(LLMProvider):
        async def complete(self, *args, **kwargs):
            from src.llm.provider import LLMResponse

            return LLMResponse(content="")

        async def stream_complete(self, *args, **kwargs):
            from src.llm.provider import StreamChunk

            yield StreamChunk()

        def get_context_window(self, model: str | None = None) -> int:
            return 32_000

        def abort(self) -> None:
            pass

        async def close(self) -> None:
            pass

    provider_registry.register("state-test", CwdTestProvider(name="state-test", api_key="x", default_model="m"))

    root = RootCoara(workspace_dir=workspace_a, provider_name="state-test")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id

    # Seed workspace A with a message before switching away.
    from src.core.types import Message, MessageRole

    seed = Message(role=MessageRole.USER, content="hello from workspace A")
    root.foreground_coara.message_history.append(seed)
    fg_a = root.foreground_coara

    original_cwd = Path.cwd()
    try:
        os.chdir(workspace_a)
        # A -> B：b 是全新 WorkspaceSession，历史独立；A 的历史留在 A 的 session。
        await root.switch_workspace("b")
        fg_b = root.foreground_coara
        assert fg_b is not root
        assert fg_b is not fg_a
        assert fg_b.workspace_dir == workspace_b
        # 新 session 尚未开始首个 turn（env seed 在首个 turn 才惰性注入），历史为空
        assert fg_b.message_history == []
        # A session 的 message_history 不被切换触碰
        assert any(msg.content == "hello from workspace A" for msg in fg_a.message_history)

        # B -> A：切回对等 session，A 的历史原样恢复。
        await root.switch_workspace("a")
        assert root.foreground_coara is fg_a
        assert len(root.foreground_coara.message_history) == 1
        assert root.foreground_coara.message_history[0].content == "hello from workspace A"

        # 再切到 B：复用内存中缓存的 WorkspaceSession（同一实例）。
        await root.switch_workspace("b")
        assert root.foreground_coara is fg_b
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_switch_workspace_during_active_turn_forces_fresh_session(tmp_path: Path, monkeypatch) -> None:
    """回合中经 ws(switch) 切换：抛出 CoaraRunCancelledError 打断当前回合，上下文切到新工作空间。"""
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    class InterruptTestProvider(LLMProvider):
        async def complete(self, *args, **kwargs):
            from src.llm.provider import LLMResponse

            return LLMResponse(content="")

        async def stream_complete(self, *args, **kwargs):
            from src.llm.provider import StreamChunk

            yield StreamChunk()

        def get_context_window(self, model: str | None = None) -> int:
            return 32_000

        def abort(self) -> None:
            pass

        async def close(self) -> None:
            pass

    provider_registry.register(
        "interrupt-test",
        InterruptTestProvider(name="interrupt-test", api_key="x", default_model="m"),
    )

    root = RootCoara(workspace_dir=workspace_a, provider_name="interrupt-test")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    fg_a = root.foreground_coara

    # Simulate workspace A's ongoing session: earlier history plus the in-flight
    # switch tail (user input + assistant ws(switch) tool call).
    from src.core.types import Message, MessageRole, ToolCall

    fg_a.message_history.append(Message(role=MessageRole.USER, content="old context in A"))
    fg_a.message_history.append(Message(role=MessageRole.USER, content="切换到 b"))
    fg_a.message_history.append(
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc-1", name="ws", arguments={"action": "switch", "name": "b"})],
        )
    )

    # Fake an active turn on the source session: ws(switch) detects _inside_turn and
    # interrupts via CoaraRunCancelledError instead of returning a ToolResult.
    fg_a._inside_turn = True

    original_cwd = Path.cwd()
    try:
        os.chdir(workspace_a)
        tool = WsTool(parent_coara=root)
        inv = tool.create_invocation({"action": "switch", "name": "b"})

        from src.coara.turn_completion import CoaraRunCancelledError

        with pytest.raises(CoaraRunCancelledError) as excinfo:
            await inv.execute()
        # reason 带目标工作空间名，orchestrator 据此产出切换通知
        assert excinfo.value.reason == "switch_workspace:b"

        # 不再 chdir；cli view / registry active 指向 b
        assert root.workspace_manager.active_name == "b"
        assert root.view_workspace_id("cli") == root.workspace_manager.registry.resolve_name_or_id("b").id
        # 前台切到 b 的全新 WorkspaceSession
        fg_b = root.foreground_coara
        assert fg_b is not root
        assert fg_b is not fg_a
        assert fg_b.workspace_dir == workspace_b
        assert all(msg.content != "old context in A" for msg in fg_b.message_history)
        # 源 session（A）的切换尾巴被剥离，更早的历史保留
        assert all(msg.content != "切换到 b" for msg in fg_a.message_history)
        assert all(not msg.tool_calls for msg in fg_a.message_history)
        assert any(msg.content == "old context in A" for msg in fg_a.message_history)
    finally:
        fg_a._inside_turn = False
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_switch_workspace_refreshes_environment_seed(tmp_path: Path, monkeypatch) -> None:
    """切换工作空间后，目标 session 首个 turn 注入的环境种子必须反映新工作目录，源 session 种子不受影响。"""
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    class SeedTestProvider(LLMProvider):
        async def complete(self, *args, **kwargs):
            from src.llm.provider import LLMResponse

            return LLMResponse(content="")

        async def stream_complete(self, *args, **kwargs):
            from src.llm.provider import StreamChunk

            yield StreamChunk()

        def get_context_window(self, model: str | None = None) -> int:
            return 32_000

        def abort(self) -> None:
            pass

        async def close(self) -> None:
            pass

    provider_registry.register("seed-test", SeedTestProvider(name="seed-test", api_key="x", default_model="m"))

    root = RootCoara(workspace_dir=workspace_a, provider_name="seed-test")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    fg_a = root.foreground_coara

    # Simulate the environment seed injected at session start in workspace A.
    from src.coara.injections.environment_injector import build_environment_seed_messages
    from src.core.types import MessageRole

    seed = build_environment_seed_messages(workspace_a)[0]
    fg_a.message_history.append(seed)
    assert str(workspace_a) in fg_a.message_history[0].content

    original_cwd = Path.cwd()
    try:
        os.chdir(workspace_a)
        await root.switch_workspace("b")
        fg_b = root.foreground_coara
        assert fg_b is not root
        assert fg_b is not fg_a
        # 新架构不做原地刷新：目标 session 切换时历史为空，
        # env seed 在该 session 的首个 turn 由 inject_environment_seed 惰性注入
        assert fg_b.message_history == []

        from src.coara.turn_loop.user_turn_injectors import UserTurnContext, inject_environment_seed

        ctx = UserTurnContext(content="你好")
        await inject_environment_seed(fg_b, ctx)
        # The seed messages now reflect workspace B（prefix 模块：环境上下文 +
        # 工作空间概况各一条；旧断言假定单条已过期）。环境上下文条须指向 B；
        # 概况条在 ws.md 未生成时是占位符（不含路径），只校验不含 A。
        assert len(fg_b.message_history) == 2
        assert all(m.role == MessageRole.USER for m in fg_b.message_history)
        env_seed = fg_b.message_history[0].content
        assert str(workspace_b) in env_seed
        assert all(str(workspace_a) not in m.content for m in fg_b.message_history)
        # 幂等：同一会话不会重复注入种子
        await inject_environment_seed(fg_b, ctx)
        assert len(fg_b.message_history) == 2
        # 源 session（A）的种子原样保留，不被切换触碰
        assert str(workspace_a) in fg_a.message_history[0].content
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_ws_list_not_served_from_stale_tool_cache(tmp_path: Path, monkeypatch) -> None:
    """ws(list) must reflect the current workspace, not a cached result from another."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )

    coara_home = tmp_path / "home"
    nx = tmp_path / "nx"
    pora = tmp_path / "pora"
    nx.mkdir()
    pora.mkdir()

    manager = WorkspaceManager(nx, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(nx, name="nx")
    manager.registry.ensure_workspace(pora, name="pora")
    manager.registry.save()
    manager.switch(manager.registry.resolve_name_or_id("nx").id)

    from src.core.tool_base import ToolResult
    from src.tools.cache import tool_cache

    stale = ToolResult.success(format_workspace_catalog(manager, active_name="nx"))
    tool_cache.set("ws", {"action": "list"}, stale)

    root = SimpleNamespace(
        workspace_manager=manager,
        foreground_coara=SimpleNamespace(workspace_dir=pora),
    )
    from src.coara.root import RootCoara

    root.sync_workspace_manager_to_foreground = RootCoara.sync_workspace_manager_to_foreground.__get__(root, RootCoara)

    tool = WsTool(parent_coara=root)
    inv = tool.create_invocation({"action": "list"})
    result = await inv.execute()
    assert not result.is_error
    text = str(result.content)
    assert "pora **(当前)**" in text
    assert "nx **(当前)**" not in text


def test_ws_tool_is_not_globally_cacheable() -> None:
    from src.agent.executor import _CACHEABLE_TOOL_KINDS
    from src.core.tool_base import ToolKind

    assert WsTool().kind == ToolKind.OTHER
    assert WsTool.should_defer is True
    assert (WsTool.summary or "").strip()
    # READ is excluded: read.py owns an mtime-aware cache; an executor-layer
    # cache would serve stale contents after external file edits.
    assert ToolKind.READ not in _CACHEABLE_TOOL_KINDS
    assert ToolKind.OTHER not in _CACHEABLE_TOOL_KINDS


@pytest.mark.asyncio
async def test_workspace_kind_query_set_and_validate(tmp_path: Path) -> None:
    """kind action：查询当前性质、设置 code、非法值报错、未找到报错。"""
    coara_home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager = WorkspaceManager(tmp_path, coara_home=coara_home)
    await manager.initialize()
    manager.registry.ensure_workspace(workspace, name="demo")
    manager.registry.save()

    class FakeRoot:
        workspace_manager = manager

    # 查询（省略 kind 参数）：默认 managed
    inv = WsInvocation({"action": "kind", "name": "demo"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error, result.content
    assert "managed" in str(result.content)

    # 设置为 code
    inv = WsInvocation({"action": "kind", "name": "demo", "kind": "code"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error, result.content
    entry = manager.registry.resolve_name_or_id("demo")
    assert entry is not None
    assert entry.kind.value == "code"

    # 幂等：已设 code 再设 code 提示未改动
    inv = WsInvocation({"action": "kind", "name": "demo", "kind": "code"}, FakeRoot())
    result = await inv.execute()
    assert not result.is_error
    assert "未改动" in str(result.content)

    # 非法值报错
    inv = WsInvocation({"action": "kind", "name": "demo", "kind": "bogus"}, FakeRoot())
    result = await inv.execute()
    assert result.is_error

    # 未找到
    inv = WsInvocation({"action": "kind", "name": "nope"}, FakeRoot())
    result = await inv.execute()
    assert result.is_error
