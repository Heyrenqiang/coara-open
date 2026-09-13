"""访客房间白名单（matrix.guest_rooms）测试。"""

from __future__ import annotations

from types import SimpleNamespace

from src.matrix_client.ingress_helpers import guest_room_allowed


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
