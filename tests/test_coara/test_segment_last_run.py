"""段模型携带模型 + 空间 last-run 持久化（模型决定权在端）。

端输入开段时：Segment 记录 provider/model，并把空间 last-run 持久化到
registry 条目（重启加载 / janitor 都用它）；非端输入（background/event）
只记段、不更新 last-run。切换模型（含回合中延迟）都立即持久化到 target
自身所在空间。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.llm_binding import touch_entry_llm
from tests.helpers import FakeProvider, make_test_coara


@pytest.fixture
def manager(tmp_path: Path, monkeypatch):
    from src.workspace.manager import WorkspaceManager

    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    return WorkspaceManager(tmp_path, coara_home=tmp_path / "home")


def _coara_with_manager(tmp_path: Path, monkeypatch, manager, *, provider="kimi", model="k3"):
    coara = make_test_coara(tmp_path)
    coara.provider_name = provider
    coara.model_name = model
    entry = manager.registry.ensure_workspace(tmp_path, name="demo")
    coara.workspace_manager = manager
    return coara, entry


def test_segment_open_stamps_model_and_persists_last_run(tmp_path, monkeypatch, manager) -> None:
    coara, entry = _coara_with_manager(tmp_path, monkeypatch, manager, provider="kimi", model="k3")

    seg = coara._segments.open("web", turn_id="t1", mid_turn=False)
    coara._stamp_segment_model(seg)

    assert seg.provider == "kimi"
    assert seg.model == "k3"
    assert (entry.provider, entry.model) == ("kimi", "k3")


def test_non_user_source_does_not_persist_last_run(tmp_path, monkeypatch, manager) -> None:
    coara, entry = _coara_with_manager(tmp_path, monkeypatch, manager, provider="kimi", model="k3")

    seg = coara._segments.open("background", turn_id="t1", mid_turn=False)
    coara._stamp_segment_model(seg)

    # 段仍记录模型，但 last-run 不写（janitor/后台运行不推进）
    assert seg.provider == "kimi"
    assert seg.model == "k3"
    assert entry.provider is None
    assert entry.model is None


def test_continuation_segment_updates_last_run(tmp_path, monkeypatch, manager) -> None:
    coara, entry = _coara_with_manager(tmp_path, monkeypatch, manager, provider="kimi", model="k3")

    seg = coara._segments.open("matrix", turn_id="t1", mid_turn=True)
    coara._stamp_segment_model(seg)
    assert (entry.provider, entry.model) == ("kimi", "k3")

    # 该端切到 deepseek 后，再接续输入开段 → last-run 跟进
    coara.provider_name = "deepseek"
    coara.model_name = "deepseek-flash"
    seg2 = coara._segments.open("matrix", turn_id="t1", mid_turn=True)
    coara._stamp_segment_model(seg2)
    assert (entry.provider, entry.model) == ("deepseek", "deepseek-flash")


def test_touch_entry_llm_skips_rewrite_when_unchanged(tmp_path, monkeypatch, manager) -> None:
    coara, entry = _coara_with_manager(tmp_path, monkeypatch, manager)

    assert touch_entry_llm(manager, entry.id, "kimi", "k3") is True
    assert touch_entry_llm(manager, entry.id, "kimi", "k3") is False
    assert (entry.provider, entry.model) == ("kimi", "k3")
    assert touch_entry_llm(manager, entry.id, "deepseek", "d1") is True
    assert (entry.provider, entry.model) == ("deepseek", "d1")


@pytest.mark.asyncio
async def test_switch_llm_target_persists_to_target_space_even_deferred(tmp_path, monkeypatch) -> None:
    """切换（含回合中延迟）都立即持久化到 target 自身空间，不依赖 active_id。"""
    from src.coara.root import RootCoara
    from src.llm.registry import provider_registry

    p = FakeProvider([])
    p.name = "kimi"
    p.default_model = "k3"
    monkeypatch.setitem(provider_registry._providers, "kimi", p)

    root = RootCoara(workspace_dir=tmp_path, provider_name="kimi")
    try:
        from src.workspace.manager import WorkspaceManager

        monkeypatch.setattr(
            "src.workspace.registry.is_ephemeral_workspace_path",
            lambda _path: False,
        )
        # 用真实 manager 登记空间，替代 root 内部 manager，避免与 tmp_path 冲突
        wm = WorkspaceManager(tmp_path / "reg", coara_home=tmp_path / "home")
        root.workspace_manager = wm
        entry = wm.registry.ensure_workspace(tmp_path, name="demo")

        target = make_test_coara(tmp_path)
        target.workspace_manager = wm

        # 非回合中：立即切换 + 持久化
        applied = root.switch_llm_target(target, "kimi", "k3")
        assert applied == ("kimi", "k3")
        assert (entry.provider, entry.model) == ("kimi", "k3")

        # 回合中：延迟生效，但持久化立即写
        target._inside_turn = True
        root.switch_llm_target(target, "kimi", "k3")
        assert target._pending_llm_switch == ("kimi", "k3")
        assert (entry.provider, entry.model) == ("kimi", "k3")
    finally:
        await root.stop_idle_timeout_watcher()


@pytest.mark.asyncio
async def test_switch_llm_target_ignores_active_id_mismatch(tmp_path, monkeypatch) -> None:
    """绑定目标按 target 自身空间匹配，不写 workspace_manager.active_id 指向的空间。"""
    from src.coara.root import RootCoara
    from src.llm.registry import provider_registry

    p = FakeProvider([])
    p.name = "kimi"
    p.default_model = "k3"
    monkeypatch.setitem(provider_registry._providers, "kimi", p)

    root = RootCoara(workspace_dir=tmp_path, provider_name="kimi")
    try:
        from src.workspace.manager import WorkspaceManager

        monkeypatch.setattr(
            "src.workspace.registry.is_ephemeral_workspace_path",
            lambda _path: False,
        )
        wm = WorkspaceManager(tmp_path / "reg", coara_home=tmp_path / "home")
        root.workspace_manager = wm
        # active_id 指向 A 空间；target 属于 B 空间
        ws_a = wm.registry.ensure_workspace(tmp_path / "ws_a", name="ws_a")
        ws_b = wm.registry.ensure_workspace(tmp_path / "ws_b", name="ws_b")
        wm.switch(ws_a.id)
        root._foreground_session_id = ws_a.id

        target = make_test_coara(tmp_path / "ws_b")
        target.workspace_manager = wm

        root.switch_llm_target(target, "kimi", "k3")

        # 绑定写到 target 自身空间 B，A 不动
        assert (ws_b.provider, ws_b.model) == ("kimi", "k3")
        assert (ws_a.provider, ws_a.model) == (None, None)
    finally:
        await root.stop_idle_timeout_watcher()
