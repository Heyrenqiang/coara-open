"""Vault slash commands: /vault (status | open hint | lock)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("vault")
async def handle_vault(root: RootCoara, args: CommandArgs) -> CommandResult:
    service = getattr(root, "vault_service", None)
    if service is None:
        return CommandResult.error("保险柜未启用（可在配置里打开 vault_enabled）")

    if args.sub == "lock":
        service.lock()
        return CommandResult.text("保险柜已锁定", locked=True)

    if args.sub in ("init", "setup"):
        return await _init_vault(service)

    if args.sub == "passwd":
        return await _change_password(service)

    return _build_status_result(service)


async def _init_vault(service) -> CommandResult:
    """交互设置保险柜主密码（未初始化时创建并解锁）。"""
    if service.is_initialized():
        return CommandResult("保险柜已初始化。修改密码用 /vault passwd。")
    try:
        import questionary
    except ImportError:
        return CommandResult.error("交互设置需要 questionary，请 pip install questionary")
    pw = (await questionary.password("设置主密码（≥8 位，字母数字混合）：").ask_async() or "")
    if not pw:
        return CommandResult("已取消", data={"cancelled": True})
    pw2 = (await questionary.password("再次输入主密码：").ask_async() or "")
    if pw != pw2:
        return CommandResult.error("两次输入不一致，未创建。")
    try:
        service.setup_or_unlock(pw, persistent=False, enforce_strength=True)
    except ValueError as exc:
        return CommandResult.error(f"密码强度不足：{exc}")
    return CommandResult("保险柜已创建并设置主密码。用 /vault status 查看，或让助手打开保险柜。")


async def _change_password(service) -> CommandResult:
    """交互修改保险柜主密码（需旧密码）。"""
    if not service.is_initialized():
        return CommandResult("保险柜尚未初始化。先 /vault init 设置主密码。")
    try:
        import questionary
    except ImportError:
        return CommandResult.error("交互设置需要 questionary，请 pip install questionary")
    old = (await questionary.password("旧主密码：").ask_async() or "")
    new = (await questionary.password("新主密码（≥8 位，字母数字混合）：").ask_async() or "")
    new2 = (await questionary.password("再次输入新主密码：").ask_async() or "")
    if new != new2:
        return CommandResult.error("两次新密码输入不一致。")
    try:
        count = service.change_password(old_password=old, new_password=new)
    except ValueError as exc:
        return CommandResult.error(f"修改失败：{exc}")
    return CommandResult(f"主密码已修改，已重新加密 {count} 个文件。")


def _build_status_result(service) -> CommandResult:
    status = service.status()
    init_text = "已初始化" if status.initialized else "未初始化"
    lock_text = "已锁定" if status.locked else "已打开"
    lines = [
        f"保险柜：{init_text} · {lock_text}",
        f"已封存：{status.entries} 项",
        f"位置：{status.vault_dir}",
    ]
    if service.is_unlocked():
        lines.append("当前已打开，可用普通文件工具读写；要上锁可以说 关保险柜 或 /vault lock")
    elif not status.initialized:
        lines.append("还没初始化：输入 /vault init 设置主密码，或让助手打开保险柜创建")
    else:
        lines.append("已锁定：让助手执行打开保险柜（会弹窗要密码）")
    return CommandResult.text(
        "\n".join(lines),
        initialized=status.initialized,
        locked=status.locked,
        entries=status.entries,
    )
