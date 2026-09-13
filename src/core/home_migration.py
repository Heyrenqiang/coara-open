"""coara_home 切换的数据迁移。

用户改 ``coara_home``（如从 C 盘换到 D 盘）后，旧 home 的全部数据（配置/用户资产/
录像带/工作区状态）需要搬到新 home，否则历史「消失」。

流程：
- ``mark_pending_migration``：``save_config_yaml`` 检测到 coara_home 变化时，在旧 home
  落一个待迁移标记（同步写入函数不做交互与迁移，避免污染所有调用方）。
- ``run_pending_migration``：下次启动早期（config 加载后、各模块初始化前，数据文件
  尚未打开的安全窗口）检测标记，确认后逐层复制旧 home → 新 home，完成后清标记。
  旧数据原地保留（绝不自动删）。

关键不变量：工作空间目录没变只是 home 变了，``workspace_id`` 不变，录像带/traces
整目录搬运即可，id 对得上，历史会话无缝接上。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.core.json_store import write_json_atomic

_PENDING_MIGRATION_FILENAME = "_pending_migration.json"

# 迁移的数据子目录/文件（相对 home 根）。只迁数据 不迁日志类等可再生内容。
# 覆盖三层：system/（配置） users/（用户资产） workspaces/（录像带/traces） 及根下数据目录。
_MIGRATE_ENTRIES = (
    "system",
    "users",
    "workspaces",
    "registry",
    "reminders",
    # 可选能力自带的目录（遥测队列等）：未装实现的发行版里不存在，迁移自然跳过
    "telemetry",
    "matrix",
    "outbox",
    "outbound_files",
    "uploads",
    "secrets",
    "runtime",
    "workflows",
)


@dataclass(frozen=True)
class PendingMigration:
    old_home: Path
    new_home: Path


def pending_migration_marker_path(old_home: Path) -> Path:
    return Path(old_home) / _PENDING_MIGRATION_FILENAME


def mark_pending_migration(old_home: Path, new_home: Path) -> Path:
    """落待迁移标记（原子写）。供 save_config_yaml 在 home 变化时调用。

    标记写在新 home——启动时生效 home 已是新 home，直接在当前 home 检测即可，
    无需反查旧位置。迁移数据仍从标记里的 old_home 读取。
    """
    old_home = Path(old_home).resolve()
    new_home = Path(new_home).resolve()
    new_home.mkdir(parents=True, exist_ok=True)
    marker = pending_migration_marker_path(new_home)
    write_json_atomic(
        marker,
        {"old_home": str(old_home), "new_home": str(new_home)},
    )
    return marker


def load_pending_migration(home: Path) -> PendingMigration | None:
    """读当前 home 的待迁移标记；不存在/损坏返回 None。"""
    import json

    marker = pending_migration_marker_path(home)
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        old = Path(str(data["old_home"])).resolve()
        new = Path(str(data["new_home"])).resolve()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return PendingMigration(old_home=old, new_home=new)


def clear_pending_migration(home: Path) -> None:
    """迁移完成后清标记（幂等）。"""
    import contextlib

    with contextlib.suppress(OSError):
        pending_migration_marker_path(home).unlink(missing_ok=True)


def home_has_data(home: Path) -> bool:
    """该 home 是否已有用户数据（以 providers.yaml 或 users/ 存在为准）。"""
    home = Path(home)
    return (home / "system" / "providers.yaml").is_file() or (home / "users").is_dir()


def estimate_migration_size(old_home: Path) -> int:
    """估算待迁移数据总字节数（用于进度提示与空间预检；失败返回 0）。"""
    total = 0
    for entry in _MIGRATE_ENTRIES:
        src = Path(old_home) / entry
        if not src.exists():
            continue
        try:
            if src.is_file():
                total += src.stat().st_size
                continue
            for item in src.rglob("*"):
                if item.is_file():
                    try:
                        total += item.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _copy_entry(src: Path, dst: Path, *, copied: list[Path], skipped: list[Path]) -> None:
    """复制单个目录/文件；已存在的目标文件跳过（不覆盖新数据 幂等）。"""
    if src.is_file():
        if dst.exists():
            skipped.append(dst)
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst)
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            skipped.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        copied.append(target)


def execute_migration(
    old_home: Path,
    new_home: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """把旧 home 的数据逐层复制到新 home（不覆盖已存在文件 幂等）。

    旧数据原地保留不动（绝不删除——读取打开中的文件安全，移动/删除才有锁冲突）。
    返回 (复制文件数, 跳过文件数)。失败抛异常（调用方决定如何呈现），已复制部分
    保留——幂等设计下次重跑跳过已复制项续迁，不重复不丢。
    """
    old_home = Path(old_home).resolve()
    new_home = Path(new_home).resolve()
    if old_home == new_home:
        return (0, 0)
    copied: list[Path] = []
    skipped: list[Path] = []
    for entry in _MIGRATE_ENTRIES:
        src = old_home / entry
        if not src.exists():
            continue
        if progress is not None:
            progress(entry)
        _copy_entry(src, new_home / entry, copied=copied, skipped=skipped)
    return (len(copied), len(skipped))


__all__ = [
    "PendingMigration",
    "clear_pending_migration",
    "estimate_migration_size",
    "execute_migration",
    "home_has_data",
    "load_pending_migration",
    "mark_pending_migration",
    "pending_migration_marker_path",
]
