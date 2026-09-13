"""Unit tests for Matrix ingress helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.matrix_client.ingress_helpers import (
    matrix_join_url,
    should_skip_matrix_self_event,
    try_defer_to_continuation_input,
)


def _patch_owner_ids(monkeypatch: pytest.MonkeyPatch, owner_ids: list[str]) -> None:
    """Patch security config read path used by trust/invite helpers.

    函数内 `from src.core.config import config_manager` 取模块属性，
    patch 模块级单例的方法即可命中。
    """
    from src.core.config import ConfigManager

    monkeypatch.setattr(
        ConfigManager,
        "get_security_config",
        lambda self: {"owner_matrix_ids": owner_ids},
    )


def test_trust_owner_list_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.matrix_client.ingress_helpers import resolve_matrix_trust_level

    _patch_owner_ids(monkeypatch, ["@alice:coara.local"])
    assert resolve_matrix_trust_level("@alice:coara.local", cli_owner=True) == "owner"
    # 名单存在时 CLI 模式不再全员 owner
    assert resolve_matrix_trust_level("@mallory:coara.local", cli_owner=True) == "untrusted"
    assert resolve_matrix_trust_level("@mallory:coara.local", cli_owner=False) == "untrusted"


def test_trust_empty_list_cli_fallback_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """空名单 + CLI 模式保持旧行为（回退 owner），bot 模式仍 untrusted。"""
    from src.matrix_client.ingress_helpers import resolve_matrix_trust_level

    _patch_owner_ids(monkeypatch, [])
    assert resolve_matrix_trust_level("@anyone:coara.local", cli_owner=True) == "owner"
    assert resolve_matrix_trust_level("@anyone:coara.local", cli_owner=False) == "untrusted"


def test_invite_owner_and_local_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.matrix_client.ingress_helpers import should_accept_matrix_invite

    _patch_owner_ids(monkeypatch, ["@alice:remote.org"])
    # owner 名单成员接受（MXID 精确匹配）
    assert should_accept_matrix_invite(sender="@alice:remote.org", bot_user_id="@coara:coara.local")
    # 本地保留域接受
    assert should_accept_matrix_invite(sender="@bob:coara.local", bot_user_id="@coara:coara.local")
    # 陌生远程拒绝；域名伪装结尾锚定拒绝
    assert not should_accept_matrix_invite(sender="@mallory:evil.com", bot_user_id="@coara:coara.local")
    assert not should_accept_matrix_invite(sender="@x:coara.local.evil.com", bot_user_id="@coora:coara.local")


def test_invite_empty_list_only_local_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """空名单时仅保留域可入；同 server_part 泛匹配已移除。"""
    from src.matrix_client.ingress_helpers import should_accept_matrix_invite

    _patch_owner_ids(monkeypatch, [])
    assert should_accept_matrix_invite(sender="@bob:coara.local", bot_user_id="@coara:coara.local")
    # 旧的「同域即接受」已收敛为保留域白名单——非保留域格式拒绝
    assert not should_accept_matrix_invite(sender="@x:not-local.io", bot_user_id="@coara:coara.local")
    assert not should_accept_matrix_invite(sender="@x:coara.local.evil.com", bot_user_id="@coara:coara.local")


def _make_root_with_active_turn(active: bool) -> MagicMock:
    root = MagicMock()
    root.foreground_coara.has_active_turn.return_value = active
    root.foreground_coara.submit_continuation_input = MagicMock()
    return root


def test_ingress_helpers() -> None:
    assert (
        matrix_join_url("https://matrix.example", "!room:example.com")
        == "https://matrix.example/_matrix/client/v3/rooms/%21room%3Aexample.com/join"
    )


def test_is_any_coara_envelope() -> None:
    from src.matrix_client.ingress_helpers import is_any_coara_envelope

    # catch-all 语义：已知 / 废弃 / 未知标签，只要像信封一律 True（handler 之后兜底吞）
    assert is_any_coara_envelope('[COARA_DIRTREE]\n{"type":"query"}\n[/COARA_DIRTREE]')
    assert is_any_coara_envelope("  [COARA_FUTURE_THING]\n{}")
    assert is_any_coara_envelope('[COARA_TOOL]{"tool_name":"shell"}')
    assert is_any_coara_envelope('[COARA_STATUS]\n{"status":"idle"}')
    # 普通聊天里提到标签不算信封（必须行首锚定）
    assert not is_any_coara_envelope("动态格式是 [COARA_DIRTREE]…")
    assert not is_any_coara_envelope("你好")
    assert not is_any_coara_envelope("")


def test_ingress_helpers_sources_envelope_spec() -> None:
    """兜底实现必须来自生成的 envelope_spec（单一真源），不得在本模块重复定义正则。"""
    import src.matrix_client.ingress_helpers as ingress
    from src.matrix_client import envelope_spec

    assert not hasattr(ingress, "_UNRECOGNIZED_COARA_ENVELOPE_RE")
    assert ingress.is_any_coara_envelope(
        '[COARA_DIRTREE]\n{"type":"query"}\n[/COARA_DIRTREE]'
    ) == envelope_spec.is_any_coara_envelope('[COARA_DIRTREE]\n{"type":"query"}\n[/COARA_DIRTREE]')
    # 真源里 COARA_DIRTREE 已被标记废弃：不得出现在有效标签集里
    assert "COARA_DIRTREE" in envelope_spec.DEPRECATED_TAGS
    assert "COARA_DIRTREE" not in envelope_spec.ENVELOPE_TAGS


def test_prefilter_swallows_unrecognized_coara_envelope() -> None:
    """旧 APK 发来已移除的 [COARA_DIRTREE] 查询：吞掉，不得落到 LLM。"""
    from src.matrix_client.ingress_helpers import prefilter_matrix_text_event

    root = MagicMock()
    body = '[COARA_DIRTREE]\n{"type":"query"}\n[/COARA_DIRTREE]'
    assert prefilter_matrix_text_event(root=root, room_id="!r", body=body) is True
    # 普通聊天不受影响
    assert prefilter_matrix_text_event(root=root, room_id="!r", body="/new") is False
    assert should_skip_matrix_self_event(
        sender="@coara:coara.local",
        bot_user_id="@coara:coara.local",
    )
    assert not should_skip_matrix_self_event(
        sender="@phone_user:coara.local",
        bot_user_id="@coara:coara.local",
    )

    busy = _make_root_with_active_turn(active=True)
    assert try_defer_to_continuation_input(busy, "follow-up", channel="matrix") is True
    # 入站存裸文本，来源标签由内核按 source 现包（turn_orchestrator 接续现包）
    busy.foreground_coara.submit_continuation_input.assert_called_once_with("follow-up", source="matrix")

    idle = _make_root_with_active_turn(active=False)
    assert try_defer_to_continuation_input(idle, "follow-up", channel="matrix") is False
    idle.foreground_coara.submit_continuation_input.assert_not_called()

    skip = _make_root_with_active_turn(active=True)
    assert try_defer_to_continuation_input(skip, "   ", channel="matrix") is False
    assert try_defer_to_continuation_input(skip, "/new", channel="matrix") is False
    skip.foreground_coara.submit_continuation_input.assert_not_called()
