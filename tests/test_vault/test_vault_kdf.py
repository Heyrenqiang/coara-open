"""Vault KDF migration (#265) and password strength policy (#266) tests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from src.vault import VaultService
from src.vault.crypto import _SCRYPT_N, _SCRYPT_P, _SCRYPT_R, LEGACY_SCRYPT_N, build_verifier, derive_key, generate_salt
from src.vault.errors import VaultAuthError
from src.vault.meta import VaultMeta, load_meta, save_meta
from src.vault.paths import vault_meta_path, vault_sealed_dir
from src.vault.store import VaultStore

_PASSWORD = "test-password-123"


def _make_legacy_store(vault_dir: Path, password: str) -> tuple[VaultStore, bytes]:
    """Build a legacy vault on disk: meta without kdf field, derived at N=2^14."""
    store = VaultStore(vault_dir)
    store.ensure_layout()
    salt = generate_salt()
    dek = derive_key(password, salt, n=LEGACY_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    save_meta(store.meta_path, VaultMeta(version=1, salt=salt, verifier=build_verifier(dek)))
    return store, dek


@pytest.mark.asyncio
async def test_new_vault_writes_current_kdf_params(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup(_PASSWORD)

    raw = json.loads(service.store.meta_path.read_text(encoding="utf-8"))
    assert raw["kdf"] == {"algo": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P}
    await service.shutdown()


@pytest.mark.asyncio
async def test_legacy_meta_unlocks_and_lazily_upgrades(tmp_path: Path) -> None:
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()

    store, dek = _make_legacy_store(vault_dir, _PASSWORD)
    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("legacy-secret", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    store.wipe_open()

    service = VaultService(home)
    await service.initialize()
    # meta 无 kdf 字段的旧 vault 仍能解锁
    assert not json.loads(vault_meta_path(vault_dir).read_text(encoding="utf-8")).get("kdf")
    open2 = service.unlock(_PASSWORD)
    assert (open2 / "a.txt").read_text(encoding="utf-8") == "legacy-secret"

    # 解锁成功后惰性升级到当前参数 密码不变
    meta = load_meta(vault_meta_path(vault_dir))
    assert meta.kdf is not None
    assert (meta.kdf.algo, meta.kdf.n, meta.kdf.r, meta.kdf.p) == ("scrypt", _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)

    # 升级后封存/再解锁全链路仍正常
    service.lock()
    service.unlock(_PASSWORD)
    assert (service.open_dir / "a.txt").read_text(encoding="utf-8") == "legacy-secret"
    service.lock()
    assert list(vault_sealed_dir(vault_dir).glob("*.cvault"))
    await service.shutdown()


@pytest.mark.asyncio
async def test_async_unlock_upgrades_legacy_kdf_off_event_loop(tmp_path: Path) -> None:
    """#338 异步解锁路径 rekey 段经 to_thread 下线 升级期间事件循环保持响应"""
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()

    store, dek = _make_legacy_store(vault_dir, _PASSWORD)
    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("legacy-secret", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    store.wipe_open()

    service = VaultService(home)
    await service.initialize()

    # 放慢 rekey 让事件循环是否饥饿可观测
    real_run = VaultService._run_kdf_upgrade

    def slow_run(self: VaultService, password: str, dek_old: bytes, meta: object) -> bytes:
        time.sleep(0.3)
        return real_run(self, password, dek_old, meta)  # type: ignore[arg-type]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(VaultService, "_run_kdf_upgrade", slow_run)

    ticks = 0
    stop = False

    async def ticker() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.02)

    ticker_task = asyncio.create_task(ticker())
    ok, created, _message = await service.setup_or_unlock_with_feedback_async(_PASSWORD, persistent=False)
    stop = True
    await ticker_task
    monkeypatch.undo()

    assert ok and not created
    # 升级耗时约 0.3s 若 rekey 卡在事件循环 ticks 会为 0
    assert ticks >= 2
    meta = load_meta(vault_meta_path(vault_dir))
    assert meta.kdf is not None and meta.kdf.n == _SCRYPT_N
    assert (service.open_dir / "a.txt").read_text(encoding="utf-8") == "legacy-secret"
    await service.shutdown()


