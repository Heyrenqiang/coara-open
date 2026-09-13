"""工作空间级 LLM 绑定：条目字段序列化 + 解析矩阵 + 绑定写路径."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.workspace.llm_binding import bind_entry_llm, clear_entry_llm, entry_llm_override
from src.workspace.manager import WorkspaceManager
from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import WorkspaceEntry


@pytest.fixture
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(
        "src.workspace.registry.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    return WorkspaceManager(tmp_path, coara_home=tmp_path / "home")


def _entry(**kwargs) -> WorkspaceEntry:
    base = {"name": "v8", "id": "v8-1", "path": "D:/code_ws/v8"}
    base.update(kwargs)
    return WorkspaceEntry(**base)


def test_entry_llm_fields_roundtrip(manager: WorkspaceManager, tmp_path: Path) -> None:
    entry = manager.registry.ensure_workspace(tmp_path, name="demo")
    bind_entry_llm(manager, entry.id, "kimi", "kimi-k2")

    fresh = WorkspaceRegistry(manager.registry.coara_home)
    fresh.load()
    loaded = fresh.get_by_id(entry.id)
    assert loaded is not None
    assert loaded.provider == "kimi"
    assert loaded.model == "kimi-k2"

    clear_entry_llm(manager, entry.id)
    fresh.load()
    loaded = fresh.get_by_id(entry.id)
    assert loaded is not None
    assert loaded.provider is None
    assert loaded.model is None


def test_override_unbound_and_empty_strings() -> None:
    assert entry_llm_override(None) == (None, None)
    assert entry_llm_override(_entry()) == (None, None)
    assert entry_llm_override(_entry(provider="  ", model="")) == (None, None)
    # 只有 model 没有 provider 视为未绑定（成对校验）
    assert entry_llm_override(_entry(model="k2")) == (None, None)


def test_override_bound_provider_valid(monkeypatch) -> None:
    monkeypatch.setattr("src.llm.registry.provider_registry.has", lambda name: True)
    assert entry_llm_override(_entry(provider="kimi", model="k2")) == ("kimi", "k2")
    # provider-only：model 留空，解析层用 provider 默认模型
    assert entry_llm_override(_entry(provider="kimi")) == ("kimi", None)


def test_override_bound_provider_missing_falls_back(monkeypatch) -> None:
    monkeypatch.setattr("src.llm.registry.provider_registry.has", lambda name: False)
    assert entry_llm_override(_entry(provider="ghost", model="x")) == (None, None)


def test_bind_unknown_workspace_returns_false(manager: WorkspaceManager) -> None:
    assert bind_entry_llm(manager, "no-such-id", "kimi", "k2") is False
    assert clear_entry_llm(manager, "no-such-id") is False
