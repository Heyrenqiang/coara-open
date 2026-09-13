"""coara_home 切换的启动期迁移确认与执行（CLI）。

启动早期（config 加载后、各模块初始化前、数据文件尚未打开的安全窗口）检测
当前 home 的待迁移标记：交互模式提示用户确认后全套复制旧 home → 新 home；
非交互模式（-w 纯 Web 无 TTY）只警告不自动迁，等用户在 CLI 确认。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from src.core.home_migration import (
    clear_pending_migration,
    estimate_migration_size,
    execute_migration,
    home_has_data,
    load_pending_migration,
)


def _format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "未知大小"
    mb = num_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


async def maybe_run_home_migration(console: Any, config: Any, *, enable_cli: bool) -> None:
    """检测当前 home 的待迁移标记并执行迁移（确认后）。"""
    new_home = getattr(config, "coara_home", None)
    if new_home is None:
        return
    new_home = Path(new_home)
    pending = load_pending_migration(new_home)
    if pending is None:
        return
    old_home = pending.old_home
    # 旧 home 无数据（极端：标记残留/数据已手动清）→ 直接清标记不打扰
    if not home_has_data(old_home):
        clear_pending_migration(new_home)
        return

    interactive = enable_cli and sys.stdin.isatty() and sys.stdout.isatty()
    size_text = _format_size(estimate_migration_size(old_home))
    if not interactive:
        console.print(
            f"[yellow]检测到系统目录从 {old_home} 切换到 {new_home}，"
            f"有 {size_text} 数据待迁移。请以交互模式（coara -c）启动一次完成迁移。[/yellow]"
        )
        return

    console.print(
        f"[yellow]检测到系统目录已从 {old_home} 切换到 {new_home}。\n"
        f"全部数据（配置/记录/会话历史/工作区，约 {size_text}）需要迁移到新目录，"
        f"这次启动会稍慢。旧数据会原样保留，不会删除。[/yellow]"
    )
    try:
        import questionary

        confirmed = await questionary.confirm("现在迁移全部数据吗？", default=True).ask_async()
    except Exception:
        confirmed = True  # 交互组件异常时默认迁移（数据不迁等于历史丢失）
    if not confirmed:
        console.print("[yellow]已跳过迁移。旧数据仍在原目录，下次启动会再次提示。[/yellow]")
        return

    def _progress(entry: str) -> None:
        console.print(f"[dim]  迁移 {entry}/ ...[/dim]")

    try:
        copied, skipped = execute_migration(old_home, new_home, progress=_progress)
    except Exception as exc:
        console.print(f"[red]数据迁移失败：{exc}[/red]")
        console.print("[yellow]已复制部分保留，下次启动会续迁。旧数据未受影响。[/yellow]")
        return
    clear_pending_migration(new_home)
    console.print(
        f"[green]数据迁移完成：复制 {copied} 个文件"
        + (f"，跳过已存在 {skipped} 个" if skipped else "")
        + f"。旧数据保留在 {old_home}，确认无误后可手动删除。[/green]"
    )
