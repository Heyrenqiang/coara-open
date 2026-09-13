"""HTTP webhook receiver for inbound events."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.event_sources.types import EventSourceDefinition

IngestCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

MAX_WEBHOOK_BODY_BYTES = 2 * 1024 * 1024


async def read_webhook_body(
    request: web.Request, *, limit: int = MAX_WEBHOOK_BODY_BYTES
) -> tuple[bytes | None, web.Response | None]:
    """Read request body up to ``limit`` bytes; return ``(raw, error_response)``."""
    try:
        raw = await request.content.read(limit + 1)
    except Exception as exc:
        logger.warning(f"Webhook body read failed: {exc}")
        return None, web.json_response({"error": "failed to read body"}, status=400)
    if len(raw) > limit:
        return None, web.json_response({"error": "payload too large"}, status=413)
    return raw, None


class WebhookIngressServer:
    """Shared aiohttp server; one route per enabled webhook event source."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        ingest: IngestCallback,
    ):
        self.host = host
        self.port = port
        self._ingest = ingest
        self._sources: dict[str, EventSourceDefinition] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def register(self, defn: EventSourceDefinition) -> None:
        self._sources[defn.id] = defn

    def has_source(self, source_id: str) -> bool:
        return source_id in self._sources

    def unregister_all(self) -> None:
        self._sources.clear()

    @property
    def url_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        if not self._sources:
            return
        if self._runner is not None:
            return

        self._app = web.Application()
        self._app.router.add_post("/webhook/{source_id}", self._handle_post)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        ids = ", ".join(sorted(self._sources))
        logger.info(f"Webhook server listening on {self.url_base} (event sources: {ids})")

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._app = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        ids = sorted(self._sources.keys())
        return web.json_response({"ok": True, "event_sources": ids})

    async def _handle_post(self, request: web.Request) -> web.Response:
        source_id = request.match_info.get("source_id", "")
        defn = self._sources.get(source_id)
        if defn is None:
            return web.json_response({"error": f"unknown event source '{source_id}'"}, status=404)

        if defn.webhook_secret:
            token = self._extract_token(request)
            if not token or not hmac.compare_digest(token.encode("utf-8"), defn.webhook_secret.encode("utf-8")):
                return web.json_response({"error": "unauthorized"}, status=401)
        else:
            # 未配 secret 时浏览器跨站表单（text/plain 等简单请求绕 CORS，
            # 127.0.0.1 本地恶意网页即可 POST）可零鉴权注入事件。拦截带
            # Origin/Referer 的浏览器来源请求；无头的正常 webhook 调用方
            # （curl/GitHub/n8n 等不发 Origin）不受影响。
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin:
                logger.warning(
                    f"Webhook '{source_id}' rejected cross-site browser request "
                    f"(Origin={origin!r}); configure webhook_secret to authenticate callers"
                )
                return web.json_response(
                    {"error": "cross-site browser requests are not accepted; configure webhook_secret"},
                    status=403,
                )

        raw, error_response = await read_webhook_body(request)
        if error_response is not None:
            return error_response
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"error": "invalid JSON body"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

        try:
            await self._ingest(source_id, body)
        except Exception as exc:
            logger.warning(f"Webhook ingest failed for '{source_id}': {exc}")
            return web.json_response({"error": "ingest failed"}, status=500)

        return web.json_response({"ok": True, "source_id": source_id})

    @staticmethod
    def _extract_token(request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.headers.get("X-Coara-Webhook-Token")
