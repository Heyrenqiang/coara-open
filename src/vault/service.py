"""Vault facade: open / close + idle auto-close after open-folder inactivity."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path

from src.core.logger import logger
from src.core.types import VaultStatus
from src.vault.crypto import build_verifier, derive_key, generate_salt
from src.vault.env_password import vault_password_from_env
from src.vault.errors import VaultAuthError, VaultError, VaultLockedError, VaultNotInitializedError
from src.vault.guard import (
    register_vault_root,
    set_activity_touch,
    set_vault_open_root,
    unregister_vault_root,
)
from src.vault.meta import VaultMeta, current_kdf_params, kdf_is_current, save_meta
from src.vault.password_policy import validate_password_strength
from src.vault.paths import resolve_vault_dir
from src.vault.session import DEFAULT_IDLE_TTL_SECONDS, VaultSession
from src.vault.store import VaultStore

_IDLE_WATCH_INTERVAL = 5.0

# rekey 备份目录里的 meta 快照名 启动对账据此判断崩溃点
_REKEY_BACKUP_META = "meta.json"


class VaultService:
    def __init__(
        self,
        coara_home: Path | str,
        *,
        session_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
    ) -> None:
        self.coara_home = Path(coara_home).expanduser().resolve()
        self.vault_dir = resolve_vault_dir(self.coara_home)
        self.store = VaultStore(self.vault_dir)
        self.session = VaultSession(ttl_seconds=session_ttl_seconds)
        self._initialized = False
        self._owns_open_session = False
        self._idle_task: asyncio.Task[None] | None = None
        # 串行化 async 解锁路径：sync 时代事件循环天然串行 async 化后需显式保序
        self._unlock_op_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        # 启动自愈须先于 ensure_layout 后者会补建空的 sealed/ 目录
        self.store.recover_sealed_from_prev()
        self._reconcile_rekey_backup()
        self.store.ensure_layout()
        # Never wipe open/ here: another process may hold it unlocked, and a
        # subsequent empty seal would destroy sealed ciphertext.
        if self.open_dir.is_dir():
            logger.warning(
                "Vault open/ already exists at startup ({}); leaving on disk until this process unlocks",
                self.open_dir,
            )
        set_vault_open_root(None)
        set_activity_touch(None)
        self._owns_open_session = False
        register_vault_root(self.vault_dir)
        self._initialized = True
        self._idle_task = asyncio.create_task(self._idle_watch_loop(), name="vault-idle-watch")
        logger.info(f"Vault ready at {self.vault_dir} (idle TTL={self.session.ttl_seconds:.0f}s)")

    async def shutdown(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        self.lock()
        self.store.close()
        unregister_vault_root(self.vault_dir)
        set_vault_open_root(None)
        set_activity_touch(None)
        self._initialized = False

    async def _idle_watch_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_IDLE_WATCH_INTERVAL)
            except asyncio.CancelledError:
                raise
            if not self._initialized:
                return
            if self.session.has_dek() and self.session.is_expired():
                logger.info("Vault idle timeout — auto-closing private folder")
                # 串行化：与 change_password_async（worker 线程里 rekey）互斥，
                # 防止改密中途此处的 lock() 覆盖 _owns_open_session/DEK 状态
                async with self._unlock_op_lock:
                    await asyncio.to_thread(self.lock)

    @property
    def open_dir(self) -> Path:
        return self.store.open_dir

    def is_initialized(self) -> bool:
        return self.store.is_initialized()

    def setup(self, password: str, *, enforce_strength: bool = True) -> None:
        self._require_ready()
        if enforce_strength:
            validate_password_strength(password)
        self.store.initialize(password)

    def setup_or_unlock(self, password: str, *, persistent: bool = False, enforce_strength: bool = True) -> bool:
        self._require_ready()
        created = False
        if not self.is_initialized():
            self.setup(password, enforce_strength=enforce_strength)
            created = True
        try:
            dek = self.store.verify_password(password)
        except ValueError as exc:
            raise VaultAuthError(str(exc)) from exc
        dek = self._maybe_upgrade_kdf(password, dek)
        self.session.unlock(dek, persistent=persistent)
        self._materialize()
        return created

    def setup_or_unlock_with_feedback(self, password: str, *, persistent: bool = False) -> tuple[bool, bool, str]:
        """Unlock or create vault and return a user-facing (success, created, message) tuple.

        Centralizes error handling so CLI, Matrix, and Web prompts share the same
        feedback text.
        """
        try:
            created = self.setup_or_unlock(password, persistent=persistent)
        except VaultAuthError:
            return False, False, "主密码错误。"
        except VaultError as exc:
            return False, False, f"解锁失败: {exc}"
        except ValueError as exc:
            return False, False, f"密码不符合要求: {exc}"
        except Exception as exc:
            logger.warning("Vault unlock failed: %s", exc)
            return False, False, f"解锁失败: {exc}"
        if created:
            return True, True, "保险柜已创建并解锁。"
        return True, False, "保险柜已解锁。"

    async def setup_or_unlock_async(
        self, password: str, *, persistent: bool = False, enforce_strength: bool = True
    ) -> bool:
        """Async variant of :meth:`setup_or_unlock`: the legacy-KDF rekey section
        runs in a worker thread (``asyncio.to_thread``) so the event loop is not
        blocked by scrypt derivation + full re-encryption. Session state changes
        stay on the loop thread; behavior and failure-rollback semantics match
        the sync path exactly.
        """
        async with self._unlock_op_lock:
            self._require_ready()
            created = False
            if not self.is_initialized():
                self.setup(password, enforce_strength=enforce_strength)
                created = True
            try:
                dek = self.store.verify_password(password)
            except ValueError as exc:
                raise VaultAuthError(str(exc)) from exc
            dek = await self._maybe_upgrade_kdf_async(password, dek)
            self.session.unlock(dek, persistent=persistent)
            self._materialize()
            return created

    async def setup_or_unlock_with_feedback_async(
        self, password: str, *, persistent: bool = False
    ) -> tuple[bool, bool, str]:
        """Async variant of :meth:`setup_or_unlock_with_feedback` (same feedback text)"""
        try:
            created = await self.setup_or_unlock_async(password, persistent=persistent)
        except VaultAuthError:
            return False, False, "主密码错误。"
        except VaultError as exc:
            return False, False, f"解锁失败: {exc}"
        except ValueError as exc:
            return False, False, f"密码不符合要求: {exc}"
        except Exception as exc:
            logger.warning("Vault unlock failed: %s", exc)
            return False, False, f"解锁失败: {exc}"
        if created:
            return True, True, "保险柜已创建并解锁。"
        return True, False, "保险柜已解锁。"

    def unlock(self, password: str, *, persistent: bool = False) -> Path:
        self._require_ready()
        if not self.is_initialized():
            raise VaultNotInitializedError("vault is not set up yet")
        try:
            dek = self.store.verify_password(password)
        except ValueError as exc:
            raise VaultAuthError(str(exc)) from exc
        dek = self._maybe_upgrade_kdf(password, dek)
        self.session.unlock(dek, persistent=persistent)
        return self._materialize()

    def open(self, password: str | None = None, *, persistent: bool = False) -> Path:
        if self.is_unlocked() and self.open_dir.is_dir():
            self.touch_activity()
            set_vault_open_root(self.open_dir)
            return self.open_dir.resolve()
        if password is None:
            if self.try_unlock_from_env():
                return self.open_dir.resolve()
            raise VaultLockedError("vault is locked")
        return self.unlock(password, persistent=persistent)

    def try_unlock_from_env(self) -> bool:
        password = vault_password_from_env()
        if not password:
            return False
        try:
            # Idle TTL still applies (not persistent forever). Env passwords
            # are exempt from the strength policy (behavior unchanged).
            self.setup_or_unlock(password, persistent=False, enforce_strength=False)
        except (VaultAuthError, ValueError) as exc:
            logger.error(f"Vault env unlock failed: {exc}")
            return False
        except Exception:
            logger.exception("Vault env unlock failed")
            return False
        return True

    def touch_activity(self) -> None:
        """Refresh idle timer (call on any open/ folder access)."""
        if self.session.has_dek() and not self.session.is_expired():
            self.session.touch()

    def lock(self) -> None:
        """Seal ``open/`` into ciphertext, wipe plaintext, clear DEK.

        Only seals/wipes when this process owns an open session. Calling
        ``lock`` while locked is a no-op for on-disk data (does not wipe a
        foreign open/ tree).

        Admission to ``open/`` is closed under the same tree lock as file-tool
        mutations: an in-flight write finishes and is sealed, or a late write
        sees a closed gate and fails — never wiped without being sealed.
        """
        from src.vault.guard import vault_open_tree_lock

        dek = self.session.get_dek()
        owned = self._owns_open_session and dek is not None
        seal_ok = True
        with vault_open_tree_lock():
            # Close admit gate under the same lock as writers so an in-flight
            # open/ write either finishes (and is sealed) or starts after the
            # gate is closed (and fails closed). Never wipe unsealed bytes.
            set_vault_open_root(None)
            set_activity_touch(None)
            if owned:
                try:
                    self.store.seal_from_open(dek=dek)
                except VaultError:
                    seal_ok = False
                    logger.exception("vault seal_from_open refused")
                except Exception:
                    seal_ok = False
                    logger.exception("vault seal_from_open failed")
            self.session.lock()
            if owned and seal_ok:
                self.store.wipe_open()
            self._owns_open_session = False
    def is_unlocked(self) -> bool:
        if not self.session.has_dek():
            return False
        if self.session.is_expired():
            self.lock()
            return False
        return True

    def status(self) -> VaultStatus:
        self._require_ready()
        unlocked = self.is_unlocked()
        return VaultStatus(
            initialized=self.is_initialized(),
            locked=not unlocked,
            entries=self.store.count_sealed() if self.is_initialized() else 0,
            vault_dir=str(self.vault_dir),
        )

    def reset(self) -> None:
        """删除全部内容回到未初始化：忘记主密码的终极手段，不可逆。

        先按正常流程加锁（封存并擦除 open/），再清空整个 vault 目录
        （meta、sealed/、rekey 备份等），最后重建空布局。重置后
        ``is_initialized()`` 为 False，可重新 ``setup`` 新密码。
        """
        self._require_ready()
        self.lock()
        if self.vault_dir.is_dir():
            for child in self.vault_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        child.unlink()
        self.store.ensure_layout()

    async def change_password_async(self, *, old_password: str, new_password: str) -> int:
        """Async variant of :meth:`change_password`: runs the rekey in a worker
        thread while holding ``_unlock_op_lock``, so the event-loop idle-watch
        auto-lock (and async unlock paths) cannot interleave with the rekey and
        clobber ``_owns_open_session`` / DEK state mid-operation."""
        async with self._unlock_op_lock:
            return await asyncio.to_thread(
                self.change_password, old_password=old_password, new_password=new_password
            )

    async def lock_async(self) -> None:
        """Async variant of :meth:`lock` serialized against change_password_async."""
        async with self._unlock_op_lock:
            await asyncio.to_thread(self.lock)

    def change_password(self, *, old_password: str, new_password: str) -> int:
        self._require_ready()
        if not self.is_initialized():
            raise VaultNotInitializedError("vault is not set up yet")
        validate_password_strength(new_password)
        # 跨进程互斥：其他进程持有 open/ 会话（marker 存活）时拒绝改密。
        # 否则对方稍后空闲加锁会用旧 DEK 整体覆盖 sealed/，而 meta 已指向
        # 新盐/新 verifier——新旧密码都无法解密，密文不可恢复。
        self.store._assert_can_take_open_session()
        was_open = self._owns_open_session and self.session.has_dek() and not self.session.is_expired()
        if was_open:
            dek = self.session.get_dek()
            if dek is not None:
                try:
                    self.store.seal_from_open(dek=dek)
                except Exception:
                    logger.exception("seal before rekey failed")
                    raise
            self.store.wipe_open()
            self._owns_open_session = False
            set_vault_open_root(None)
            set_activity_touch(None)

        try:
            dek_old = self.store.verify_password(old_password)
        except ValueError as exc:
            raise VaultAuthError(str(exc)) from exc

        new_salt = generate_salt()
        dek_new = derive_key(new_password, new_salt)
        new_meta = VaultMeta(version=1, salt=new_salt, verifier=build_verifier(dek_new), kdf=current_kdf_params())
        count = self._rekey_sealed(dek_old=dek_old, dek_new=dek_new, new_meta=new_meta)

        self.session.unlock(dek_new, persistent=False)
        self._materialize()
        logger.info(f"Vault password changed; re-encrypted {count} object(s)")
        return count

    def _rekey_sealed(self, *, dek_old: bytes, dek_new: bytes, new_meta: VaultMeta) -> int:
        """Re-encrypt all sealed objects and publish the new meta, with rollback.

        Snapshots ``sealed/`` to ``.rekey_backup`` first; on any failure the
        snapshot is restored so a crash never leaves ciphertext under the old
        DEK while the meta already describes the new one.
        """
        backup_dir = self.vault_dir / ".rekey_backup"
        try:
            self.store.snapshot_sealed(backup_dir)
            # meta 快照供启动对账判断崩溃点：与当前 meta 一致 = save_meta 未发生
            shutil.copy2(self.store.meta_path, backup_dir / _REKEY_BACKUP_META)
        except Exception:
            logger.exception("Vault rekey: backup failed; aborting")
            raise

        try:
            count = self.store.rekey(dek_old=dek_old, dek_new=dek_new)
            save_meta(self.store.meta_path, new_meta)
        except Exception:
            logger.exception("Vault rekey failed; restoring")
            try:
                self.store.restore_sealed(backup_dir)
            except Exception:
                logger.exception("restore failed")
            with contextlib.suppress(OSError):
                shutil.rmtree(backup_dir, ignore_errors=True)
            raise
        else:
            with contextlib.suppress(OSError):
                shutil.rmtree(backup_dir, ignore_errors=True)
        return count

    def _reconcile_rekey_backup(self) -> None:
        """Startup reconciliation for a rekey crash window.

        A leftover ``.rekey_backup`` means the process died inside
        :meth:`_rekey_sealed`. The meta snapshot parked next to the backup
        tells where: if the on-disk meta still matches the snapshot, the
        crash happened before ``save_meta`` — ciphertext may be under the new
        DEK while the meta describes the old one — so roll back to the backup
        (meta unchanged ⇒ old ciphertext is the consistent state). If the
        meta moved on, the rekey completed and only cleanup remained.
        """
        backup_dir = self.vault_dir / ".rekey_backup"
        if not backup_dir.exists():
            return
        backup_meta = backup_dir / _REKEY_BACKUP_META
        try:
            if not backup_meta.is_file():
                # 旧版本残留无 meta 快照 无法判定崩溃点 留给人工恢复
                logger.warning(
                    "Vault .rekey_backup residue without meta snapshot; leaving for manual recovery: %s",
                    backup_dir,
                )
                return
            meta_unchanged = (
                self.store.meta_path.is_file() and backup_meta.read_bytes() == self.store.meta_path.read_bytes()
            )
            if meta_unchanged:
                self.store.restore_sealed(backup_dir)
                with contextlib.suppress(OSError):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                logger.warning("Vault rekey interrupted before meta publish; rolled back sealed ciphertext from backup")
            else:
                with contextlib.suppress(OSError):
                    shutil.rmtree(backup_dir, ignore_errors=True)
                logger.info("Vault rekey completed before crash; cleaned stale .rekey_backup")
        except Exception:
            logger.exception(
                "Vault rekey backup reconciliation failed; manual recovery may be required: %s", backup_dir
            )

    def _run_kdf_upgrade(self, password: str, dek_old: bytes, meta: VaultMeta) -> bytes:
        """Legacy-KDF rekey heavy work: derive + snapshot + re-encrypt + publish meta

        File/crypto only (no session state) — safe to run in a worker thread
        """
        new_salt = generate_salt()
        dek_new = derive_key(password, new_salt)
        new_meta = VaultMeta(
            version=meta.version,
            salt=new_salt,
            verifier=build_verifier(dek_new),
            kdf=current_kdf_params(),
        )
        self._rekey_sealed(dek_old=dek_old, dek_new=dek_new, new_meta=new_meta)
        return dek_new

    def _maybe_upgrade_kdf(self, password: str, dek_old: bytes) -> bytes:
        """Lazily rekey legacy-KDF vaults (meta without kdf field) on unlock.

        Password stays the same; only the derivation parameters are raised to
        the current recommendation. An upgrade failure must never block the
        unlock — fall back to the legacy DEK and keep the old meta.
        """
        meta = self.store.load_meta()
        if kdf_is_current(meta):
            return dek_old
        try:
            dek_new = self._run_kdf_upgrade(password, dek_old, meta)
        except Exception:
            logger.exception("Vault KDF upgrade failed; keeping legacy parameters")
            return dek_old
        logger.info("Vault KDF upgraded to current scrypt parameters")
        return dek_new

    async def _maybe_upgrade_kdf_async(self, password: str, dek_old: bytes) -> bytes:
        """Async variant: the rekey section runs via ``asyncio.to_thread`` off the event loop"""
        meta = self.store.load_meta()
        if kdf_is_current(meta):
            return dek_old
        try:
            dek_new = await asyncio.to_thread(self._run_kdf_upgrade, password, dek_old, meta)
        except Exception:
            logger.exception("Vault KDF upgrade failed; keeping legacy parameters")
            return dek_old
        logger.info("Vault KDF upgraded to current scrypt parameters")
        return dek_new

    def _materialize(self) -> Path:
        dek = self.session.require_dek()
        # 解锁路径兜底自愈 防止运行期间异常留下 sealed.prev
        self.store.recover_sealed_from_prev()
        try:
            open_path = self.store.materialize_open(dek=dek)
        except VaultError:
            self.session.lock()
            self._owns_open_session = False
            set_vault_open_root(None)
            set_activity_touch(None)
            raise
        self._owns_open_session = True
        set_vault_open_root(open_path)
        set_activity_touch(self.touch_activity)
        return open_path

    def _require_ready(self) -> None:
        if not self._initialized:
            raise RuntimeError("VaultService is not initialized")


def get_vault_service(coara: object) -> VaultService | None:
    return getattr(coara, "vault_service", None)