@pytest.mark.asyncio
async def test_async_unlock_upgrade_failure_keeps_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#338 异步路径失败回退语义与同步一致 升级失败不阻塞解锁"""
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()

    store, dek = _make_legacy_store(vault_dir, _PASSWORD)
    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("legacy-secret", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    store.wipe_open()

    service = VaultService(home)
    await service.initialize()

    def boom(self: VaultService, *_a: object, **_k: object) -> bytes:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(VaultService, "_run_kdf_upgrade", boom)
    ok, _, _ = await service.setup_or_unlock_with_feedback_async(_PASSWORD, persistent=False)
    assert ok
    assert (service.open_dir / "a.txt").read_text(encoding="utf-8") == "legacy-secret"
    # meta 保持旧参数 下次解锁仍会尝试升级
    assert load_meta(vault_meta_path(vault_dir)).kdf is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_wrong_password_fails_on_legacy_and_current(tmp_path: Path) -> None:
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()
    _make_legacy_store(vault_dir, _PASSWORD)

    service = VaultService(home)
    await service.initialize()
    with pytest.raises(VaultAuthError):
        service.unlock("wrong-password-xyz")
    await service.shutdown()

    service2 = VaultService(tmp_path / "home2")
    await service2.initialize()
    service2.setup(_PASSWORD)
    with pytest.raises(VaultAuthError):
        service2.unlock("wrong-password-xyz")
    await service2.shutdown()


@pytest.mark.asyncio
async def test_tampered_kdf_params_rejected(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup(_PASSWORD)

    meta_path = service.store.meta_path
    good = json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        for bad_kdf in (
            {"algo": "md5", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P},
            {"algo": "scrypt", "n": 2**30, "r": _SCRYPT_R, "p": _SCRYPT_P},
            {"algo": "scrypt", "n": _SCRYPT_N + 1, "r": _SCRYPT_R, "p": _SCRYPT_P},
        ):
            meta_path.write_text(json.dumps({**good, "kdf": bad_kdf}), encoding="utf-8")
            with pytest.raises(VaultAuthError):
                service.unlock(_PASSWORD)
    finally:
        meta_path.write_text(json.dumps(good), encoding="utf-8")
    await service.shutdown()


@pytest.mark.asyncio
async def test_change_password_upgrades_legacy_kdf(tmp_path: Path) -> None:
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()
    _make_legacy_store(vault_dir, _PASSWORD)

    service = VaultService(home)
    await service.initialize()
    count = service.change_password(old_password=_PASSWORD, new_password="new-password-456")
    assert count == 0

    meta = load_meta(service.store.meta_path)
    assert meta.kdf is not None and meta.kdf.n == _SCRYPT_N
    service.lock()
    with pytest.raises(VaultAuthError):
        service.unlock(_PASSWORD)
    service.unlock("new-password-456")
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("weak", ["short1", "12345678", "password", "abcdefgh", "PASSWORD1", "1q2w3e4r"])
async def test_weak_passwords_rejected_on_setup(tmp_path: Path, weak: str) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    with pytest.raises(ValueError, match="vault password"):
        service.setup(weak)
    assert not service.is_initialized()
    await service.shutdown()


@pytest.mark.asyncio
async def test_compliant_password_accepted(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("My-Vault-2026")
    assert service.is_initialized()
    await service.shutdown()


@pytest.mark.asyncio
async def test_change_password_rejects_weak_new_password(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup(_PASSWORD)
    with pytest.raises(ValueError, match="vault password"):
        service.change_password(old_password=_PASSWORD, new_password="12345678")
    # 拒绝后旧密码仍可用
    service.unlock(_PASSWORD)
    await service.shutdown()


@pytest.mark.asyncio
async def test_unlock_does_not_enforce_strength(tmp_path: Path) -> None:
    """既有弱密码 vault 必须仍能解锁 复杂度只在设置/修改时校验"""
    home = tmp_path / "home"
    probe = VaultService(home)
    await probe.initialize()
    vault_dir = probe.vault_dir
    await probe.shutdown()
    store = VaultStore(vault_dir)
    store.initialize("12345678")

    service = VaultService(home)
    await service.initialize()
    service.unlock("12345678")
    assert service.is_unlocked()
    await service.shutdown()


@pytest.mark.asyncio
async def test_env_password_exempt_from_strength(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量路径行为不变 弱密码也可经 COARA_VAULT_PASSWORD 建箱并解锁"""
    monkeypatch.setenv("COARA_VAULT_PASSWORD", "12345678")
    service = VaultService(tmp_path / "home")
    await service.initialize()
    assert service.try_unlock_from_env()
    assert service.is_initialized()
    assert service.is_unlocked()
    await service.shutdown()
