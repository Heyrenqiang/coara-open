"""Vault open/close folder tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tools.builtin.file_io.file_support import resolve_workspace_path
from src.tools.builtin.vault.vault import VaultTool, _PasswordPromptResult
from src.vault import VaultService
from src.vault.errors import VaultAuthError
from src.vault.guard import clear_vault_roots, is_under_vault_open
from src.vault.paths import vault_open_dir, vault_sealed_dir
from src.vault.store import VaultStore


@pytest.fixture(autouse=True)
def _clear_vault_roots():
    clear_vault_roots()
    yield
    clear_vault_roots()


@pytest.mark.asyncio
async def test_open_materialize_and_file_tools(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    assert open_dir.is_dir()
    assert is_under_vault_open(open_dir)

    note = open_dir / "notes" / "a.txt"
    note.parent.mkdir(parents=True)
    note.write_text("hello-secret", encoding="utf-8")

    path, err = resolve_workspace_path(str(note), tmp_path, "write", read_only=False)
    assert err is None and path == note.resolve()

    sealed = vault_sealed_dir(service.vault_dir)
    service.lock()
    assert not vault_open_dir(service.vault_dir).exists()
    assert sealed.is_dir()
    assert list(sealed.glob("*.cvault"))

    # Sealed ciphertext blocked
    sample = next(sealed.glob("*.cvault"))
    blocked, err2 = resolve_workspace_path(str(sample), tmp_path, "read", read_only=True)
    assert blocked is None and err2 is not None

    open2 = service.unlock("test-password-123")
    restored = open2 / "notes" / "a.txt"
    assert restored.read_text(encoding="utf-8") == "hello-secret"
    await service.shutdown()


@pytest.mark.asyncio
async def test_vault_tool_no_approval_open_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = MagicMock()
    root._web_server = None
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    root.vault_service = service

    async def fake_pw(*_a, **_k):
        return _PasswordPromptResult(password="test-password-123")

    monkeypatch.setattr("src.tools.builtin.vault.vault._ask_password", fake_pw)

    tool = VaultTool(parent_coara=root)
    assert tool.requires_approval is False
    result = await tool.create_invocation({"action": "open"}).execute()
    assert not result.is_error
    assert "工作目录" in result.content
    assert service.is_unlocked()

    closed = await tool.create_invocation({"action": "close"}).execute()
    assert not closed.is_error
    assert not service.is_unlocked()
    await service.shutdown()


def test_vault_unknown_action_raises() -> None:
    tool = VaultTool()
    with pytest.raises(ValueError, match="未知的 vault action"):
        tool.create_invocation({"action": "write"})


@pytest.mark.asyncio
async def test_wrong_password(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    with pytest.raises(VaultAuthError):
        service.unlock("wrong-password-xyz")
    await service.shutdown()


@pytest.mark.asyncio
async def test_change_password(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("old-password-123")
    open_dir = service.unlock("old-password-123")
    (open_dir / "x.txt").write_text("body", encoding="utf-8")
    service.lock()

    service.change_password(old_password="old-password-123", new_password="new-password-456")
    assert (service.open_dir / "x.txt").read_text(encoding="utf-8") == "body"
    service.lock()
    with pytest.raises(VaultAuthError):
        service.unlock("old-password-123")
    service.unlock("new-password-456")
    await service.shutdown()


@pytest.mark.asyncio
async def test_rekey_leaves_no_tmp_residue(tmp_path: Path) -> None:
    """#344 rekey 走唯一临时名原子写 sealed/ 不留固定 .tmp 残留"""
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("old-password-123")
    open_dir = service.unlock("old-password-123")
    (open_dir / "x.txt").write_text("body", encoding="utf-8")
    service.lock()

    service.change_password(old_password="old-password-123", new_password="new-password-456")
    assert not list(vault_sealed_dir(service.vault_dir).glob("*.tmp"))
    await service.shutdown()


