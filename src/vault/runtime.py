"""宝箱运行时开关：配置页 启用/停用 立即生效并持久化，无需重启。

启用：创建并初始化 VaultService，把服务引用与 VaultTool 挂到 Root 与全部
已加载的工作空间对等会话（对等会话启动时若服务缺失则未挂工具，这里补上）。
停用：正常 shutdown（封存 open/、擦明文、注销守卫），摘掉所有会话的服务
引用——已挂的 VaultTool 会自然报「宝箱未启用」。
"""

from __future__ import annotations

import contextlib
from typing import Any

from src.core.logger import logger


def _iter_coaras(root: Any):
    """Root 自身 + 全部已加载的工作空间对等会话。"""
    yield root
    for session in getattr(root, "_sessions", {}).values():
        coara = getattr(session, "coara", None)
        if coara is not None:
            yield coara


def _attach(root: Any, service: Any) -> None:
    # 装配点豁免：vault 服务的启用/停用是「领域服务 ↔ 工具层」的组装动作，
    # 必须把 VaultTool 挂到各会话——这是分层中允许的装配边界（工具实现仍在 tools 层，
    # vault 只在此一处 import 并注册，不反向依赖工具内部逻辑）。
    from src.tools.builtin.vault.vault import VaultTool

    for coara in _iter_coaras(root):
        coara.vault_service = service
        if service is not None:
            try:
                coara.register_tool(VaultTool(parent_coara=coara), replace=True)
            except Exception as exc:
                logger.warning(f"vault tool attach failed: {exc}")


def _persist(enabled: bool) -> None:
    from src.core.config import config_manager

    try:
        config_manager.save_config_yaml({"vault_enabled": enabled})
    except Exception as exc:
        logger.warning(f"persist vault_enabled failed: {exc}")
    try:
        config = config_manager.config
    except Exception:
        config = None
    if config is not None:
        with contextlib.suppress(Exception):
            config.vault_enabled = enabled


async def set_vault_enabled(root: Any, enabled: bool) -> None:
    """立即启用/停用宝箱服务并持久化 ``vault_enabled``。"""
    current = getattr(root, "vault_service", None)
    if enabled and current is None:
        from src.core.coara_home import resolve_coara_home
        from src.core.config import config_manager
        from src.vault import VaultService

        try:
            config = config_manager.config
        except Exception:
            config = None
        coara_home = resolve_coara_home(
            getattr(root, "workspace_dir", None),
            getattr(config, "coara_home", None),
        )
        service = VaultService(coara_home)
        await service.initialize()
        _attach(root, service)
    elif not enabled and current is not None:
        await current.shutdown()
        _attach(root, None)
    _persist(enabled)
