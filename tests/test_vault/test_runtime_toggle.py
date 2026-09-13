"""宝箱运行时开关（src/vault/runtime.py）：启用/停用立即生效并持久化。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.config import config_manager
from src.vault.runtime import set_vault_enabled


def _fake_coara() -> Any:
    return SimpleNamespace(vault_service=None, register_tool=lambda *a, **k: None)


@pytest.fixture()
def saved_config(monkeypatch) -> dict:
    """拦截 save_config_yaml，不写真实配置。"""
    saved: dict = {}
    monkeypatch.setattr(config_manager, "save_config_yaml", lambda d: saved.update(d))
    return saved


@pytest.mark.asyncio
async def test_enable_attach_disable_detach(tmp_path: Path, saved_config: dict) -> None:
    root = _fake_coara()
    root.workspace_dir = tmp_path
    root._sessions = {}

    await set_vault_enabled(root, True)
    assert root.vault_service is not None
    assert saved_config == {"vault_enabled": True}

    # 启用后新加载的对等会话：再次开关会把服务镜像过去
    peer = _fake_coara()
    root._sessions["ws1"] = SimpleNamespace(coara=peer)
    await set_vault_enabled(root, False)
    assert root.vault_service is None
    assert saved_config == {"vault_enabled": False}
    await set_vault_enabled(root, True)
    assert peer.vault_service is root.vault_service

    await root.vault_service.shutdown()


@pytest.mark.asyncio
async def test_service_state_survives_toggle(tmp_path: Path, saved_config: dict) -> None:
    """停用再启用：同一 vault 目录，已设置的密码与内容不丢。"""
    root = _fake_coara()
    root.workspace_dir = tmp_path
    root._sessions = {}

    await set_vault_enabled(root, True)
    service = root.vault_service
    service.setup("keep-password-123")
    await set_vault_enabled(root, False)

    await set_vault_enabled(root, True)
    assert root.vault_service is not service
    assert root.vault_service.is_initialized()
    # 旧密码仍能解锁
    root.vault_service.unlock("keep-password-123")
    root.vault_service.lock()
    await root.vault_service.shutdown()


@pytest.mark.asyncio
async def test_toggle_idempotent(tmp_path: Path, saved_config: dict) -> None:
    root = _fake_coara()
    root.workspace_dir = tmp_path
    root._sessions = {}

    await set_vault_enabled(root, False)  # 本就停用：不报错
    assert root.vault_service is None
    await set_vault_enabled(root, True)
    service = root.vault_service
    await set_vault_enabled(root, True)  # 重复启用：不换服务
    assert root.vault_service is service
    await service.shutdown()