@pytest.mark.asyncio
async def test_startup_rolls_back_rekey_crash_before_meta_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#339 进程死于 rekey 与 save_meta 之间 启动对账自动回滚 旧密码可解"""
    import src.vault.service as service_mod

    home = tmp_path / "home"
    service = VaultService(home)
    await service.initialize()
    service.setup("old-password-123")
    open_dir = service.unlock("old-password-123")
    (open_dir / "x.txt").write_text("body", encoding="utf-8")
    service.lock()

    def crash_save_meta(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt("simulated process kill")

    monkeypatch.setattr(service_mod, "save_meta", crash_save_meta)
    with pytest.raises(KeyboardInterrupt):
        service.change_password(old_password="old-password-123", new_password="new-password-456")
    monkeypatch.undo()

    # 崩溃现场：备份残留 密文已是新 DEK meta 仍是旧
    assert (service.vault_dir / ".rekey_backup").exists()
    await service.shutdown()

    service2 = VaultService(home)
    await service2.initialize()
    assert not (service2.vault_dir / ".rekey_backup").exists()
    open2 = service2.unlock("old-password-123")
    assert (open2 / "x.txt").read_text(encoding="utf-8") == "body"
    await service2.shutdown()


@pytest.mark.asyncio
async def test_startup_cleans_rekey_backup_after_meta_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#339 进程死于 save_meta 之后 启动对账只清理残留 新密码可解"""
    import shutil

    import src.vault.service as service_mod

    home = tmp_path / "home"
    service = VaultService(home)
    await service.initialize()
    service.setup("old-password-123")
    open_dir = service.unlock("old-password-123")
    (open_dir / "x.txt").write_text("body", encoding="utf-8")
    service.lock()

    real_rmtree = shutil.rmtree

    def flaky_rmtree(path: object, *a: object, **k: object) -> None:
        if str(path).endswith(".rekey_backup"):
            raise OSError("simulated lock on backup dir")
        real_rmtree(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(service_mod.shutil, "rmtree", flaky_rmtree)
    service.change_password(old_password="old-password-123", new_password="new-password-456")
    monkeypatch.undo()

    # rmtree 被容错吞掉 备份残留 但 meta 已发布 磁盘状态一致
    assert (service.vault_dir / ".rekey_backup").exists()
    await service.shutdown()

    service2 = VaultService(home)
    await service2.initialize()
    assert not (service2.vault_dir / ".rekey_backup").exists()
    open2 = service2.unlock("new-password-456")
    assert (open2 / "x.txt").read_text(encoding="utf-8") == "body"
    await service2.shutdown()


@pytest.mark.asyncio
async def test_startup_leaves_ambiguous_rekey_backup_for_manual_recovery(tmp_path: Path) -> None:
    """#339 旧版本残留（无 meta 快照）无法判定崩溃点 不擅自回滚 留人工恢复"""
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    backup = service.vault_dir / ".rekey_backup"
    backup.mkdir()
    (backup / "stale.txt").write_text("x", encoding="utf-8")
    await service.shutdown()

    service2 = VaultService(tmp_path / "home")
    await service2.initialize()
    assert backup.exists()
    assert (service2.vault_dir / "sealed").exists()
    await service2.shutdown()


@pytest.mark.asyncio
async def test_idle_timeout_auto_locks(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home", session_ttl_seconds=0.05)
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "a.txt").write_text("x", encoding="utf-8")
    assert service.is_unlocked()
    await asyncio.sleep(0.2)
    # is_unlocked triggers seal when expired
    assert not service.is_unlocked()
    assert not vault_open_dir(service.vault_dir).exists()
    assert list(vault_sealed_dir(service.vault_dir).glob("*.cvault"))
    await service.shutdown()


@pytest.mark.asyncio
async def test_touch_extends_idle(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home", session_ttl_seconds=0.25)
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    for _ in range(4):
        await asyncio.sleep(0.08)
        service.touch_activity()
    assert service.is_unlocked()
    assert open_dir.is_dir()
    await service.shutdown()


@pytest.mark.asyncio
async def test_timed_out_recovers_if_vault_unlocked_cross_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 3: timed_out should re-check is_unlocked() — a cross-frontend
    unlock (e.g. Matrix user sent password right as Web prompt timed out)
    may have unlocked the vault."""
    root = MagicMock()
    root._web_server = None
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    root.vault_service = service

    async def fake_pw(*_a, **_k):
        # Simulate: Web prompt timed out, but vault got unlocked by another frontend
        service.unlock("test-password-123")
        return _PasswordPromptResult(timed_out=True)

    monkeypatch.setattr("src.tools.builtin.vault.vault._ask_password", fake_pw)

    tool = VaultTool(parent_coara=root)
    result = await tool.create_invocation({"action": "open"}).execute()
    # Despite timed_out, vault is unlocked → tool succeeds
    assert not result.is_error
    assert "工作目录" in result.content
    await service.shutdown()


@pytest.mark.asyncio
async def test_no_prompt_returns_specific_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 4: no_tty should return a specific message, not 'timeout'."""
    root = MagicMock()
    root._web_server = None
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    root.vault_service = service

    async def fake_pw(*_a, **_k):
        return _PasswordPromptResult(no_prompt=True)

    monkeypatch.setattr("src.tools.builtin.vault.vault._ask_password", fake_pw)

    tool = VaultTool(parent_coara=root)
    result = await tool.create_invocation({"action": "open"}).execute()
    assert result.is_error
    assert "无 TTY" in result.content or "无法弹出密码框" in result.content
    await service.shutdown()


@pytest.mark.asyncio
async def test_initialize_does_not_wipe_foreign_open(tmp_path: Path) -> None:
    home = tmp_path / "home"
    owner = VaultService(home)
    await owner.initialize()
    owner.setup("test-password-123")
    open_dir = owner.unlock("test-password-123")
    secret = open_dir / "keep.txt"
    secret.write_text("do-not-wipe", encoding="utf-8")

    peer = VaultService(home)
    await peer.initialize()
    assert secret.read_text(encoding="utf-8") == "do-not-wipe"
    peer.lock()  # locked peer must not wipe owner's open/
    assert secret.read_text(encoding="utf-8") == "do-not-wipe"
    await peer.shutdown()

    owner.lock()
    assert list(vault_sealed_dir(owner.vault_dir).glob("*.cvault"))
    await owner.shutdown()


@pytest.mark.asyncio
async def test_seal_refuses_when_open_missing(tmp_path: Path) -> None:
    from src.vault.errors import VaultError

    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "a.txt").write_text("x", encoding="utf-8")
    service.lock()
    assert list(vault_sealed_dir(service.vault_dir).glob("*.cvault"))

    # Simulate foreign wipe of open/ while holding a DEK incorrectly:
    # seal_from_open must refuse rather than clear sealed/.
    service.unlock("test-password-123")
    import shutil

    shutil.rmtree(service.open_dir)
    with pytest.raises(VaultError, match="refuse seal"):
        service.store.seal_from_open(dek=service.session.require_dek())
    assert list(vault_sealed_dir(service.vault_dir).glob("*.cvault"))
    await service.shutdown()


@pytest.mark.asyncio
async def test_refuse_unlock_while_other_process_holds_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.vault.errors import VaultError
    from src.vault.store import _OPEN_SESSION_MARKER, VaultStore

    home = tmp_path / "home"
    owner = VaultService(home)
    await owner.initialize()
    owner.setup("test-password-123")
    open_dir = owner.unlock("test-password-123")
    (open_dir / _OPEN_SESSION_MARKER).write_text("999999", encoding="utf-8")

    peer = VaultService(home)
    await peer.initialize()
    monkeypatch.setattr(VaultStore, "_pid_alive", lambda self, _pid: True)
    with pytest.raises(VaultError, match="another process"):
        peer.unlock("test-password-123")
    await peer.shutdown()
    await owner.shutdown()


def test_materialize_open_loser_of_race_never_wipes_plaintext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#25：占位原子化——持锁进程存活时第二实例被拒，且先解者窗口内新写明文不被 wipe。"""
    from src.vault.errors import VaultError
    from src.vault.store import _OPEN_SESSION_MARKER

    vault_dir = tmp_path / "vault"
    owner = VaultStore(vault_dir)
    root = owner.materialize_open(dek=b"x" * 32)
    (root / "secret.txt").write_text("plain", encoding="utf-8")
    # 模拟另一进程持锁：标记改写为外部 pid 且探测存活
    (root / _OPEN_SESSION_MARKER).write_text("999999", encoding="utf-8")

    peer = VaultStore(vault_dir)
    monkeypatch.setattr(VaultStore, "_pid_alive", lambda self, _pid: True)
    with pytest.raises(VaultError, match="another process"):
        peer.materialize_open(dek=b"x" * 32)
    assert (root / "secret.txt").read_text(encoding="utf-8") == "plain"


def test_claim_session_marker_race_loser_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#25：check 与占位之间被抢先占位——O_EXCL 撞车后存活复核拒绝，赢家标记不被篡夺。"""
    from src.vault.errors import VaultError

    store = VaultStore(tmp_path / "vault")
    store.ensure_open_dir()
    marker = store._session_marker_path()
    real_assert = VaultStore._assert_can_take_open_session
    calls = {"n": 0}

    def racy_assert(self: VaultStore) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次存活检查时标记尚不存在，随后被"另一进程"抢先创建
            marker.write_text("999999", encoding="utf-8")
            return
        real_assert(self)

    monkeypatch.setattr(VaultStore, "_assert_can_take_open_session", racy_assert)
    monkeypatch.setattr(VaultStore, "_pid_alive", lambda self, _pid: True)
    store._assert_can_take_open_session()  # 入口检查通过（随后被抢先占位）
    with pytest.raises(VaultError, match="another process"):
        store._claim_session_marker()
    assert marker.read_text(encoding="utf-8").strip() == "999999"


def test_materialize_open_replaces_stale_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#25：陈旧标记（持锁进程已死）被清理后重新占位，旧 open 树仍被重建。"""
    import os

    vault_dir = tmp_path / "vault"
    stale = VaultStore(vault_dir)
    stale.ensure_open_dir()
    marker = stale._session_marker_path()
    marker.write_text("999999", encoding="utf-8")
    (stale.open_dir / "leftover.txt").write_text("old", encoding="utf-8")

    monkeypatch.setattr(VaultStore, "_pid_alive", lambda self, _pid: False)
    store = VaultStore(vault_dir)
    root = store.materialize_open(dek=b"x" * 32)
    assert marker.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not (root / "leftover.txt").exists()


def test_materialize_open_relock_same_process(tmp_path: Path) -> None:
    """#25：同进程重复解锁允许接管（旧占位属本进程），open 树照常重建。"""
    store = VaultStore(tmp_path / "vault")
    root = store.materialize_open(dek=b"x" * 32)
    (root / "a.txt").write_text("one", encoding="utf-8")
    reopened = store.materialize_open(dek=b"x" * 32)
    assert not (reopened / "a.txt").exists()


@pytest.mark.asyncio
async def test_shell_cwd_allows_vault_open(tmp_path: Path) -> None:
    from src.tools.builtin.runtime.shell import ShellTool

    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "n.txt").write_text("hi", encoding="utf-8")

    parent = MagicMock()
    parent.workspace_manager = MagicMock()
    parent.session_id = "s1"
    parent.identity = MagicMock(coara_id="c1")
    tool = ShellTool(workspace_root=tmp_path / "ws", parent_coara=parent)
    (tmp_path / "ws").mkdir()
    inv = tool.create_invocation(
        {
            "command": "python -c \"print(open('n.txt',encoding='utf-8').read())\"",
            "working_directory": str(open_dir),
        }
    )
    result = await inv.execute()
    assert not result.is_error, result.content
    assert "hi" in result.content
    await service.shutdown()


@pytest.mark.asyncio
async def test_seal_cleans_prev_backup_after_success(tmp_path: Path) -> None:
    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "a.txt").write_text("one", encoding="utf-8")
    service.lock()

    open_dir = service.unlock("test-password-123")
    (open_dir / "b.txt").write_text("two", encoding="utf-8")
    service.lock()

    sealed = vault_sealed_dir(service.vault_dir)
    assert not (service.vault_dir / "sealed.prev").exists()
    assert len(list(sealed.glob("*.cvault"))) == 2

    open2 = service.unlock("test-password-123")
    assert (open2 / "a.txt").read_text(encoding="utf-8") == "one"
    assert (open2 / "b.txt").read_text(encoding="utf-8") == "two"
    await service.shutdown()


