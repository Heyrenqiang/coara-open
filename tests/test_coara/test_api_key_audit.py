"""API key 审计与系统消息的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara import api_key_audit
from src.core import system_messages

# ── system_messages ─────────────────────────────────────────────────────────


def test_add_and_list_system_messages(tmp_path: Path) -> None:
    ok = system_messages.add_system_message(tmp_path, kind="api_key", title="t1", body="b1")
    assert ok is True
    messages = system_messages.list_system_messages(tmp_path)
    assert len(messages) == 1
    assert messages[0]["title"] == "t1"
    assert messages[0]["kind"] == "api_key"


def test_system_message_dedupe_key_skips(tmp_path: Path) -> None:
    first = system_messages.add_system_message(tmp_path, kind="api_key", title="t", body="b", dedupe_key="k1")
    second = system_messages.add_system_message(tmp_path, kind="api_key", title="t", body="b", dedupe_key="k1")
    assert first is True
    assert second is False
    assert len(system_messages.list_system_messages(tmp_path)) == 1


# ── api_key_audit ───────────────────────────────────────────────────────────


def test_state_fingerprint_stable_and_sorted() -> None:
    a = api_key_audit._state_fingerprint({"web_search": ["exa", "baidu"], "llm": ["deepseek"]})
    b = api_key_audit._state_fingerprint({"llm": ["deepseek"], "web_search": ["baidu", "exa"]})
    assert a == b


def test_usable_key_accepts_standard_api_keys() -> None:
    assert api_key_audit._is_usable_key("sk-abc123XYZ-_.") is True


def test_usable_key_rejects_whitespace_and_non_ascii() -> None:
    """含空白/控制字符/非 ASCII 的 key 无法进 HTTP header，视为格式非法。"""
    assert api_key_audit._is_usable_key("") is False
    assert api_key_audit._is_usable_key("sk-abc 123") is False
    assert api_key_audit._is_usable_key("sk-abc\t123") is False
    assert api_key_audit._is_usable_key("sk-中文key") is False


def test_audit_writes_only_on_state_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """状态未变不重复写；变更才写并更新快照。"""
    fake_missing = {"web_search": ["exa"]}
    monkeypatch.setattr(api_key_audit, "build_missing_map", lambda cm: dict(fake_missing))

    class _CM:
        def list_providers(self):
            return []

    # 首次：无快照 → 视为变更，写消息
    assert api_key_audit.audit_api_keys(_CM(), tmp_path) is True
    assert len(system_messages.list_system_messages(tmp_path)) == 1

    # 状态未变：静默
    assert api_key_audit.audit_api_keys(_CM(), tmp_path) is False
    assert len(system_messages.list_system_messages(tmp_path)) == 1

    # 状态变更：再写一条
    fake_missing["web_search"] = ["exa", "serper"]
    assert api_key_audit.audit_api_keys(_CM(), tmp_path) is True
    assert len(system_messages.list_system_messages(tmp_path)) == 2


def test_audit_all_set_writes_cleared_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """全部配齐时（missing 为空）也写一条「已全部配置」的消息。"""
    monkeypatch.setattr(api_key_audit, "build_missing_map", lambda cm: {})

    class _CM:
        def list_providers(self):
            return []

    assert api_key_audit.audit_api_keys(_CM(), tmp_path) is True
    messages = system_messages.list_system_messages(tmp_path)
    assert messages[0]["title"] == "API key 已全部配置"


# ── media 白名单（仅 Agnes） ─────────────────────────────────────────────


def test_media_invocation_rejects_missing_agnes(tmp_path: Path) -> None:
    from src.tools.builtin.media.media import MediaToolInvocation

    with pytest.raises(ValueError, match="agnes"):
        MediaToolInvocation(
            {"kind": "image", "prompt": "x"},
            workspace_root=tmp_path,
            available_providers={"siliconflow"},
        )


def test_media_invocation_uses_agnes(tmp_path: Path) -> None:
    from src.tools.builtin.media.media import MediaToolInvocation

    inv = MediaToolInvocation(
        {"kind": "image", "prompt": "x"},
        workspace_root=tmp_path,
        available_providers={"agnes"},
    )
    assert inv.provider == "agnes"


def test_media_tool_is_single_agnes_tool(tmp_path: Path) -> None:
    from src.tools.builtin.media.media import MediaTool

    tool = MediaTool(workspace_root=tmp_path)
    assert tool.name == "media"
    assert tool.parameters_schema["properties"]["action"]["enum"] == ["generate", "status", "cancel"]
