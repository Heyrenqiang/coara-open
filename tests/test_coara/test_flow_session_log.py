"""Flow 第二主体：录像带 agent_kind=flow、独立索引、不污染主会话 session_state。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.types import Message, MessageRole
from src.session_log.store import read_events, resolve_session_log_path
from tests.helpers import make_test_coara


@pytest.mark.asyncio
async def test_flow_root_session_kind_and_isolated_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.coara.flow_root import create_flow_root
    from src.coara.workspace_state import (
        _flow_state_file_path,
        _state_file_path,
        load_flow_session_state,
        load_session_state,
    )

    # 隔离到 tmp coara_home，避免写进开发机默认 home
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))

    ws = tmp_path / "ws"
    ws.mkdir()
    fg = make_test_coara(ws)
    fg.workspace_dir = ws
    # 主会话先占住 session_state
    from src.coara.workspace_state import save_session_state

    main_sid = "main-session-id"
    save_session_state(ws, main_sid, coara_home=home)

    fake_root = SimpleNamespace(
        foreground_coara=fg,
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
        workspace_manager=SimpleNamespace(coara_home=home, vfs=None),
    )
    flow = await create_flow_root(fake_root)

    assert flow.is_flow_subject()
    assert flow._session_agent_kind == "flow"
    assert flow._session_log is not None
    assert flow._session_log._agent_kind == "flow"

    flow.message_history.append(Message(role=MessageRole.USER, content="建一个调研流"))
    flow.persist_session_to_disk()

    # 主索引未被覆盖
    sid, _ = load_session_state(ws, coara_home=home)
    assert sid == main_sid
    assert _state_file_path(ws, coara_home=home).exists()

    # Flow 独立索引
    flow_sid, _ = load_flow_session_state(ws, coara_home=home)
    assert flow_sid == flow.session_id
    assert _flow_state_file_path(ws, coara_home=home).exists()

    # 系统带能读到 flow 事件；工作空间带不再混入 flow 事件
    from src.session_log.store import workflow_session_log_path

    flow_tape = workflow_session_log_path("flow", coara_home=home)
    events = read_events(flow_tape, session_id=flow.session_id)
    assert events
    assert any(e.get("agent_kind") == "flow" for e in events)

    ws_tape = resolve_session_log_path(ws, coara_home=home)
    ws_events = read_events(ws_tape, session_id=flow.session_id) if ws_tape.exists() else []
    assert not ws_events


@pytest.mark.asyncio
async def test_flow_root_cold_start_restores_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.coara.flow_root import create_flow_root

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))

    ws = tmp_path / "ws"
    ws.mkdir()
    fg = make_test_coara(ws)
    fg.workspace_dir = ws
    fake_root = SimpleNamespace(
        foreground_coara=fg,
        event_bus=SimpleNamespace(publish=lambda *a, **k: None),
        workspace_manager=SimpleNamespace(coara_home=home, vfs=None),
    )

    first = await create_flow_root(fake_root)
    first.message_history.append(Message(role=MessageRole.USER, content="第一句构建指令"))
    first.message_history.append(Message(role=MessageRole.ASSISTANT, content="已登记节点"))
    first.persist_session_to_disk()
    saved_sid = first.session_id

    second = await create_flow_root(fake_root)
    assert second.session_id == saved_sid
    texts = [str(m.content) for m in second.message_history]
    assert any("第一句构建指令" in t for t in texts)