def test_seal_rename_failure_restores_previous_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟断电：staging rename 失败时旧密文从 sealed.prev 回迁 不留缺失窗口"""
    store = VaultStore(tmp_path / "vault")
    store.initialize("test-password-123")
    dek = store.verify_password("test-password-123")

    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("one", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1

    (open_dir / "b.txt").write_text("two", encoding="utf-8")
    real_rename = Path.rename

    def flaky_rename(self: Path, target: Path) -> None:
        if self.name == ".seal_staging":
            raise OSError("simulated power cut")
        real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="simulated power cut"):
        store.seal_from_open(dek=dek)

    sealed = vault_sealed_dir(store.vault_dir)
    assert len(list(sealed.glob("*.cvault"))) == 1
    assert not (store.vault_dir / "sealed.prev").exists()


@pytest.mark.asyncio
async def test_startup_recovers_sealed_from_prev(tmp_path: Path) -> None:
    """启动自愈：sealed 缺失但 sealed.prev 存在时回滚 密文不丢"""
    home = tmp_path / "home"
    service = VaultService(home)
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "a.txt").write_text("secret", encoding="utf-8")
    service.lock()
    await service.shutdown()

    # 模拟断电窗口：sealed 已让位给 sealed.prev 新密文尚未就位
    sealed = vault_sealed_dir(service.vault_dir)
    prev = service.vault_dir / "sealed.prev"
    sealed.rename(prev)

    service2 = VaultService(home)
    await service2.initialize()
    assert sealed.is_dir()
    assert not prev.exists()
    assert len(list(sealed.glob("*.cvault"))) == 1

    open2 = service2.unlock("test-password-123")
    assert (open2 / "a.txt").read_text(encoding="utf-8") == "secret"
    await service2.shutdown()


@pytest.mark.asyncio
async def test_startup_drops_stale_prev_when_sealed_intact(tmp_path: Path) -> None:
    """sealed 已就位时 残留的 sealed.prev 只是未清理的备份 直接删除"""
    home = tmp_path / "home"
    service = VaultService(home)
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "a.txt").write_text("secret", encoding="utf-8")
    service.lock()
    await service.shutdown()

    sealed = vault_sealed_dir(service.vault_dir)
    entries = sorted(sealed.glob("*.cvault"))
    prev = service.vault_dir / "sealed.prev"
    prev.mkdir()
    (prev / "stale.cvault").write_bytes(b"stale")

    service2 = VaultService(home)
    await service2.initialize()
    assert not prev.exists()
    assert sorted(p.name for p in sealed.glob("*.cvault")) == [p.name for p in entries]

    open2 = service2.unlock("test-password-123")
    assert (open2 / "a.txt").read_text(encoding="utf-8") == "secret"
    await service2.shutdown()


def test_restore_sealed_swaps_backup_into_place(tmp_path: Path) -> None:
    """rekey 回滚：备份经 staging + rename 交换就位 不留 sealed.prev / staging 残留"""
    store = VaultStore(tmp_path / "vault")
    store.initialize("test-password-123")
    dek = store.verify_password("test-password-123")

    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("one", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1

    backup_dir = store.vault_dir / ".rekey_backup"
    store.snapshot_sealed(backup_dir)

    # 备份之后 sealed 继续演进（多封了一个文件）
    (open_dir / "b.txt").write_text("two", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 2

    store.restore_sealed(backup_dir)

    assert not (store.vault_dir / "sealed.prev").exists()
    assert not (store.vault_dir / ".seal_staging").exists()
    # 回滚到备份内容：只剩 a.txt 的密文
    store.wipe_open()
    root = store.materialize_open(dek=dek)
    assert (root / "a.txt").read_text(encoding="utf-8") == "one"
    assert not (root / "b.txt").exists()


def test_restore_sealed_rename_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟断电：staging rename 失败时当前密文从 sealed.prev 回迁 不留缺失窗口"""
    store = VaultStore(tmp_path / "vault")
    store.initialize("test-password-123")
    dek = store.verify_password("test-password-123")

    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("one", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    before = sorted(p.name for p in store.sealed_dir.glob("*.cvault"))

    backup_dir = store.vault_dir / ".rekey_backup"
    store.snapshot_sealed(backup_dir)

    real_rename = Path.rename

    def flaky_rename(self: Path, target: Path) -> None:
        if self.name == ".seal_staging":
            raise OSError("simulated power cut")
        real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="simulated power cut"):
        store.restore_sealed(backup_dir)

    assert sorted(p.name for p in store.sealed_dir.glob("*.cvault")) == before
    assert not (store.vault_dir / "sealed.prev").exists()


