from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from src.core.logger import logger
from src.core.workspace_layout import UPLOAD_DIR_NAME
from src.ui.handler_contract import HandlerMixinBase
from src.ui.web_upload_limits import (
    BINARY_SUFFIXES as _BINARY_SUFFIXES,
)
from src.ui.web_upload_limits import (
    IMAGE_SUFFIXES as _IMAGE_SUFFIXES,
)
from src.ui.web_upload_limits import (
    MAX_INLINE_FILE_BYTES,
    MAX_UPLOAD_FILE_BYTES,
)
from src.ui.web_upload_limits import (
    OFFICE_SUFFIXES as _OFFICE_SUFFIXES,
)
from src.ui.web_upload_limits import (
    sanitize_upload_filename as _sanitize_upload_filename,
)


class FilesHandlers(HandlerMixinBase):
    async def _handle_recent_files(self, request: web.Request) -> web.Response:
        """最近文件：跨来源按时间倒序（默认本空间，可 scope=all 看全部）。

        Query params:
            workspace: 空间过滤，默认当前 web 视图空间；``all`` = 跨空间全部
            before:    分页游标（上一页最旧一条的 ts），只取早于它的
            limit:     每页条数（默认 30，上限 100）
        """
        self._check_token(request)
        from src.records.recent_files import list_recent_files

        scope = str(request.query.get("workspace") or "").strip()
        if scope == "all":
            ws_id: str | None = None
        elif scope:
            ws_id = scope
        else:
            _cur = getattr(self.root, "web_view_workspace_id", None) or getattr(
                self.root, "_foreground_session_id", None
            )
            ws_id = str(_cur) if _cur else None
        raw_before = request.query.get("before")
        try:
            before = float(raw_before) if raw_before else None
        except ValueError:
            before = None
        try:
            limit = max(1, min(100, int(request.query.get("limit", "30"))))
        except ValueError:
            limit = 30
        if self.coara_home is None:
            return web.json_response({"files": [], "has_more": False})
        entries, has_more = await asyncio.to_thread(
            list_recent_files, self.coara_home, workspace_id=ws_id, before_ts=before, limit=limit
        )
        return web.json_response({"files": entries, "has_more": has_more})

    def _resolve_upload_path(self, ref: str) -> Path | None:
        """Resolve a sanitized upload ref under ``<workspace>/uploads``; None if invalid."""
        uploads_dir = self.workspace_dir / UPLOAD_DIR_NAME
        uploads_root = uploads_dir.resolve()
        safe_ref = Path(ref).name
        path = (uploads_dir / safe_ref).resolve()
        try:
            path.relative_to(uploads_root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path

    async def _handle_upload(self, request: web.Request) -> web.Response:
        """Handle multipart file upload (images for vision, attachments)."""
        self._check_token(request)
        home = self.coara_home  # 只为记录最近文件；为空就跳过索引，不影响上传本身
        reader = await request.multipart()
        uploads_dir = self.workspace_dir / UPLOAD_DIR_NAME
        uploads_dir.mkdir(parents=True, exist_ok=True)

        uploaded = []
        async for part in reader:
            if getattr(part, "name", None) != "file":
                continue
            filename = getattr(part, "filename", None) or f"upload_{uuid.uuid4().hex[:8]}"
            # Sanitize filename (Win32 reserved names / trailing dots / spaces)
            safe_name = _sanitize_upload_filename(filename)
            dest = uploads_dir / safe_name
            # Avoid collision
            counter = 1
            while dest.exists():
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            with open(dest, "wb") as f:
                received = 0
                too_large = False
                while True:
                    chunk = await part.read_chunk(65536)  # type: ignore[union-attr]  # aiohttp 存根的迭代元素是并集
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_UPLOAD_FILE_BYTES:
                        too_large = True
                        break
                    f.write(chunk)
            if too_large:
                dest.unlink(missing_ok=True)
                return web.json_response({"error": "file too large (max 50MB)"}, status=413)
            uploaded.append(
                {
                    "filename": dest.name,
                    "size": dest.stat().st_size,
                    "ref": dest.name,
                }
            )
            # 最近文件索引：你发我的文件记一条（inbound / web）。
            try:
                from src.records.recent_files import record_recent_file

                if home is not None:
                    _ws = getattr(self.root, "web_view_workspace_id", None) or getattr(
                        self.root, "_foreground_session_id", None
                    )
                    record_recent_file(
                        home,
                        origin="inbound",
                        end="web",
                        name=dest.name,
                        path=str(dest.resolve()),
                        mime=mimetypes.guess_type(dest.name)[0] or "",
                        size=dest.stat().st_size,
                        workspace_id=str(_ws) if _ws else None,
                    )
            except Exception as exc:  # noqa: BLE001 — 索引故障不影响上传
                logger.warning(f"recent_files record failed (web upload): {exc}")
        return web.json_response({"files": uploaded})

    async def _append_text_file_refs(self, text: str, refs: list[str]) -> str:
        """Inline uploaded files into the user message (not vision blocks).

        与 read 工具的文本提取能力对齐：文本照常内联；Office/PDF 复用读取器
        提取成文本内联；纯二进制（zip/exe 等无可提取文本）只给绝对路径提示，
        模型需要时用 read/shell 处理。磁盘读 + 解码跑 worker 线程，不阻塞事件循环。
        """
        parts: list[str] = [text.rstrip()]
        for ref in refs:
            path = self._resolve_upload_path(ref)
            if path is None:
                logger.warning(f"File ref not found: {ref}")
                continue
            content = await asyncio.to_thread(self._extract_upload_text, path)
            if content is None:
                # 无可提取文本的二进制：给绝对路径提示，模型经工具读取/处理。
                parts.append(
                    f"\n\n--- 附件: {path.name} ---\n（二进制文件，位于 {path.resolve()}，可用 read/shell 工具处理）"
                )
                continue
            if len(content) > self._MAX_TEXT_FILE_CHARS:
                content = content[: self._MAX_TEXT_FILE_CHARS] + "\n…（附件已截断）"
            parts.append(f"\n\n--- 附件: {path.name} ---\n{content}")
        return "\n".join(parts).strip() if len(parts) > 1 else text

    def _extract_upload_text(self, path: Path) -> str | None:
        """提取上传文件的可读文本；纯二进制返回 None。同步（经 to_thread 调用）。"""
        suffix = path.suffix.lower()
        # Office / PDF：复用文档读取器提取为 Markdown-ish 文本（与 read 工具一致）。
        if suffix in _OFFICE_SUFFIXES or suffix == ".pdf":
            try:
                from src.tools.builtin.file_io.excel_reader import read_excel_workbook
                from src.tools.builtin.file_io.pdf_reader import read_pdf_document
                from src.tools.builtin.file_io.pptx_reader import read_pptx_presentation
                from src.tools.builtin.file_io.word_reader import read_word_document

                readers = {
                    ".docx": read_word_document,
                    ".xlsx": read_excel_workbook,
                    ".pptx": read_pptx_presentation,
                    ".pdf": read_pdf_document,
                }
                content, _meta = readers[suffix](path)
                return content
            except Exception as exc:
                logger.warning(f"Failed to extract Office/PDF text {path.name}: {exc}")
                return None
        # 纯二进制（压缩/可执行/音视频/数据库）：无可提取文本，返回 None 走路径提示。
        if suffix in _BINARY_SUFFIXES:
            return None
        # 其余按文本尝试解码；解码失败（NUL 字节/大量替换符）视为二进制。
        try:
            raw = path.read_bytes()
        except Exception as exc:
            logger.warning(f"Failed to read file ref {path.name}: {exc}")
            return None
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content = raw.decode("utf-8", errors="replace")
            except Exception:
                return None
        sample = content[:65536]
        if "\x00" in sample or sample.count("\ufffd") > max(1, len(sample) // 100):
            return None
        return content

    def _build_upload_attachments(
        self, image_refs: list[str], file_refs: list[str]
    ) -> list[dict[str, Any]]:
        """组装上传附件的展示元数据（气泡渲染 + 视图回放）。

        图片 is_image=True（前端渲染缩略图），其余为文件卡片。url 由前端按
        ref 经 workspace/file-raw 生成，这里不带。只收录磁盘上真实存在的引用。
        workspace_dir 缺失（测试夹具 / 未绑定）时返回空，不阻塞回合。
        """
        if getattr(self, "workspace_dir", None) is None:
            return []
        attachments: list[dict[str, Any]] = []
        for ref in image_refs:
            path = self._resolve_upload_path(ref)
            if path is None:
                continue
            attachments.append(
                {
                    "ref": path.name,
                    "filename": path.name,
                    "size": path.stat().st_size,
                    "mime": mimetypes.guess_type(path.name)[0] or "",
                    "is_image": True,
                }
            )
        for ref in file_refs:
            path = self._resolve_upload_path(ref)
            if path is None:
                continue
            attachments.append(
                {
                    "ref": path.name,
                    "filename": path.name,
                    "size": path.stat().st_size,
                    "mime": mimetypes.guess_type(path.name)[0] or "",
                    "is_image": False,
                }
            )
        return attachments

    async def _resolve_image_refs(self, refs: list[str]) -> list[dict[str, Any]]:
        """Resolve image references (uploaded via REST) to vision image blocks.

        Non-image uploads are skipped (use ``file_refs`` for text attachments).
        """
        from src.matrix_client.remote_vision import is_image_upload

        blocks: list[dict[str, Any]] = []
        for ref in refs:
            path = self._resolve_upload_path(ref)
            if path is None:
                logger.warning(f"Image ref not found: {ref}")
                continue
            mime = mimetypes.guess_type(path.name)[0]
            if not is_image_upload(mime, path.name):
                logger.warning(f"Skipping non-image ref in image_refs: {ref}")
                continue
            try:
                import base64

                from src.utils.multimodal_content import (
                    image_block_from_base64,
                    image_bytes_blocks_with_notice,
                )

                def _render(raw: bytes, _mime: str | None = mime, _path: Path | None = path) -> list[dict[str, Any]]:
                    try:
                        return image_bytes_blocks_with_notice(raw, "high")
                    except Exception:
                        # Pillow 无法处理的图（损坏/异常）→ 小图回退原始 base64 不丢图；
                        # 大图放弃并提示（原样回退会把巨串永久驻留上下文，token 爆炸）。
                        if len(raw) > self._FALLBACK_RAW_IMAGE_MAX_BYTES:
                            logger.warning(
                                f"Image {_path.name if _path else ''} unreadable and too large "
                                f"({len(raw)} bytes), dropped instead of raw base64 fallback"
                            )
                            return []
                        _mt = _mime if (_mime and _mime.startswith("image/")) else "image/png"
                        return [
                            image_block_from_base64(
                                base64.b64encode(raw).decode("ascii"), _mt
                            )
                        ]

                # 读文件 + 压缩/Base64 均须脱离事件循环（50MB-class 读取）。
                raw_bytes = await asyncio.to_thread(path.read_bytes)
                new_blocks = await asyncio.to_thread(_render, raw_bytes)
                blocks.extend(new_blocks)
            except Exception as exc:
                logger.warning(f"Failed to read image {ref}: {exc}")
        return blocks

    # ------------------------------------------------------------------
    # Slash command handler
    # ------------------------------------------------------------------

    async def _handle_outbound_file(self, request: web.Request) -> web.StreamResponse:
        """Serve a file previously pushed via ``send_file`` (token required)."""
        self._check_token(request)
        file_id = str(request.match_info.get("file_id") or "").strip()
        bridge = self._web_file_bridge
        if bridge is None or not file_id:
            return web.Response(status=404, text="not found")
        path = bridge.resolve_file(file_id)
        if path is None:
            return web.Response(status=404, text="not found")
        import mimetypes

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Prefer original filename from stem after uuid — stored as {id}{suffix}.
        # Content-Disposition uses the on-disk name; chat UI already has filename.
        headers = {
            "Content-Type": mime,
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        }
        return web.FileResponse(path, headers=headers)

    def _persist_outbound_file(self, attachment: dict[str, Any], owner: Any | None = None) -> None:
        """Web 投递附件落盘：录像带（冷备/审计）+ web 会话视图存储（聊天区恢复卡片）。

        **归属只认发起者**（*owner*：workspace_dir / session_id / turn_id，由 send_file
        调用经 ``bind_runtime_context`` 带出）。绝不读 ``_view_coara()`` / ``self.workspace_dir``：
        投递可能异步完成（视频要等几分钟），期间用户切了空间，按「当时的视图」落带
        就会把产出写进别人的线（实测事故：nx 请求的视频落进 v8 线）。

        归属不完整时**宁可不落带**（记 warning）——跨空间落带正是这次的病根，绝不
        用「大概是当前空间」兜底。
        """
        workspace_dir = str(getattr(owner, "workspace_dir", "") or "")
        session_id = str(getattr(owner, "session_id", "") or "")
        turn_id = str(getattr(owner, "turn_id", "") or "")
        if not workspace_dir or not session_id:
            logger.warning(
                "[Web] outbound file 缺发起者归属（workspace_dir/session_id），跳过落带："
                f"{attachment.get('filename', '')}"
            )
            return
        caption = str(attachment.get("caption") or "")
        files_payload = [
            {
                "file_id": attachment.get("file_id", ""),
                "url": attachment.get("url", ""),
                "filename": attachment.get("filename", ""),
                "mime": attachment.get("mime", ""),
                "size": int(attachment.get("size") or 0),
                "caption": caption,
                "is_image": bool(attachment.get("is_image")),
                "is_video": bool(attachment.get("is_video")),
                "is_audio": bool(attachment.get("is_audio")),
            }
        ]
        # 录像带用发起者自己的 recorder（该会话的事件带），查不到就只落聊天区
        recorder = self._recorder_for_session(session_id)
        if recorder is not None:
            with contextlib.suppress(Exception):
                recorder.record_files(files=files_payload, turn_id=turn_id, source="web")
        # 视图带：web 聊天区刷新恢复的数据源，走内核录制器（唯一落带入口）。
        try:
            from src.ui.view_recorder import record_view_frame

            record_view_frame(
                {
                    "kind": "files",
                    "turn_id": turn_id,
                    "source": "web",
                    "subject": "root",
                    "session_id": session_id,
                    "workspace_dir": str(workspace_dir or ""),
                    "payload": {"files": files_payload, "caption": caption},
                },
                coara_home=self.coara_home,
            )
        except Exception as exc:  # noqa: BLE001 — 视图落盘绝不中断投递
            logger.warning(f"view files persist failed: {exc}")

    def _recorder_for_session(self, session_id: str) -> Any | None:
        """按会话 id 只读查该会话的录像带 recorder（发起者自己的 ``_session_log``）。"""
        if not session_id:
            return None
        sessions = getattr(getattr(self, "root", None), "_sessions", None)
        if not isinstance(sessions, dict):
            return None
        for session in sessions.values():
            coara = getattr(session, "coara", None)
            if coara is not None and str(getattr(coara, "session_id", "") or "") == session_id:
                return getattr(coara, "_session_log", None)
        return None

    def _resolve_safe_path(self, raw_path: str, ws_root: Path) -> Path | None:
        """Resolve a path relative to workspace root, rejecting escapes."""
        raw_path = raw_path.strip()
        if not raw_path or raw_path == ".":
            return ws_root

        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve()
                resolved.relative_to(ws_root)
                return resolved
            except ValueError:
                return None

        try:
            resolved = (ws_root / raw_path).resolve()
            resolved.relative_to(ws_root)
            return resolved
        except ValueError:
            return None

    # 绝对路径读文件的敏感禁区：命中即拒（防持 token 读密钥/凭据）。
    _SENSITIVE_PATH_PARTS = (".ssh", ".gnupg", ".aws", ".kube", ".docker", "id_rsa", "id_ed25519")
    _SENSITIVE_FILE_NAMES = (".env", ".netrc", ".htpasswd")

    def _is_sensitive_path(self, target: Path) -> bool:
        parts = {p.lower() for p in target.parts}
        if any(s in parts for s in self._SENSITIVE_PATH_PARTS):
            return True
        name = target.name.lower()
        return name in self._SENSITIVE_FILE_NAMES or name.endswith((".pem", ".key", ".pfx", ".p12", ".kdbx"))

    def _resolve_file_target(self, raw_path: str, ws_root: Path) -> Path | None:
        """Resolve a file path for the read-only file endpoints.

        Relative paths stay confined to the workspace via _resolve_safe_path;
        absolute paths are accepted for inline preview of files the agent
        references (CLI tool output links), except sensitive locations
        (.ssh/.env/keys) which are refused. Returns None when a relative path
        escapes or an absolute path is sensitive.
        """
        if Path(raw_path.strip()).is_absolute():
            resolved = Path(raw_path.strip()).resolve()
            if self._is_sensitive_path(resolved):
                return None
            return resolved
        return self._resolve_safe_path(raw_path, ws_root)

    async def _handle_workspace_file(self, request: web.Request) -> web.Response:
        """Read a file's content for the web file viewer.

        Query params:
            path: Relative path from workspace root, or an absolute path.
            offset: Starting line number for text files (1-based, default 1).
            limit: Max lines to read for text files (default 500).
        """
        self._check_token(request)
        raw_path = request.query.get("path", "")
        try:
            offset = int(request.query.get("offset", "1"))
        except ValueError:
            offset = 1
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500

        if not raw_path:
            return web.json_response({"error": "path 参数不能为空"}, status=400)

        ws_root = self._view_coara().workspace_dir.resolve()
        target = self._resolve_file_target(raw_path, ws_root)
        if target is None:
            return web.json_response({"error": f"路径不在工作区范围内: {raw_path}"}, status=403)
        if target.is_dir():
            return self._directory_listing_response(target, raw_path)
        if not target.is_file():
            return web.json_response({"error": f"不是文件: {raw_path}"}, status=400)

        try:
            file_size = target.stat().st_size
        except OSError as exc:
            return web.json_response({"error": f"无法读取文件: {exc}"}, status=500)

        truncated = file_size > MAX_INLINE_FILE_BYTES
        name = target.name
        suffix = target.suffix.lower()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        try:
            if suffix in _IMAGE_SUFFIXES:
                # Return image as base64 for inline preview
                import base64

                data = target.read_bytes()[:MAX_INLINE_FILE_BYTES]
                return web.json_response(
                    {
                        "path": raw_path,
                        "name": name,
                        "type": "image",
                        "mime": mime,
                        "data": base64.b64encode(data).decode("ascii"),
                        "size": file_size,
                        "truncated": truncated,
                    }
                )

            if suffix in _OFFICE_SUFFIXES:
                return self._office_file_response(target, raw_path, name, file_size, truncated)

            if suffix in _BINARY_SUFFIXES:
                return web.json_response(
                    {"path": raw_path, "name": name, "type": "binary", "mime": mime, "size": file_size}
                )

            # Try as text; downgrade to binary on NUL bytes or heavy replacement chars.
            text = target.read_text(encoding="utf-8", errors="replace")
            sample = text[:65536]
            if "\x00" in sample or sample.count("�") > max(1, len(sample) // 100):
                return web.json_response(
                    {"path": raw_path, "name": name, "type": "binary", "mime": mime, "size": file_size}
                )
            lines = text.split("\n")
            total_lines = len(lines)
            start = max(0, offset - 1)
            end = min(start + limit, total_lines)
            content = "\n".join(lines[start:end])
            return web.json_response(
                {
                    "path": raw_path,
                    "name": name,
                    "type": "text",
                    "language": suffix[1:] if suffix else "",
                    "content": content,
                    "total_lines": total_lines,
                    "offset": offset,
                    "limit": limit,
                    "has_more": end < total_lines,
                    "size": file_size,
                    "truncated": truncated,
                }
            )
        except Exception as exc:
            return web.json_response({"error": f"读取失败: {exc}"}, status=500)

    def _directory_listing_response(self, target: Path, raw_path: str) -> web.Response:
        """List a directory for the file viewer (type=directory)."""
        max_entries = 500
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return web.json_response({"error": f"无法读取目录: {exc}"}, status=500)
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                break
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else int(child.stat().st_size)
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if is_dir else "file",
                    "size": size,
                }
            )
        return web.json_response(
            {
                "path": raw_path,
                "name": target.name or str(target),
                "type": "directory",
                "entries": entries,
                "size": 0,
                "truncated": truncated,
            }
        )

    def _office_file_response(
        self, target: Path, raw_path: str, name: str, file_size: int, truncated: bool
    ) -> web.Response:
        """Extract an Office document (.docx/.xlsx/.pptx) as markdown-ish text."""
        from src.tools.builtin.file_io.excel_reader import read_excel_workbook
        from src.tools.builtin.file_io.pptx_reader import read_pptx_presentation
        from src.tools.builtin.file_io.word_reader import read_word_document

        readers = {
            ".docx": read_word_document,
            ".xlsx": read_excel_workbook,
            ".pptx": read_pptx_presentation,
        }
        try:
            content, _meta = readers[target.suffix.lower()](target)
        except Exception as exc:
            return web.json_response({"error": f"读取 Office 文档失败: {exc}"}, status=500)
        if len(content) > MAX_INLINE_FILE_BYTES:
            content = content[:MAX_INLINE_FILE_BYTES]
            truncated = True
        return web.json_response(
            {
                "path": raw_path,
                "name": name,
                "type": "office",
                "content": content,
                "size": file_size,
                "truncated": truncated,
            }
        )

    async def _handle_workspace_file_raw(self, request: web.Request) -> web.StreamResponse:
        """Stream the raw file for inline preview (PDF/audio/video) or download.

        Query params:
            path: Relative path from workspace root, or an absolute path.
            download: "1" forces Content-Disposition: attachment.
        """
        self._check_token(request)
        raw_path = request.query.get("path", "")
        if not raw_path:
            return web.json_response({"error": "path 参数不能为空"}, status=400)

        ws_root = self._view_coara().workspace_dir.resolve()
        target = self._resolve_file_target(raw_path, ws_root)
        if target is None:
            return web.json_response({"error": f"路径不在工作区范围内: {raw_path}"}, status=403)
        if not target.is_file():
            return web.json_response({"error": f"不是文件: {raw_path}"}, status=400)

        from urllib.parse import quote

        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        disposition = "attachment" if request.query.get("download") == "1" else "inline"
        headers = {
            "Content-Type": mime,
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(target.name, safe='')}",
            "X-Content-Type-Options": "nosniff",
        }
        return web.FileResponse(target, headers=headers)

    @staticmethod
    def _normalize_tool_ws_fields(
        event_type: str,
        target: dict[str, Any],
        source: dict[str, Any] | None = None,
    ) -> None:
        """Map trace payload names onto the browser tool-activity protocol."""
        if event_type not in {"tool_start", "tool_call", "tool_complete", "tool_result"}:
            return
        src = source or {}
        if "tool" not in target:
            tool_name = target.get("tool_name") or src.get("tool_name") or src.get("tool")
            if tool_name:
                target["tool"] = tool_name
        if "call_id" not in target:
            call_id = target.get("tool_call_id") or src.get("call_id") or src.get("tool_call_id")
            if call_id:
                target["call_id"] = call_id
        if "args" not in target:
            args = target.get("arguments") or src.get("arguments")
            if args is not None:
                target["args"] = args
