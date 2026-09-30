"""访客房间白名单（matrix.guest_rooms）与空间 kind=external 双门测试。"""

from __future__ import annotations

from types import SimpleNamespace

from src.matrix_client.ingress_helpers import (
    guest_room_allowed,
    untrusted_ingress_allowed,
    workspace_allows_untrusted,
)
from src.workspace.types import WorkspaceKind


def _patch_config(monkeypatch, guest_rooms):
    from src.core.config import config_manager

    fake = SimpleNamespace(matrix=SimpleNamespace(guest_rooms=guest_rooms))
    monkeypatch.setattr(config_manager, "_config", fake, raising=False)


def test_default_allows_all(monkeypatch):
    _patch_config(monkeypatch, ["*"])
    assert guest_room_allowed("!any:coara.local")


def test_empty_list_rejects_all(monkeypatch):
    _patch_config(monkeypatch, [])
    assert not guest_room_allowed("!any:coara.local")


def test_exact_room_match(monkeypatch):
    _patch_config(monkeypatch, ["!open:coara.local"])
    assert guest_room_allowed("!open:coara.local")
    assert not guest_room_allowed("!closed:coara.local")


def test_none_falls_back_to_allow_all(monkeypatch):
    # guest_rooms 为 None（配置缺省路径）→ 默认 ["*"] 放行
    _patch_config(monkeypatch, None)
    assert guest_room_allowed("!any:coara.local")


def test_workspace_allows_untrusted_only_external():
    registry = SimpleNamespace(
        get_by_id=lambda wid: {
            "ext": SimpleNamespace(kind=WorkspaceKind.EXTERNAL),
            "norm": SimpleNamespace(kind=WorkspaceKind.NORMAL),
        }.get(wid)
    )
    root = SimpleNamespace(workspace_manager=SimpleNamespace(registry=registry))
    assert workspace_allows_untrusted(root, "ext")
    assert not workspace_allows_untrusted(root, "norm")
    assert not workspace_allows_untrusted(root, "missing")


def test_untrusted_ingress_requires_external_workspace(monkeypatch):
    _patch_config(monkeypatch, ["*"])
    registry = SimpleNamespace(
        get_by_id=lambda wid: {
            "ext": SimpleNamespace(kind=WorkspaceKind.EXTERNAL),
            "norm": SimpleNamespace(kind=WorkspaceKind.NORMAL),
        }.get(wid)
    )
    root = SimpleNamespace(workspace_manager=SimpleNamespace(registry=registry))
    ok, reason = untrusted_ingress_allowed(root, "!r:hs", workspace_id="ext")
    assert ok and reason == ""
    ok, reason = untrusted_ingress_allowed(root, "!r:hs", workspace_id="norm")
    assert not ok and reason == "workspace_kind"
