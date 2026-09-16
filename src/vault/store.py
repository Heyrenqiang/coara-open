"""Sealed object store + plaintext ``open/`` materialization."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import uuid
from pathlib import Path

from src.core.json_store import write_bytes_atomic
from src.core.logger import logger
from src.vault.crypto import decrypt_bytes, encrypt_bytes
from src.vault.errors import VaultError
from src.vault.meta import VaultMeta, create_meta, load_meta, save_meta, unlock_dek
from src.vault.paths import (
    ENTRY_SUFFIX,
    ensure_vault_layout,
    vault_meta_path,
    vault_open_dir,
    vault_sealed_dir,
)
from src.vault.schema import normalize_logical_path

_OPEN_SESSION_MARKER = ".coara_vault_session"


def _pid_alive_windows(pid: int) -> bool:
    """Windows liveness check via OpenProcess/GetExitCodeProcess.

    ``os.kill(pid, 0)`` is unreliable on Windows (and PID reuse can make a
    stale session marker look alive), so query the kernel process state
    directly. Unknown outcomes are treated as alive (fail closed), matching
    the POSIX branch in ``VaultStore._pid_alive``.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806
    STILL_ACTIVE = 259  # noqa: N806
    ERROR_ACCESS_DENIED = 5  # noqa: N806

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 声明 64 位安全原型：restype/argtypes 默认 c_int，会把 64 位 HANDLE
    # 截断成 32 位（高位清零甚至变负），GetExitCodeProcess 拿垃圾句柄恒失败，
    # 陈旧 vault 会话标记被判存活（fail-closed）永远无法解锁。
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but is protected.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class VaultStore:
    def __init__(self, vault_dir: Path):
        self.vault_dir = vault_dir.expanduser().resolve()
        self.meta_path = vault_meta_path(self.vault_dir)
        self.sealed_dir = vault_sealed_dir(self.vault_dir)
        self.open_dir = vault_open_dir(self.vault_dir)

    def ensure_layout(self) -> None:
        ensure_vault_layout(self.vault_dir)

    def is_initialized(self) -> bool:
        return self.meta_path.exists()

    def initialize(self, password: str) -> None:
        self.ensure_layout()
        if self.is_initialized():
            raise ValueError("vault already initialized")
        save_meta(self.meta_path, create_meta(password))

    def close(self) -> None:
        pass

    def load_meta(self) -> VaultMeta:
        return load_meta(self.meta_path)

    def verify_password(self, password: str) -> bytearray:
        return unlock_dek(password, self.load_meta())

    def count_sealed(self) -> int:
        if not self.sealed_dir.is_dir():
            return 0
        return sum(1 for p in self.sealed_dir.glob(f"*{ENTRY_SUFFIX}") if p.is_file())

    def wipe_open(self) -> None:
        if self.open_dir.exists():
            shutil.rmtree(self.open_dir, ignore_errors=True)

    def ensure_open_dir(self) -> Path:
        self.open_dir.mkdir(parents=True, exist_ok=True)
        return self.open_dir

    def _iter_sealed(self) -> list[Path]:
        if not self.sealed_dir.is_dir():
            return []
        return sorted(p for p in self.sealed_dir.glob(f"*{ENTRY_SUFFIX}") if p.is_file())

    def _read_payload(self, path: Path, *, dek: bytes) -> dict[str, object]:
        raw = decrypt_bytes(path.read_bytes(), dek)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"invalid sealed entry: {path}")
        return payload

    def _session_marker_path(self) -> Path:
        return self.open_dir / _OPEN_SESSION_MARKER

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return _pid_alive_windows(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def _assert_can_take_open_session(self) -> None:
        marker = self._session_marker_path()
        if not marker.is_file():
            return
        try:
            pid = int(marker.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            return
        if pid == os.getpid():
            return
        if self._pid_alive(pid):
            raise VaultError(f"refuse unlock: another process (pid={pid}) already holds open/; close it there first")

    def _claim_session_marker(self) -> None:
        """原子占位 open 会话标记（O_EXCL），消除双进程 check-then-act 竞态。

        占位必须先于任何 wipe：落败进程在 wipe 之前就被拒绝，
        先解锁进程窗口内的新写明文不会被对方清掉。
        """
        self.ensure_open_dir()
        marker = self._session_marker_path()
        for _ in range(2):
            try:
                fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # 已有占位：持锁进程存活（或就是本进程旧占位）则按存活检查裁决；
                # 陈旧标记清掉后重试一次，重试再撞车说明有并发赢家
                self._assert_can_take_open_session()
                with contextlib.suppress(OSError):
                    marker.unlink()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return
        raise VaultError("refuse unlock: could not claim open/ session marker")

    def _wipe_open_tree(self, *, preserve: Path) -> None:
        """清空 open/ 内容但保留指定文件（会话标记占位先行，wipe 不得顺带删）。"""
        root = self.open_dir
        if not root.is_dir():
            return
        for child in root.iterdir():
            if child == preserve:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()

    def materialize_open(self, *, dek: bytes) -> Path:
        """Decrypt sealed objects into ``open/`` (replaces previous open tree)."""
        self._assert_can_take_open_session()
        self._claim_session_marker()
        sealed_paths = self._iter_sealed()
        self._wipe_open_tree(preserve=self._session_marker_path())
        root = self.ensure_open_dir()
        ok = 0
        errors = 0
        for path in sealed_paths:
            try:
                payload = self._read_payload(path, dek=dek)
                logical = normalize_logical_path(str(payload.get("path") or ""))
                data = payload.get("data_b64")
                if not isinstance(data, str):
                    errors += 1
                    logger.warning("sealed entry missing data_b64: %s", path.name)
                    continue
                content = base64.b64decode(data.encode("ascii"))
                target = root / Path(*logical.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                ok += 1
            except Exception:
                errors += 1
                logger.exception("failed to materialize sealed entry %s", path.name)
        if sealed_paths and ok == 0:
            self.wipe_open()
            raise VaultError(
                f"failed to decrypt any of {len(sealed_paths)} sealed entries "
                f"({errors} error(s)); ciphertext left unchanged"
            )
        if errors:
            logger.warning("materialize_open: %s ok, %s failed", ok, errors)
        return root.resolve()

    def seal_from_open(self, *, dek: bytes) -> int:
        """Replace sealed/ with encryption of every file under ``open/``.

        Fail closed: never delete ``sealed/`` when ``open/`` is missing.
        An empty ``open/`` (present directory, zero files) is allowed and seals
        to an empty ciphertext set — only call this from a process that owns
        the open session.
        """
        root = self.open_dir
        if not root.is_dir():
            raise VaultError("refuse seal: open/ missing; sealed ciphertext left unchanged")

        staging = self.vault_dir / ".seal_staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        count = 0
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.name == _OPEN_SESSION_MARKER:
                continue
            rel = file_path.relative_to(root).as_posix()
            try:
                logical = normalize_logical_path(rel)
            except ValueError:
                continue
            object_id = uuid.uuid4().hex
            payload = {
                "v": 3,
                "path": logical,
                "data_b64": base64.b64encode(file_path.read_bytes()).decode("ascii"),
            }
            blob = encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), dek)
            (staging / f"{object_id}{ENTRY_SUFFIX}").write_bytes(blob)
            count += 1

        prev_dir = self.vault_dir / "sealed.prev"
        if prev_dir.exists():
            shutil.rmtree(prev_dir)
        if self.sealed_dir.exists():
            self.sealed_dir.rename(prev_dir)
        try:
            staging.rename(self.sealed_dir)
        except BaseException:
            # 回迁旧密文 避免 sealed/ 缺失窗口
            if prev_dir.exists() and not self.sealed_dir.exists():
                prev_dir.rename(self.sealed_dir)
            raise
        shutil.rmtree(prev_dir, ignore_errors=True)
        return count

    def recover_sealed_from_prev(self) -> bool:
        """Crash recovery: roll ``sealed.prev`` back when ``sealed/`` is missing.

        ``seal_from_open`` parks the previous ciphertext at ``sealed.prev``
        while swapping in the new tree; a power cut inside that window leaves
        only the backup. Also cleans up a stale backup when ``sealed/`` is
        already in place (swap succeeded, cleanup did not).
        """
        prev_dir = self.vault_dir / "sealed.prev"
        if not prev_dir.exists():
            return False
        if self.sealed_dir.exists():
            shutil.rmtree(prev_dir, ignore_errors=True)
            return False
        prev_dir.rename(self.sealed_dir)
        logger.warning("vault: sealed/ missing; recovered ciphertext from sealed.prev backup")
        return True

    def snapshot_sealed(self, backup_dir: Path) -> None:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        if self.sealed_dir.is_dir():
            shutil.copytree(self.sealed_dir, backup_dir / "sealed")

    def restore_sealed(self, backup_dir: Path) -> None:
        """Restore ``sealed/`` from a rekey backup via the seal swap pattern.

        The backup is copied into staging first, then swapped in with renames
        (current ``sealed/`` parks at ``sealed.prev``), so a crash mid-restore
        never leaves a half-written ``sealed/`` — ``recover_sealed_from_prev``
        rolls the previous ciphertext back on next start.
        """
        src = backup_dir / "sealed"
        staging = self.vault_dir / ".seal_staging"
        if staging.exists():
            shutil.rmtree(staging)
        if src.is_dir():
            shutil.copytree(src, staging)
        else:
            staging.mkdir(parents=True, exist_ok=True)

        # 归一化上次崩溃的残留（sealed 缺失时从 sealed.prev 自愈）再开始交换
        self.recover_sealed_from_prev()
        prev_dir = self.vault_dir / "sealed.prev"
        if prev_dir.exists():
            shutil.rmtree(prev_dir)
        if self.sealed_dir.exists():
            self.sealed_dir.rename(prev_dir)
        try:
            staging.rename(self.sealed_dir)
        except BaseException:
            # 回迁旧密文 避免 sealed/ 缺失窗口
            if prev_dir.exists() and not self.sealed_dir.exists():
                prev_dir.rename(self.sealed_dir)
            raise
        shutil.rmtree(prev_dir, ignore_errors=True)

    def rekey(self, *, dek_old: bytes, dek_new: bytes) -> int:
        count = 0
        for path in self._iter_sealed():
            payload = self._read_payload(path, dek=dek_old)
            blob = encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), dek_new)
            # 唯一临时名 + fsync + replace 与 core.json_store 原子写同标准
            write_bytes_atomic(path, blob)
            count += 1
        return count
