"""最近文件索引：跨来源、按时间倒序的收发流水。

登记「什么时候、哪个端、哪个空间、碰了哪个文件、它现在在哪」。只记索引，
不复制文件——各类文件仍待在原处（outbound_files / uploads / 工作空间内）。

存储：``<coara_home>/users/default/recent_files.jsonl``（全局一份，append-only）。
全量流水，不按时间过期；到物理上限才轮转丢最旧（纯防爆）。读取时校验文件
存在性，过滤死链。三端各读这条流，按空间过滤，默认显示最近窗口，可翻全部。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from src.core.json_store import write_text_atomic
from src.core.logger import logger

# 物理上限（防爆）：超了轮转丢最旧。正常几年用不到。
_MAX_INDEX_BYTES = 10 * 1024 * 1024
_TAIL_READ_BYTES = 512 * 1024

_FILENAME = "recent_files.jsonl"

_write_lock = threading.Lock()


def _index_path(coara_home: Path) -> Path:
    return Path(coara_home).expanduser().resolve() / "users" / "default" / _FILENAME


def _kind_for(mime: str, name: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime in ("application/pdf",) or mime.startswith("text/"):
        return "doc"
    ext = Path(name or "").suffix.lower()
    if ext in {".md", ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"}:
        return "doc"
    _code_exts = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
        ".c", ".cpp", ".h", ".json", ".yaml", ".yml", ".toml", ".html",
        ".css", ".sh", ".ps1",
    }
    if ext in _code_exts:
        return "code"
    return "other"


def record_recent_file(
    coara_home: Path,
    *,
    origin: str,
    end: str,
    name: str,
    path: str,
    mime: str = "",
    size: int = 0,
    workspace_id: str | None = None,
) -> None:
    """追加一条最近文件索引。失败只记日志，绝不中断主流程（文件收发本身已成功）。

    origin: outbound（我发你）/ inbound（你发我）
    end:    web / cli / matrix（触发端）
    path:   磁盘绝对路径（读取时校验存在性，过滤死链）
    """
    try:
        idx = _index_path(coara_home)
        idx.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "origin": origin,
            "end": end,
            "workspace_id": workspace_id,
            "name": name,
            "mime": mime,
            "size": int(size or 0),
            "path": path,
            "kind": _kind_for(mime, name),
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _write_lock:
            with idx.open("a", encoding="utf-8") as fh:
                fh.write(line)
            _maybe_rotate(idx)
    except Exception as exc:  # noqa: BLE001 — 索引故障不影响文件收发
        logger.warning(f"recent_files record failed ({origin}/{name}): {exc}")


def _maybe_rotate(idx: Path) -> None:
    try:
        if idx.stat().st_size <= _MAX_INDEX_BYTES:
            return
        lines = idx.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        write_text_atomic(idx, "".join(lines[len(lines) // 2:]))
    except OSError as exc:
        logger.warning(f"recent_files rotate failed: {exc}")


def list_recent_files(
    coara_home: Path,
    *,
    workspace_id: str | None = None,
    before_ts: float | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    """按时间倒序返回最近文件（过滤死链）。``before_ts`` 分页：只取早于它的。

    返回 (entries, has_more)。``workspace_id=None`` 表示跨空间全部。
    """
    idx = _index_path(coara_home)
    try:
        size = idx.stat().st_size
    except OSError:
        return [], False
    if size == 0:
        return [], False
    try:
        with idx.open("rb") as fh:
            fh.seek(max(0, size - _TAIL_READ_BYTES))
            tail = fh.read()
    except OSError:
        return [], False

    out: list[dict[str, Any]] = []
    for raw in reversed(tail.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # 窗口首行可能截断：跳过
        if not isinstance(entry, dict):
            continue
        ts = float(entry.get("ts") or 0.0)
        if before_ts is not None and ts >= before_ts:
            continue
        if workspace_id is not None and entry.get("workspace_id") != workspace_id:
            continue
        # 死链过滤：文件本体已被清理的索引不再展示。
        path = str(entry.get("path") or "")
        if not path or not Path(path).is_file():
            continue
        out.append(entry)
        if len(out) >= limit:
            return out, True
    return out, False
