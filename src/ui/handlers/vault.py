from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase


class VaultHandlers(HandlerMixinBase):
    def _vault_status_payload(self) -> dict[str, Any]:
        service = getattr(self.root, "vault_service", None)
        payload: dict[str, Any] = {
            "enabled": service is not None,
            "initialized": False,
            "unlocked": False,
            "entries": 0,
        }
        if service is not None:
            status = service.status()
            payload.update(
                {
                    "initialized": status.initialized,
                    "unlocked": service.is_unlocked(),
                    "entries": status.entries,
                }
            )
        return payload

    def _vault_service_or_400(self) -> Any:
        return getattr(self.root, "vault_service", None)

    async def _handle_vault_status(self, request: web.Request) -> web.Response:
        self._check_token(request)
        return web.json_response(self._vault_status_payload())

    async def _handle_vault_enabled(self, request: web.Request) -> web.Response:
        """启用/停用宝箱：立即生效并持久化 vault_enabled。"""
        self._check_token(request)
        from src.vault.runtime import set_vault_enabled

        data = await request.json()
        try:
            await set_vault_enabled(self.root, bool(data.get("enabled")))
        except Exception as exc:
            logger.warning(f"vault toggle failed: {exc}")
            return web.json_response({"detail": "toggle_failed"}, status=500)
        return web.json_response(self._vault_status_payload())

    async def _handle_vault_password_set(self, request: web.Request) -> web.Response:
        """首次设置主密码（未初始化才允许；已设置只能改密或重置）。"""
        self._check_token(request)
        service = self._vault_service_or_400()
        if service is None:
            return web.json_response({"detail": "vault_disabled"}, status=400)
        if service.is_initialized():
            return web.json_response({"detail": "already_initialized"}, status=400)
        data = await request.json()
        password = str(data.get("password") or "")
        try:
            await asyncio.to_thread(service.setup, password)
        except ValueError:
            return web.json_response({"detail": "weak_password"}, status=400)
        return web.json_response(self._vault_status_payload())

    async def _handle_vault_password_change(self, request: web.Request) -> web.Response:
        """修改主密码：验旧密码，全部密文重新加密。"""
        from src.vault.errors import VaultAuthError, VaultNotInitializedError

        self._check_token(request)
        service = self._vault_service_or_400()
        if service is None:
            return web.json_response({"detail": "vault_disabled"}, status=400)
        data = await request.json()
        old_password = str(data.get("old_password") or "")
        new_password = str(data.get("new_password") or "")
        try:
            # 经 async 变体持 _unlock_op_lock：与 idle 看门狗的自动上锁互斥，
            # 防止 rekey 中途 lock() 覆盖会话状态
            await service.change_password_async(
                old_password=old_password,
                new_password=new_password,
            )
        except VaultNotInitializedError:
            return web.json_response({"detail": "not_initialized"}, status=400)
        except VaultAuthError:
            return web.json_response({"detail": "wrong_password"}, status=400)
        except ValueError:
            return web.json_response({"detail": "weak_password"}, status=400)
        return web.json_response(self._vault_status_payload())

    async def _handle_vault_lock(self, request: web.Request) -> web.Response:
        """立即上锁（封存 open/ 明文）。"""
        self._check_token(request)
        service = self._vault_service_or_400()
        if service is None:
            return web.json_response({"detail": "vault_disabled"}, status=400)
        await service.lock_async()
        return web.json_response(self._vault_status_payload())

    async def _handle_vault_reset(self, request: web.Request) -> web.Response:
        """重置宝箱：删除全部封存内容回到未初始化，不可逆。"""
        self._check_token(request)
        service = self._vault_service_or_400()
        if service is None:
            return web.json_response({"detail": "vault_disabled"}, status=400)
        await asyncio.to_thread(service.reset)
        return web.json_response(self._vault_status_payload())

    # ------------------------------------------------------------------
    # SPA index
    # ------------------------------------------------------------------
