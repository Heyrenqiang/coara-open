"""REST handler 共享基类/mixin（src/ui/handlers 域）."""

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
