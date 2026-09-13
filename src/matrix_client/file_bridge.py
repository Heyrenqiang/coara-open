"""Matrix file upload bridge for coara tools."""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from src.matrix_client.media_inbound import MAX_MATRIX_UPLOAD_BYTES
from src.matrix_client.remote_vision import is_image_upload
from src.matrix_client.send_guard import matrix_room_send_content

EnsureJoinedFn = Callable[[str], Awaitable[bool]]


class MatrixFileBridge:
    """Small adapter around Matrix media upload + room_send."""

    def __init__(self, *, homeserver: str, client: Any):
        self.homeserver = homeserver.rstrip("/")
        self.client = client
        self.current_room_id: str = ""
        self._ensure_joined: EnsureJoinedFn | None = None
        # 常驻组件共享一个会话：惰性创建（须在事件循环内），aclose 统一关闭
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def aclose(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    def set_current_room(self, room_id: str) -> None:
        self.current_room_id = room_id

    def set_ensure_joined(self, fn: EnsureJoinedFn | None) -> None:
        self._ensure_joined = fn

    async def send_file(
        self,
        path: Path,
        *,
        caption: str = "",
        room_id: str = "",
        owner: Any | None = None,
    ) -> dict[str, Any]:
        """Upload one file to the target Matrix room.

        ``owner``（Web 桥用的发起者归属）对 Matrix 无意义：房间就是归属，忽略。
        """
        del owner
        explicit_room = str(room_id or "").strip()
        if not explicit_room:
            from src.coara.turn_context import get_turn_channel_id

            explicit_room = get_turn_channel_id() or ""
        target_room = explicit_room or self.current_room_id
        if not target_room:
            raise RuntimeError(
                "No remote channel is active. Send a message from the remote client first or pass room_id."
            )
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > MAX_MATRIX_UPLOAD_BYTES:
            limit_mb = MAX_MATRIX_UPLOAD_BYTES // (1024 * 1024)
            raise RuntimeError(
                f"文件 {size / (1024 * 1024):.1f}MB 超过手机端单文件上限 {limit_mb}MB，未发送。"
                "不要改发 Web 端或其它端：文件只发到用户当前说话的端。"
                "请直接告诉用户文件过大，由其决定压缩后重发还是换端获取。"
            )

        content_uri = await self._upload(path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = caption.strip() or path.name
        is_image = is_image_upload(mime_type, path.name)
        content: dict[str, Any] = {
            "msgtype": "m.image" if is_image else "m.file",
            "body": body,
            "url": content_uri,
            "info": {
                "mimetype": mime_type,
                "size": path.stat().st_size,
            },
        }
        content["filename"] = path.name

        ensure_joined = (
            (lambda: self._ensure_joined(target_room))  # type: ignore[misc]
            if self._ensure_joined is not None
            else None
        )
        ok = await matrix_room_send_content(
            self.client,
            target_room,
            content,
            ensure_joined=ensure_joined,
        )
        if not ok:
            raise RuntimeError(f"Matrix room_send failed for {path.name} in {target_room}")

        return {
            "room_id": target_room,
            "mxc_uri": content_uri,
            "filename": path.name,
            "send_result": "ok",
        }

    async def _upload(self, path: Path) -> str:
        token = getattr(self.client, "access_token", "")
        if not token:
            raise RuntimeError("Matrix client is not logged in.")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": mime_type,
        }
        params = {"filename": path.name}
        endpoints = (
            f"{self.homeserver}/_matrix/media/v3/upload",
            f"{self.homeserver}/_matrix/media/r0/upload",
        )
        last_error: str | None = None
        session = self._http()
        for url in endpoints:
            with path.open("rb") as handle:
                async with session.post(url, headers=headers, params=params, data=handle) as response:
                    payload = await response.json(content_type=None)
                    if response.status not in (200, 201):
                        last_error = f"{response.status} {payload}"
                        continue
                    content_uri = payload.get("content_uri")
                    if not content_uri:
                        raise RuntimeError(f"Matrix upload response missing content_uri: {payload}")
                    return str(content_uri)
        raise RuntimeError(f"Matrix upload failed: {last_error or 'unknown error'}")
