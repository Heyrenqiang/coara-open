"""coara_home 迁移的完整性与边界测试。"""

from __future__ import annotations

from pathlib import Path

from src.core.home_migration import (
    clear_pending_migration,
    estimate_migration_size,
    execute_migration,
    home_has_data,
    load_pending_migration,
    mark_pending_migration,
)


def _seed_home(home: Path) -> None:
    """造一个有数据的 home：三层 + 根下目录。"""
    (home / "system").mkdir(parents=True, exist_ok=True)
    (home / "system" / "providers.yaml").write_text("providers: {}", encoding="utf-8")
    (home / "system" / "config.yaml").write_text("coara_home: x", encoding="utf-8")
    (home / "users" / "default" / "records").mkdir(parents=True, exist_ok=True)
    (home / "users" / "default" / "records" / "r1.json").write_text("{}", encoding="utf-8")
    ws = home / "workspaces" / "v8-abc123"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "session_events.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (home / "registry").mkdir(parents=True, exist_ok=True)
    (home / "registry" / "workspaces.yaml").write_text("workspaces: {}", encoding="utf-8")


def test_mark_and_load_pending_migration(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    marker = mark_pending_migration(old, new)
    assert marker.parent == new.resolve()
    pending = load_pending_migration(new)
    assert pending is not None
    assert pending.old_home == old.resolve()
    assert pending.new_home == new.resolve()
    clear_pending_migration(new)
    assert load_pending_migration(new) is None


def test_load_pending_migration_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_pending_migration(tmp_path) is None
    (tmp_path / "_pending_migration.json").write_text("not json", encoding="utf-8")
    assert load_pending_migration(tmp_path) is None


def test_execute_migration_copies_all_and_keeps_old(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _seed_home(old)
    copied, skipped = execute_migration(old, new)
    assert copied > 0
    assert skipped == 0
    # 新 home 数据完整
    assert (new / "system" / "providers.yaml").is_file()
    assert (new / "users" / "default" / "records" / "r1.json").is_file()
    assert (new / "workspaces" / "v8-abc123" / "session_events.jsonl").is_file()
    assert (new / "registry" / "workspaces.yaml").is_file()
    # 旧 home 原样保留
    assert (old / "system" / "providers.yaml").is_file()
    assert (old / "workspaces" / "v8-abc123" / "session_events.jsonl").is_file()


def test_execute_migration_does_not_overwrite_existing(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _seed_home(old)
    # 新 home 已有一个同名文件（内容不同）→ 不覆盖
    (new / "system").mkdir(parents=True, exist_ok=True)
    (new / "system" / "config.yaml").write_text("NEW", encoding="utf-8")
    execute_migration(old, new)
    assert (new / "system" / "config.yaml").read_text(encoding="utf-8") == "NEW"
    # 其余文件仍迁入
    assert (new / "system" / "providers.yaml").is_file()


def test_execute_migration_idempotent_on_rerun(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _seed_home(old)
    execute_migration(old, new)
    # 重跑：全部跳过 不重复不报错
    copied, skipped = execute_migration(old, new)
    assert copied == 0
    assert skipped > 0


def test_execute_migration_same_home_is_noop(tmp_path: Path) -> None:
    home = tmp_path / "same"
    _seed_home(home)
    copied, skipped = execute_migration(home, home)
    assert (copied, skipped) == (0, 0)


def test_home_has_data(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not home_has_data(empty)
    (empty / "system").mkdir()
    (empty / "system" / "providers.yaml").write_text("x", encoding="utf-8")
    assert home_has_data(empty)


def test_estimate_migration_size(tmp_path: Path) -> None:
    home = tmp_path / "old"
    _seed_home(home)
    assert estimate_migration_size(home) > 0


def test_config_marks_pending_on_home_change(tmp_path: Path) -> None:
    """save_config_yaml 改 coara_home 时落待迁移标记到旧 home。"""
    from src.core.config import config_manager

    old = tmp_path / "old"
    new = tmp_path / "new"
    _seed_home(old)
    config_path = old / "system" / "config.yaml"
    config_path.write_text(f"coara_home: {old}\n", encoding="utf-8")

    # 直接调 save_config_yaml 改 home（隔离 config_manager 全局态：写路径由 _find_writable_config_yaml 决定）
    old_raw = dict(config_manager._raw_config)  # noqa: SLF001
    try:
        config_manager._raw_config = {"coara_home": str(old)}  # noqa: SLF001
        # _find_writable_config_yaml 依赖 _raw_config 找可写 config.yaml；指到旧 home 的 system/config.yaml
        config_manager.save_config_yaml({"coara_home": str(new)})
        # 标记写在新 home（启动时生效 home=新 home 便于检测）
        pending_at_new = load_pending_migration(new)
        assert pending_at_new is not None
        assert pending_at_new.old_home == old.resolve()
        assert pending_at_new.new_home == new.resolve()
    finally:
        config_manager._raw_config = old_raw  # noqa: SLF001
