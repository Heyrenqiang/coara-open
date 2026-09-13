"""REST handler 共享基类/mixin（src/ui/handlers 域）.

与 ``web_server.py`` 中 WebServer 自带的 ``_check_token``（仅校验不写回）不同，
本 mixin 提供「校验并把最新 token 写回 ``self.auth_token``」的版本，供
DashboardRestHandlers / SettingsHandlers 等独立 handler 类复用。
"""

from __future__ import annotations

from aiohttp import web

from src.ui.dashboard_auth import check_dashboard_token
from src.ui.handler_contract import HandlerMixinBase


class DashboardAuthMixin(HandlerMixinBase):
    """Provide ``_check_token`` that refreshes ``self.auth_token`` from the request.

    Requires ``self.workspace_dir`` and ``self.coara_home`` on the host class.
    """

    def _check_token(self, request: web.Request) -> None:
        self.auth_token = check_dashboard_token(
            request,
            workspace_dir=self.workspace_dir,
            coara_home=self.coara_home,
        )
