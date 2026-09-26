"""工作流系统级化专项测试：系统根解析、flow_name 绑定、stale 重建。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workflow.draft_store import WorkflowDraftStore
from src.workflow.paths import (
    workflow_assets_root,
    workflow_drafts_dir,
)
from tests.workflow_wdl_fixtures import minimal_two_step_wdl


def test_workflow_root_is_system_level() -> None:
    """系统级存储：默认解析落在隔离 home 的 users/default/workflows。"""
    root = workflow_assets_root()
    assert root.name == "workflows"
    assert root.parent.name == "default"
    assert workflow_drafts_dir().parent == root


def test_workflow_root_honors_explicit_coara_home(tmp_path: Path) -> None:
    home = tmp_path / "explicit-home"
    root = workflow_assets_root(coara_home=home)
    assert root == home / "users" / "default" / "workflows"
    assert workflow_drafts_dir(coara_home=home).parent == root


def test_draft_store_flow_name_roundtrip(tmp_path: Path) -> None:
    store = WorkflowDraftStore(coara_home=tmp_path)
    draft = store.save(minimal_two_step_wdl(name="demo"), flow_name="调研")
    assert draft.flow_name == "调研"
    loaded = store.get(draft.draft_id)
    assert loaded is not None and loaded.flow_name == "调研"

    # 覆盖保存：不显式给 flow_name 时保留原绑定
    again = store.save(minimal_two_step_wdl(name="demo"), draft_id=draft.draft_id)
    assert again.flow_name == "调研"

    # find_by_name：绑定 flow 名优先，投影 name 兜底
    assert store.find_by_name("调研").draft_id == draft.draft_id
    assert store.find_by_name("demo").draft_id == draft.draft_id
    assert store.find_by_name("不存在") is None


def test_flow_coordinator_binding_and_stale_rebuild() -> None:
    from src.coara.flow_coordinator import FlowCoordinator

    coord = FlowCoordinator()
    coord.bind_draft("调研", "draft-bbbb2222")
    assert coord.draft_id_for("调研") == "draft-bbbb2222"
    assert coord.flow_for_draft("draft-bbbb2222") == "调研"

    coord.mark_stale("调研")
    assert coord.is_stale("调研")

    name = coord.rebuild_from_projection(None, flow="调研", wdl=minimal_two_step_wdl(name="调研"))
    assert name == "调研"
    assert not coord.is_stale("调研")
    # 重建后绑定仍在，图结构来自投影
    assert coord.draft_id_for("调研") == "draft-bbbb2222"
    snap = coord.snapshot("调研")
    assert snap is not None and len(snap["nodes"]) == 2

    coord.unbind_draft("draft-bbbb2222")
    assert coord.draft_id_for("调研") is None


@pytest.mark.asyncio
async def test_orchestrator_spawn_writes_through_to_draft(tmp_path: Path) -> None:
    """编排写穿：spawn 后绑定草案已落盘（系统草案库），flow ↔ draft 绑定建立。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)

    class _FakeCoord:
        def __init__(self) -> None:
            self._draft: str | None = None

        def get_flow(self, flow):
            return object()  # 非新 flow：不触发 open_editor

        async def spawn_node(self, parent, **kwargs) -> str:
            return f"节点 {kwargs['node_id']} 已注册"

        def export_wdl(self, flow: str) -> str:
            return minimal_two_step_wdl(name=flow)

        def draft_id_for(self, flow: str):
            return self._draft

        def bind_draft(self, flow: str, draft_id: str) -> None:
            self._draft = draft_id

    fake = _FakeCoord()
    parent._flow_coordinator = fake

    result = await OrchestratorToolInvocation(
        {
            "action": "spawn",
            "description": "节点A",
            "prompt": "做任务A",
            "flow": "demo-wt",
            "node_id": "a",
        },
        parent,
    ).execute()

    assert result.is_error is False
    assert fake._draft is not None and fake._draft.startswith("draft-")
    store = WorkflowDraftStore()  # 系统根（隔离 home）
    saved = store.get(fake._draft)
    assert saved is not None
    assert saved.flow_name == "demo-wt"
    assert "nodes" in saved.wdl


