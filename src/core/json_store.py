"""Atomic JSON file writes shared by persistence stores."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from src.core.logger import logger

T = TypeVar("T")


def write_json_atomic(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write JSON to *path* via a same-directory temp file + replace."""
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=indent))


def _replace_with_dir_fsync(temp_path: Path, path: Path) -> None:
    """``os.replace`` 后补目录项 fsync 保证 rename 本身落盘"""
    # Windows 下目标文件被进程读占用（如 devtools 读磁盘镜像）时 replace 会抛
    # WinError 5（Access Denied）。这是瞬时共享冲突：有界退避重试即可越过，
    # 避免高频写（每轮 LLM 调用都落盘镜像）因撞上读而丢数据。
    for attempt in range(4):
        try:
            os.replace(temp_path, path)
            break
        except PermissionError as exc:
            if attempt >= 3:
                if path.is_dir():
                    raise IsADirectoryError(
                        f"原子写失败：目标路径是目录而非文件：{path}"
                    ) from exc
                raise PermissionError(
                    f"原子写失败：目标不可写（只读或被占用）：{path}"
                ) from exc
            time.sleep(0.02 * (attempt + 1))
        except OSError as exc:
            # Linux 下目标是目录时 replace 直接抛 EISDIR，同样转成清晰错误
            if path.is_dir():
                raise IsADirectoryError(
                    f"原子写失败：目标路径是目录而非文件：{path}"
                ) from exc
            raise
    # Windows 与部分网络盘不支持目录 fsync 失败不致命 沿用 suppress 容错
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def write_text_atomic(path: Path, text: str) -> None:
    """Write plain text to *path* via a unique same-directory temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # replace 前 fsync 保证内容落盘 网络盘上失败不致命
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        _replace_with_dir_fsync(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write raw bytes to *path* via a unique same-directory temp file + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        _replace_with_dir_fsync(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


class TypedJsonStore(Generic[T]):
    """Load/save ``dict[id, T]`` from a JSON file with atomic writes.

    Corruption guard: when the file cannot be parsed it is renamed to
    ``<name>.corrupt`` and the instance stops saving, so a later ``save``
    can never overwrite unreadable data with an empty collection. The guard
    is an in-memory flag on this instance and lifts only on process restart
    (after the file is repaired or removed).
    """

    def __init__(
        self,
        path: Path,
        *,
        extract_items: Callable[[Any], list[Any]],
        wrap_payload: Callable[[list[dict[str, Any]]], Any],
        parse_record: Callable[[Any], T],
        serialize_records: Callable[[dict[str, T]], list[dict[str, Any]]],
        record_label: str = "record",
    ) -> None:
        self.path = path
        self._extract_items = extract_items
        self._wrap_payload = wrap_payload
        self._parse_record = parse_record
        self._serialize_records = serialize_records
        self._record_label = record_label
        self._corrupted = False

    def load(self) -> dict[str, T]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 仅内容不可解析才隔离 瞬时 OS 错误（文件锁/杀软）不误伤健康文件
            logger.warning(f"Failed to load {self._record_label}s from {self.path}: {exc}")
            self._quarantine_corrupt_file()
            return {}
        except OSError as exc:
            logger.warning(f"Failed to read {self._record_label}s from {self.path}: {exc}")
            return {}
        items = self._extract_items(raw)
        records: dict[str, T] = {}
        for item in items:
            try:
                record = self._parse_record(item)
            except Exception as exc:
                logger.warning(f"Skipping invalid {self._record_label}: {exc}")
                continue
            record_id = getattr(record, "id", None)
            if not record_id:
                logger.warning(f"Skipping {self._record_label} without id")
                continue
            records[str(record_id)] = record
        return records

    def save(self, records: dict[str, T]) -> bool:
        """Persist records; return False when the corruption guard skipped the write"""
        if self._corrupted:
            logger.warning(
                f"Refusing to save {self._record_label}s to {self.path}: "
                "store file was corrupt at load; fix or remove the .corrupt file and restart"
            )
            return False
        serialized = self._serialize_records(records)
        write_json_atomic(self.path, self._wrap_payload(serialized))
        return True

    def _quarantine_corrupt_file(self) -> None:
        """Rename the unreadable file aside and block further saves on this instance"""
        self._corrupted = True
        corrupt_path = self.path.with_name(f"{self.path.name}.corrupt")
        try:
            os.replace(self.path, corrupt_path)
        except OSError as exc:
            logger.warning(f"Failed to quarantine corrupt {self._record_label} store {self.path}: {exc}")
