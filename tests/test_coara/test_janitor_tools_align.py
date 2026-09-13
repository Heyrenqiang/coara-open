"""janitor 工具表 ≡ 父会话（prompt cache 前缀）回归。"""

from __future__ import annotations

from types import SimpleNamespace

from src.coara import janitor_maintenance as jm


def _patch_ws_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.coara.janitor_maintenance.workspace_id_for", lambda workspace_dir: str(workspace_dir)
    )


def test_align_tools_exactly_to_parent_sets_override_and_keys(monkeypatch) -> None:
    """有父会话：LLM defs 与可执行 keys 均对齐；不多出 review。"""
    _patch_ws_id(monkeypatch)
    janitor = SimpleNamespace(
        _tool_definitions_override=None,
        _tool_manager=SimpleNamespace(
            tools={
                "read": SimpleNamespace(name="read"),
                "write": SimpleNamespace(name="write"),
                "review": SimpleNamespace(name="review"),
                "record": SimpleNamespace(name="record"),
            }
        ),
        register_tool=lambda *a, **k: None,
    )
    parent = SimpleNamespace(
        _tool_manager=SimpleNamespace(
            tools={
                "read": SimpleNamespace(name="read", should_defer=False),
                "record": SimpleNamespace(name="record", should_defer=False),
                "local_search": SimpleNamespace(name="local_search", should_defer=True),
            }
        ),
        _get_tool_definitions_for_llm=lambda: [
            {"name": "read"},
            {"name": "record"},
            {"name": "local_search"},
        ],
    )
    store = object()
    bound: list[str] = []

    def _register(tool, replace=True):
        bound.append(tool.name)
        janitor._tool_manager.tools[tool.name] = tool

    janitor.register_tool = _register
    root = SimpleNamespace(
        _sessions={"ws": SimpleNamespace(coara=parent)},
        records_store=store,
    )
    jm._align_tools_exactly_to_parent(janitor, root, "ws")
    assert set(janitor._tool_manager.tools.keys()) == {"read", "record", "local_search"}
    assert "review" not in janitor._tool_manager.tools
    assert janitor._tool_definitions_override == [
        {"name": "read"},
        {"name": "record"},
        {"name": "local_search"},
    ]
    assert "record" in bound
    assert "local_search" in bound


def test_align_tools_skipped_without_parent(monkeypatch) -> None:
    """父会话缺失：不改动 override。"""
    _patch_ws_id(monkeypatch)
    janitor = SimpleNamespace(
        _tool_definitions_override=None,
        _tool_manager=SimpleNamespace(tools={"read": object()}),
    )
    jm._align_tools_exactly_to_parent(janitor, SimpleNamespace(_sessions={}), "ws")
    assert janitor._tool_definitions_override is None


def test_janitor_config_tools_empty() -> None:
    """yaml 白名单作废：config.tools 恒为空列表。"""
    cfg = jm._janitor_config()
    assert cfg is not None
    assert cfg.tools == []
