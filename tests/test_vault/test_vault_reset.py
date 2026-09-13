"""VaultService.reset：删除全部内容回到未初始化。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.vault import VaultService
from src.vault.paths import vault_sealed_dir


@pytest.mark.asyncio
async def test_reset_wipes_everything(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("old-password-123")
    open_dir = service.unlock("old-password-123")
    (open_dir / "secret.txt").write_text("body", encoding="utf-8")
    service.lock()
    assert service.status().entries == 1

    service.reset()

    assert not service.is_initialized()
    assert service.status().entries == 0
    assert list(vault_sealed_dir(service.vault_dir).glob("*")) == []
    assert not service.open_dir.exists() or list(service.open_dir.glob("**/*")) == []
    # 旧密码失效（meta 已删），可直接重新设置新密码
    service.setup("new-password-456")
    assert service.is_initialized()
    service.lock()
    await service.shutdown()


@pytest.mark.asyncio
async def test_reset_when_locked_and_empty(tmp_path: Path) -> None:
    """未初始化/锁定状态下 reset 幂等不报错。"""
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.reset()
    assert not service.is_initialized()
    service.reset()
    assert not service.is_initialized()
    await service.shutdown()