@pytest.mark.asyncio
async def test_orchestrator_save_reuses_bound_draft(tmp_path: Path) -> None:
    """save(flow=…) 幂等：复用写穿绑定的 draft_id 覆盖，不产生重复草案。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)

    class _FakeCoord:
        def __init__(self) -> None:
            self._draft = "draft-cccc3333"

        def export_wdl(self, flow: str) -> str:
            return minimal_two_step_wdl(name=flow)

        def draft_id_for(self, flow: str):
            return self._draft

        def bind_draft(self, flow: str, draft_id: str) -> None:
            self._draft = draft_id

    fake = _FakeCoord()
    parent._flow_coordinator = fake

    # 预置绑定草案
    store = WorkflowDraftStore()
    store.save(minimal_two_step_wdl(name="demo-save"), draft_id="draft-cccc3333", flow_name="demo-save")

    result = await OrchestratorToolInvocation({"action": "save", "flow": "demo-save"}, parent).execute()
    assert result.is_error is False
    assert result.metadata["draft_id"] == "draft-cccc3333"
    # 仍然只有这一份草案
    assert len(store.list_drafts()) == 1


def test_workflow_session_tape_paths(tmp_path: Path) -> None:
    """录像带跟主体：flow/engine 主体各有系统带，路径在工作流系统目录。"""
    from src.session_log.store import workflow_session_log_path

    flow_tape = workflow_session_log_path("flow", coara_home=tmp_path)
    engine_tape = workflow_session_log_path("engine", coara_home=tmp_path)
    root = tmp_path / "users" / "default" / "workflows"
    assert flow_tape == root / "session_events.jsonl"
    assert engine_tape == root / "engine_session_events.jsonl"


def test_recorder_log_path_override(tmp_path: Path) -> None:
    """recorder 显式 log_path 覆盖工作空间解析（主体带路由的实现基础）。"""
    from src.session_log.recorder import SessionLogRecorder

    tape = tmp_path / "custom" / "tape.jsonl"
    rec = SessionLogRecorder(workspace_dir=tmp_path / "ws", session_id="s1", log_path=tape)
    assert rec._ensure_path() == tape


@pytest.mark.asyncio
async def test_orchestrator_rebases_onto_external_edit(tmp_path: Path) -> None:
    """共同编辑：草案被外部（画布）推进后，编排动作先 rebase 再应用——
    对方加进草案的节点不会被会话的全文写穿静默抹掉。"""
    from src.tools.builtin.orchestrator.orchestrator import OrchestratorToolInvocation
    from tests.helpers import make_test_coara

    parent = make_test_coara(tmp_path)
    store = WorkflowDraftStore()
    first = store.save(minimal_two_step_wdl(name="共同编辑"), draft_id="draft-reb111", flow_name="共同编辑")

    class _RebaseCoord:
        def __init__(self) -> None:
            self.rebuilt: list[str] = []
            self._sync = first.updated_at

        def is_stale(self, flow) -> bool:
            return False

        def draft_id_for(self, flow):
            return "draft-reb111"

        def draft_sync(self, flow):
            return self._sync

        def note_draft_sync(self, flow, ts) -> None:
            self._sync = ts

        def rebuild_from_projection(self, parent, *, flow, wdl) -> str:
            self.rebuilt.append(wdl)
            return flow

    fake = _RebaseCoord()
    parent._flow_coordinator = fake

    inv = OrchestratorToolInvocation({"action": "edge", "flow": "共同编辑", "frm": "a", "to": "b"}, parent)
    # 无外部修改：不 rebase
    await inv._rebase_before_mutation()
    assert fake.rebuilt == []

    # 画布保存了新版本（updated_at 推进）→ 下次编排动作前先 rebase
    store.save(minimal_two_step_wdl(name="共同编辑"), draft_id="draft-reb111")
    await inv._rebase_before_mutation()
    assert len(fake.rebuilt) == 1
    # rebase 后同步点已对齐，再次调用不重复重建
    await inv._rebase_before_mutation()
    assert len(fake.rebuilt) == 1


@pytest.mark.asyncio
async def test_draft_put_conflict_returns_409(tmp_path: Path, monkeypatch) -> None:
    """画布自动保存带过期基线 → 409（不静默覆盖会话写穿的成果）。"""
    from src.ui.dashboard_handlers import DashboardRestHandlers

    handlers = DashboardRestHandlers.__new__(DashboardRestHandlers)
    handlers.workspace_dir = tmp_path
    handlers.coara_home = None
    monkeypatch.setattr(handlers, "_check_token", lambda _request: None)

    store = WorkflowDraftStore()
    saved = store.save(minimal_two_step_wdl(name="冲突检测"), draft_id="draft-cf123456")

    class _MatchInfo:
        def get(self, key: str, default: str = "") -> str:
            return {"draft_id": "draft-cf123456"}.get(key, default)

    class _Request:
        def __init__(self, body: dict) -> None:
            self.match_info = _MatchInfo()
            self._body = body

        async def json(self) -> dict:
            return self._body

    # 基线一致：正常保存
    ok = await handlers.handle_api_workflow_draft_put(
        _Request({"wdl": minimal_two_step_wdl(name="冲突检测"), "base_updated_at": saved.updated_at})
    )
    assert ok.status == 200

    # 基线过期（模拟会话写穿推进了草案）：409
    conflict = await handlers.handle_api_workflow_draft_put(
        _Request({"wdl": minimal_two_step_wdl(name="冲突检测"), "base_updated_at": saved.updated_at})
    )
    assert conflict.status == 409
    import json as _json

    body = _json.loads(conflict.text)
    assert body["conflict"] is True and body["updated_at"] != saved.updated_at
