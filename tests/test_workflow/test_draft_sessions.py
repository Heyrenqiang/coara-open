"""每草案一个构建会话（section）：注册表 + 切换语义的行为测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.types import Message, MessageRole
from src.workflow import draft_sessions
from src.workflow.draft_store import WorkflowDraftStore

_WDL_A = "name: 甲流\nnodes:\n  收:\n    task: 收集\nedges: []\n"
_WDL_B = "name: 乙流\nnodes:\n  写:\n    task: 写作\nedges: []\n"


def test_registry_bind_mark_unbind(tmp_path: Path):
    home = tmp_path / "home"
    assert draft_sessions.session_for("draft-x", home) is None
    draft_sessions.bind_session("draft-x", "sid-1", home)
    assert draft_sessions.session_for("draft-x", home) == "sid-1"
    draft_sessions.mark_active("draft-x", home)
    assert draft_sessions.last_active(home) == "draft-x"
    draft_sessions.unbind("draft-x", home)
    assert draft_sessions.session_for("draft-x", home) is None
    assert draft_sessions.last_active(home) is None


def test_draft_delete_unbinds_session(tmp_path: Path):
    home = tmp_path / "home"
    store = WorkflowDraftStore(home)
    draft = store.save(_WDL_A, source_subagent="root")
    draft_sessions.bind_session(draft.draft_id, "sid-1", home)
    draft_sessions.mark_active(draft.draft_id, home)
    assert store.delete(draft.draft_id)
    assert draft_sessions.session_for(draft.draft_id, home) is None
    assert draft_sessions.last_active(home) is None


def _flow_coara(ws: Path, home: Path, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.core.config import config_manager
    from src.core.types import CoaraPersona
    from tests.helpers import FakeProvider

    # 录像带/索引落点必须钉在测试 home：config 可能已被其他测试加载成真实 home
    monkeypatch.setattr(config_manager, "_config", None)
    coara = CoaraBase(
        name="flow-root",
        persona=CoaraPersona(name="flow-root", role="builder"),
        workspace_dir=ws,
        provider=FakeProvider([]),
        user_facing=True,
        is_owner_context=True,
        session_agent_kind="flow",
    )
    coara.workspace_manager = SimpleNamespace(coara_home=home, vfs=None)
    coara._rebuild_session_log()
    return coara


def test_switch_between_draft_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.coara.flow_root import switch_flow_draft_session

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    store = WorkflowDraftStore(home)
    draft_a = store.save(_WDL_A, source_subagent="root")
    draft_b = store.save(_WDL_B, source_subagent="root")

    coara = _flow_coara(ws, home, monkeypatch)

    # 切到 A：开新会话并绑定
    assert switch_flow_draft_session(coara, draft_a.draft_id, coara_home=home)
    sid_a = coara.session_id
    assert draft_sessions.session_for(draft_a.draft_id, home) == sid_a
    assert draft_sessions.last_active(home) == draft_a.draft_id

    # 在 A 里聊一轮并落盘
    coara.message_history.append(Message(role=MessageRole.USER, content="给甲流加个节点"))
    coara.persist_session_to_disk()

    # 切到 B：全新空会话；A 的历史不串味
    assert switch_flow_draft_session(coara, draft_b.draft_id, coara_home=home)
    assert coara.session_id != sid_a
    assert not any("甲流" in str(m.content) for m in coara.message_history)

    # 切回 A：恢复 A 的会话（同 session_id 同历史）
    assert switch_flow_draft_session(coara, draft_a.draft_id, coara_home=home)
    assert coara.session_id == sid_a
    assert any("给甲流加个节点" in str(m.content) for m in coara.message_history)

    # 已在 A：no-op
    assert not switch_flow_draft_session(coara, draft_a.draft_id, coara_home=home)


def test_switch_skipped_during_active_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.coara.flow_root import switch_flow_draft_session

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    store = WorkflowDraftStore(home)
    draft_a = store.save(_WDL_A, source_subagent="root")
    draft_b = store.save(_WDL_B, source_subagent="root")

    coara = _flow_coara(ws, home, monkeypatch)
    assert switch_flow_draft_session(coara, draft_a.draft_id, coara_home=home)
    sid_a = coara.session_id
    monkeypatch.setattr(coara, "has_active_turn", lambda: True)

    assert not switch_flow_draft_session(coara, draft_b.draft_id, coara_home=home)
    assert coara.session_id == sid_a
    assert draft_sessions.session_for(draft_b.draft_id, home) is None