def test_restore_sealed_crash_window_recovers_from_prev(tmp_path: Path) -> None:
    """交换窗口断电（sealed 已让位 sealed.prev 新树未就位）自愈回滚 密文不丢"""
    store = VaultStore(tmp_path / "vault")
    store.initialize("test-password-123")
    dek = store.verify_password("test-password-123")

    open_dir = store.ensure_open_dir()
    (open_dir / "a.txt").write_text("one", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    entries = sorted(p.name for p in store.sealed_dir.glob("*.cvault"))

    # 手动摆出 restore 交换中途的断电现场
    store.sealed_dir.rename(store.vault_dir / "sealed.prev")
    (store.vault_dir / ".seal_staging").mkdir()

    assert store.recover_sealed_from_prev() is True
    assert sorted(p.name for p in store.sealed_dir.glob("*.cvault")) == entries


def test_uncoordinated_seal_then_write_then_wipe_loses_plaintext(tmp_path: Path) -> None:
    """P0-1 病灶复现：seal 快照之后、wipe 之前写入的文件既不进密文也被擦掉。"""
    store = VaultStore(tmp_path / "vault")
    store.initialize("test-password-123")
    dek = store.verify_password("test-password-123")
    open_dir = store.ensure_open_dir()
    (open_dir / "early.txt").write_text("early", encoding="utf-8")
    assert store.seal_from_open(dek=dek) == 1
    (open_dir / "late.txt").write_text("late-secret", encoding="utf-8")
    store.wipe_open()

    # Re-open via store materialize
    root = store.materialize_open(dek=dek)
    assert (root / "early.txt").read_text(encoding="utf-8") == "early"
    assert not (root / "late.txt").exists()


@pytest.mark.asyncio
async def test_lock_waits_for_inflight_open_write_and_seals_it(tmp_path: Path) -> None:
    """在飞 write_text_file 持锁时 lock 必须等它结束，并把内容封进密文。"""
    import threading
    import time

    from src.tools.builtin.file_io.file_support import write_text_file
    from src.vault.guard import vault_open_tree_lock

    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    (open_dir / "early.txt").write_text("early", encoding="utf-8")

    barrier = threading.Barrier(2)
    write_error: list[BaseException] = []

    def writer() -> None:
        try:
            with vault_open_tree_lock():
                barrier.wait(timeout=5)
                time.sleep(0.15)
                write_text_file(open_dir / "late.txt", "late-secret")
        except BaseException as exc:  # noqa: BLE001
            write_error.append(exc)

    def locker() -> None:
        barrier.wait(timeout=5)
        service.lock()

    t_w = threading.Thread(target=writer)
    t_l = threading.Thread(target=locker)
    t_w.start()
    t_l.start()
    t_w.join(timeout=10)
    t_l.join(timeout=10)
    assert not t_w.is_alive() and not t_l.is_alive()
    assert write_error == []

    open2 = service.unlock("test-password-123")
    assert (open2 / "early.txt").read_text(encoding="utf-8") == "early"
    assert (open2 / "late.txt").read_text(encoding="utf-8") == "late-secret"
    await service.shutdown()


@pytest.mark.asyncio
async def test_write_after_admit_closed_fails_closed(tmp_path: Path) -> None:
    """封箱已关闭 admit 门后，write_text_file 必须失败，不得落盘后再被 wipe。"""
    from src.tools.builtin.file_io.file_support import write_text_file
    from src.vault.errors import VaultLockedError
    from src.vault.guard import set_activity_touch, set_vault_open_root

    service = VaultService(tmp_path / "home")
    await service.initialize()
    service.setup("test-password-123")
    open_dir = service.unlock("test-password-123")
    target = open_dir / "orphan.txt"

    # Simulate lock()'s admit-gate close before seal critical section
    set_vault_open_root(None)
    set_activity_touch(None)
    with pytest.raises(VaultLockedError):
        write_text_file(target, "should-not-land")
    assert not target.exists()

    # Finish a normal lock so shutdown is clean
    service.lock()
    await service.shutdown()
