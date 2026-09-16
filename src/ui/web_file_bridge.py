"""Web outbound file bridge — copy into a serveable dir and push a WS event."""

from __future__ import annotations

import contextlib
import mimetypes
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.core.logger import logger
from src.matrix_client.remote_vision import is_image_upload
from src.ui.web_socket_registry import WebSocketRegistry

# Soft cap so a runaway copy cannot fill the disk from one call.
_MAX_OUTBOUND_BYTES = 200 * 1024 * 1024
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _owner_fields(owner: Any | None) -> dict[str, str]:
    """发起者归属字段（非空才带）：端上据此判空间，不匹配即丢弃/缓冲。"""
    if owner is None:
        return {}
    fields = {
        "workspace_dir": str(getattr(owner, "workspace_dir", "") or ""),
        "session_id": str(getattr(owner, "session_id", "") or ""),
        "turn_id": str(getattr(owner, "turn_id", "") or ""),
    }
    return {key: value for key, value in fields.items() if value}


class WebFileBridge:
    """Deliver a local file to the active browser tab.

    Files are copied under ``delivery_dir`` with an opaque id, then announced
    over WebSocket as ``{"type": "file", ...}``. The HTTP handler serves them
    via ``/api/outbound-files/{file_id}`` (token-checked).

    Optional ``persist`` writes an ``assistant/files`` event to the L1 session
    event tape so chat hydrate (page switch / reload) restores the attachment.
    """

    def __init__(
        self,
        *,
        registry: WebSocketRegistry,
        delivery_dir: Path,
        persist: Callable[[dict[str, Any]], None] | None = None,
        coara_home: Path | None = None,
        workspace_id: str | None = None,
        workspace_id_getter: Callable[[], str | None] | None = None,
    ) -> None:
        self._registry = registry
        self._delivery_dir = delivery_dir
        self._delivery_dir.mkdir(parents=True, exist_ok=True)
        # file_id → absolute path on disk (in-memory index for the HTTP handler).
        self._files: dict[str, Path] = {}
        # 目录内容惰性索引（iterdir() 返回序），只用于 resolve_file 的前缀兜底段；
        # 以目录签名（count + mtime_ns）失效重建，不碰精确命中路径。
        self._dir_index: list[Path] | None = None
        self._dir_index_signature: tuple[int, int] | None = None
        self._persist = persist
        self._coara_home = coara_home
        self._workspace_id = workspace_id
        # 发送时动态解析当前 web 视图空间（构造时可能尚未绑定视图）。
        self._workspace_id_getter = workspace_id_getter

    @property
    def delivery_dir(self) -> Path:
        return self._delivery_dir

    def resolve_file(self, file_id: str) -> Path | None:
        """Return the on-disk path for a previously delivered file id.

        Falls back to scanning ``delivery_dir`` so files remain servable after
        process restart (or when the in-memory index was cleared).
        """
        key = str(file_id or "").strip()
        if not _FILE_ID_RE.fullmatch(key):
            return None
        path = self._files.get(key)
        if path is not None and path.is_file():
            return self._contained(path)

        exact = self._delivery_dir / key
        if exact.is_file():
            self._files[key] = exact
            return self._contained(exact)
        matches = sorted(self._delivery_dir.glob(f"{key}.*"))
        for candidate in matches:
            if candidate.is_file():
                self._files[key] = candidate
                return self._contained(candidate)
        if not self._delivery_dir.is_dir():
            return None
        for candidate in self._indexed_delivery_files():
            if candidate.is_file() and candidate.name.startswith(key):
                self._files[key] = candidate
                return self._contained(candidate)
        return None

    def _indexed_delivery_files(self) -> list[Path]:
        """投递目录内容快照，目录签名（count + mtime_ns）变化时重建。

        前缀兜底与现状 iterdir() 同序——索引直接记录 iterdir 返回序，
        命中优先级与逐次扫描完全一致。
        """
        signature = self._delivery_signature()
        if self._dir_index is not None and signature == self._dir_index_signature:
            return self._dir_index
        entries = list(self._delivery_dir.iterdir())
        self._dir_index = entries
        self._dir_index_signature = signature
        return entries

    def _delivery_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._delivery_dir.stat()
        except OSError:
            return None
        count = 0
        for _ in self._delivery_dir.iterdir():
            count += 1
        return count, stat.st_mtime_ns

    def _contained(self, path: Path) -> Path | None:
        try:
            path.resolve().relative_to(self._delivery_dir.resolve())
        except ValueError:
            return None
        return path

    async def send_file(
        self,
        path: Path,
        *,
        caption: str = "",
        room_id: str = "",
        owner: Any | None = None,
    ) -> dict[str, Any]:
        """Copy + announce one file to the browser.

        ``owner``（发起者归属：workspace_dir/session_id/turn_id）随 WS 帧与落带
        一起下发——投递可能异步完成，端上按它判空间（不匹配即丢弃/缓冲），视图
        线也按它落（不再读「当时的前台视图」）。
        """
        del room_id  # Matrix-only; Web uses the active WS connection.
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(str(path))

        size = path.stat().st_size
        if size > _MAX_OUTBOUND_BYTES:
            raise RuntimeError(f"文件过大（{size} 字节），Web 端发送上限为 {_MAX_OUTBOUND_BYTES} 字节")

        if not self._registry.has_active():
            raise RuntimeError("没有活跃的 Web 连接。请先在浏览器打开 Web UI。")

        file_id = uuid4().hex
        suffix = path.suffix.lower()[:16]
        dest = self._delivery_dir / f"{file_id}{suffix}"
        try:
            shutil.copy2(path, dest)
        except OSError as exc:
            raise RuntimeError(f"复制文件到 Web 投递目录失败：{exc}") from exc

        self._files[file_id] = dest
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        is_image = is_image_upload(mime_type, path.name)
        is_video = mime_type.startswith("video/")
        is_audio = mime_type.startswith("audio/")
        url = f"/api/outbound-files/{file_id}"
        caption_text = (caption or "").strip()
        attachment = {
            "file_id": file_id,
            "url": url,
            "filename": path.name,
            "mime": mime_type,
            "size": size,
            "caption": caption_text,
            "is_image": is_image,
            "is_video": is_video,
            "is_audio": is_audio,
        }
        # 帧带发起者归属：端侧边界守卫据此判断这帧属不属于当前看的空间（不匹配
        # 就丢弃/缓冲），而不是盲收当前连接上的任何文件卡。
        body = {"type": "file", **attachment, **_owner_fields(owner)}
        ok = await self._registry.send_to_active(body)
        if not ok:
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            self._files.pop(file_id, None)
            raise RuntimeError(f"向浏览器推送文件失败：{path.name}")

        if self._persist is not None:
            try:
                self._persist(attachment, owner)
            except Exception as exc:
                logger.warning(f"[Web] Failed to persist outbound file for hydrate: {exc}")

        # 最近文件索引：我发你的文件记一条（outbound / web），按发起者空间归属；
        # 归属缺失时退回当前视图空间（旧行为，仅用于索引展示，不涉及落线）。
        if self._coara_home is not None:
            try:
                from src.core.coara_home import workspace_id_for
                from src.records.recent_files import record_recent_file

                ws_id = ""
                owner_dir = str(getattr(owner, "workspace_dir", "") or "")
                if owner_dir:
                    with contextlib.suppress(Exception):
                        ws_id = workspace_id_for(Path(owner_dir))
                if not ws_id:
                    ws_id = (
                        self._workspace_id_getter()
                        if self._workspace_id_getter is not None
                        else self._workspace_id
                    )
                record_recent_file(
                    self._coara_home,
                    origin="outbound",
                    end="web",
                    name=path.name,
                    path=str(dest.resolve()),
                    mime=mime_type,
                    size=size,
                    workspace_id=ws_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Web] recent_files record failed: {exc}")

        logger.info(f"[Web] Outbound file delivered: {path.name} → {file_id}")
        return {
            "channel": "web",
            "file_id": file_id,
            "url": url,
            "filename": path.name,
            "mime": mime_type,
            "size": size,
            "send_result": "ok",
        }
