"""Dashboard token authentication."""

from __future__ import annotations

import hmac

from aiohttp import web

from src.ui.dashboard_tokens import load_or_create_dashboard_token


def read_token_from_request(request: web.Request) -> str:
    return request.query.get("token", "") or request.headers.get("X-Coara-Token", "")


def check_dashboard_token(
    request: web.Request,
    *,
    workspace_dir,
    coara_home=None,
) -> str:
    expected = load_or_create_dashboard_token(workspace_dir, coara_home)
    provided = read_token_from_request(request)
    # Constant-time comparison; encode first so non-ASCII input can't raise.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise web.HTTPUnauthorized(text="Invalid or missing token")
    return expected
