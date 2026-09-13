"""CLI @服务台：解析 / 名单 / 投递语义。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.coara.service_desk import (
    ServiceDeskError,
    canonical_desk_name,
    is_known_desk_name,
    list_service_desks,
    parse_service_desk_at,
    resolve_service_desk_coara,
)
from src.workspace.types import WorkspaceKind


def test_parse_service_desk_at_hits() -> None:
    assert parse_service_desk_at("@daily 今天摘要") == ("daily", "今天摘要")
    assert parse_service_desk_at("@配置 换模型") == ("配置", "换模型")
    assert parse_service_desk_at("@daily") == ("daily", "")


def test_parse_service_desk_at_rejects_mid_text() -> None:
    assert parse_service_desk_at("请看 @daily 摘要") is None
    assert parse_service_desk_at("hello") is None
    assert parse_service_desk_at("") is None


def test_canonical_desk_name() -> None:
    assert canonical_desk_name("config") == "配置"
    assert canonical_desk_name("配置") == "配置"
    # 记录空间与 daily 合并：daily/记录/records 是同一会话的别名，展示名归一为「记录」
    assert canonical_desk_name("daily") == "记录"
    assert canonical_desk_name("记录") == "记录"
    assert canonical_desk_name("records") == "记录"


def test_list_service_desks_includes_config_and_internal(tmp_path: Path) -> None:
    daily = SimpleNamespace(
        name="daily",
        kind=WorkspaceKind.INTERNAL,
        summary="日常",
        end_allowed=lambda end: end == "cli",
    )
    project = SimpleNamespace(
        name="v8",
        kind=WorkspaceKind.MANAGED,
        summary="",
        end_allowed=lambda end: True,
    )
    registry = MagicMock()
    registry.list_active = MagicMock(return_value=[daily, project])
    root = SimpleNamespace(workspace_manager=SimpleNamespace(registry=registry))
    desks = list_service_desks(root)
    names = [d["name"] for d in desks]
    assert "配置" in names
    assert "daily" in names
    assert "v8" not in names


def test_is_known_desk_name() -> None:
    registry = MagicMock()
    registry.list_active = MagicMock(return_value=[])
    root = SimpleNamespace(workspace_manager=SimpleNamespace(registry=registry))
    assert is_known_desk_name(root, "配置")
    assert is_known_desk_name(root, "config")
    # 记录空间服务台是常驻目标：daily/记录 均认得（不依赖条目是否已登记）
    assert is_known_desk_name(root, "daily")
    assert is_known_desk_name(root, "记录")
    assert not is_known_desk_name(root, "v8")


@pytest.mark.asyncio
async def test_resolve_config_desk_uses_module_root() -> None:
    module = SimpleNamespace(session_id="cfg-1")
    web_server = SimpleNamespace(_get_module_root=AsyncMock(return_value=module))
    root = SimpleNamespace(_web_server=web_server, workspace_manager=None)
    coara = await resolve_service_desk_coara(root, "配置", web_server=web_server)
    assert coara is module
    web_server._get_module_root.assert_awaited_with("config")


@pytest.mark.asyncio
async def test_resolve_rejects_managed_workspace() -> None:
    entry = SimpleNamespace(
        name="v8",
        kind=WorkspaceKind.MANAGED,
        end_allowed=lambda _e: True,
    )
    registry = MagicMock()
    registry.resolve_name_or_id = MagicMock(return_value=entry)
    root = SimpleNamespace(workspace_manager=SimpleNamespace(registry=registry))
    with pytest.raises(ServiceDeskError, match="不是服务空间"):
        await resolve_service_desk_coara(root, "v8")


@pytest.mark.asyncio
async def test_resolve_daily_aliases_route_to_same_session(tmp_path: Path, monkeypatch) -> None:
    """@daily / @记录 / @records 均路由到记录空间的同一会话（persona=daily）。"""
    from src.records import daily_curator as dc

    calls: list[object] = []
    session = SimpleNamespace(coara=SimpleNamespace(session_id="daily-1"))

    async def _ensure(root):
        calls.append(root)
        return session

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    root = SimpleNamespace(workspace_manager=None)

    for name in ("daily", "记录", "records", "Daily"):
        coara = await resolve_service_desk_coara(root, name)
        assert coara is session.coara
    assert len(calls) == 4


def test_at_service_desk_completer_only_desks() -> None:
    from prompt_toolkit.document import Document

    from src.cli.completers import AtServiceDeskCompleter

    completer = AtServiceDeskCompleter(
        [{"name": "daily", "summary": "日常"}, {"name": "配置", "summary": "配置"}]
    )
    doc = Document("@da", cursor_position=3)
    texts = [c.text for c in completer.get_completions(doc, None)]
    assert texts == ["@daily "]
    # 正文中间 @ 不补全
    mid = Document("看 @da", cursor_position=5)
    assert list(completer.get_completions(mid, None)) == []
