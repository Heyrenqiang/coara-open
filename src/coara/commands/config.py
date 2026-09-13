"""Configuration commands: /model /thinking /theme /sandbox.

These commands query or toggle runtime configuration switches (LLM selection,
thinking mode, color theme, sandbox). Each returns structured state in ``data``
so frontends can render toggle UIs without re-querying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register, resolve_target_coara
from src.coara.commands.types import CommandResult

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("model")
async def handle_model(root: RootCoara, args: CommandArgs) -> CommandResult:
    """List or switch the active LLM model."""
    from src.core.config import config_manager
    from src.core.errors import ConfigError, ProviderNotFoundError
    from src.llm.model_catalog import list_model_choices, resolve_model_by_index, resolve_model_selection

    target = resolve_target_coara(root, args)
    current = f"{target.provider_name}/{target.model_name}"

    # /model — list available models
    if args.sub is None:
        choices = list_model_choices(config_manager)
        lines = [f"可用模型（当前 {current}）："]
        for idx, choice in enumerate(choices, start=1):
            mark = "*" if choice.key == current else " "
            lines.append(f"{mark} {idx}. {choice.label}")
        lines.append(
            "用法：输入 /model 后 ↑↓ 选择回车（Esc 取消）；"
            "或 /model <模型名> ｜ /model <供应商>/<模型> ｜ /model <序号>（绑定当前工作空间）；"
            "/model --global <…> 改全局默认；/model --add 添加模型（选厂商填 API key 即可用）"
        )
        return CommandResult(
            output="\n".join(lines),
            data={
                "current": current,
                "choices": [{"idx": i + 1, "key": c.key, "label": c.label} for i, c in enumerate(choices)],
            },
        )

    # /model <arg> [<model>] [--global]
    # Use raw parts to preserve case (provider names may be case-sensitive).
    raw_parts = args.parts[1:]
    global_scope = "--global" in raw_parts
    parts = [p for p in raw_parts if p != "--global"]
    arg = parts[0] if parts else None
    model_arg = parts[1] if len(parts) > 1 else None

    # /model --add：添加模型（选已支持厂商 → 填 API key → 写 .env 生效并设默认）
    if arg == "--add":
        return await _handle_model_add(root)

    if arg is None:
        return CommandResult("用法：/model <模型名|供应商/模型|序号> [--global]", data={"usage": True})
    try:
        if arg.isdigit():
            provider_name, model_name = resolve_model_by_index(config_manager, int(arg))
        else:
            provider_name, model_name = resolve_model_selection(config_manager, arg, model_arg)
        if global_scope:
            applied_provider, applied_model = root.switch_llm_global(
                provider_name, model_name, origin_source=args.origin_source
            )
        elif args.target_coara is not None:
            # 端 pin 视图空间：切该空间会话并绑定到该空间（不漂移到全局前台）。
            applied_provider, applied_model = root.switch_llm_target(
                args.target_coara, provider_name, model_name, origin_source=args.origin_source
            )
        else:
            applied_provider, applied_model = root.switch_llm(
                provider_name, model_name, origin_source=args.origin_source
            )
    except (ConfigError, ProviderNotFoundError) as exc:
        return CommandResult.error(f"无法切换模型：{exc}")

    # 回合进行中时 switch_llm* 标记延迟 回合结束后生效
    deferred = getattr(target, "_pending_llm_switch", None) is not None

    # 工作空间 scope 的持久化在 switch_llm* 内写入注册表条目；--global 写全局默认
    if global_scope:
        persist_note = "已保存为全局默认（新工作空间与未绑定空间生效）"
    elif args.target_coara is not None and root.workspace_manager is not None:
        # pin 语义：绑定名取 target 自身空间，不跟随全局前台 active_entry
        entry = next(
            (e for e in root.workspace_manager.list_workspaces() if str(e.path) == str(target.workspace_dir)),
            None,
        )
        persist_note = f"已绑定到工作空间 {entry.name}（仅该空间生效）" if entry else "已绑定到当前工作空间"
    else:
        ws_name = ""
        manager = getattr(root, "workspace_manager", None)
        if manager is not None and getattr(manager, "active_entry", None) is not None:
            ws_name = manager.active_entry.name
        persist_note = f"已绑定到工作空间 {ws_name}（仅该空间生效）" if ws_name else "已绑定到当前工作空间"

    if deferred:
        output = f"已标记切换 → {applied_provider}/{applied_model}（将在当前回合结束后生效，本回合仍用原模型）"
    else:
        output = f"已切换 → {applied_provider}/{applied_model}"

    return CommandResult(
        output=output,
        data={
            "provider": applied_provider,
            "model": applied_model,
            "persist_note": persist_note,
            "scope": "global" if global_scope else "workspace",
            "deferred": deferred,
        },
    )


async def _handle_model_add(root: RootCoara) -> CommandResult:
    """/model --add：复用统一的「添加模型」流程，成功后切到该厂商默认模型。

    流程本体在 first_run_setup.run_add_model_flow（与首发启动共用，绝不两套）：
    选已支持厂商 → 填 API key → 写 system/.env 生效并设默认。这里只补
    运行时模型切换，让用户填完立刻可用。
    """
    from src.coara.frontend import get_frontend

    provider_name = await get_frontend().run_add_model_flow(None)
    if not provider_name:
        return CommandResult("已取消或未填写有效 API key", data={"cancelled": True})

    try:
        applied_provider, applied_model = root.switch_llm(provider_name, None)
        return CommandResult(
            output=f"已添加并切换 → {applied_provider}/{applied_model}",
            data={"provider": applied_provider, "model": applied_model},
        )
    except Exception:
        return CommandResult(
            output=f"已添加 {provider_name} 的密钥并设为默认厂商，请用 /model 选择模型",
            data={"provider": provider_name},
        )


@register("thinking")
async def handle_thinking(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show or toggle LLM thinking / reasoning mode."""
    from src.llm.active_context import resolve_active_llm
    from src.llm.thinking_mode import run_thinking_command

    ctx = resolve_active_llm(root)
    result = run_thinking_command(
        args.raw,
        model=ctx.model,
        base_url=ctx.base_url,
        driver=ctx.driver,
        provider_name=ctx.provider_name,
    )
    return CommandResult(
        output="\n".join(result.lines),
        data={"lines": result.lines},
    )


@register("theme")
async def handle_theme(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show or switch the CLI color theme (dark/light)."""
    from src.coara.frontend import get_frontend
    from src.core.config import config_manager
    from src.core.errors import ConfigError

    theme = get_frontend().theme
    if theme is None:
        return CommandResult("配色主题仅在 CLI 端可用", data={"usage": True})

    if args.sub is None:
        current = theme.get_theme().name
        return CommandResult(
            output=f"配色主题：{current}\n用法：/theme dark ｜ /theme light",
            data={"theme": current},
        )

    name = theme.resolve_theme_name(args.sub)
    if name is None:
        return CommandResult("用法：/theme ｜ /theme dark ｜ /theme light", data={"usage": True})

    theme.set_active_theme(name)
    persisted = True
    try:
        config_manager.save_config_yaml({"cli": {"theme": name}})
    except ConfigError:
        persisted = False
    note = "已保存，重启后保持" if persisted else "未找到可写 config.yaml，仅本会话生效"
    return CommandResult(
        output=f"配色主题 → {name}（对话正文与工具行即时生效；输入区与状态栏重启后生效。{note}）",
        data={"theme": name, "persisted": persisted},
    )


@register("sandbox")
async def handle_sandbox(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Toggle execution sandbox on/off."""
    from src.tools.sandbox import toggle_sandbox

    new_state = toggle_sandbox()
    return CommandResult(
        output=f"沙箱：{'开' if new_state else '关'}（仅对不受信的调用生效）",
        data={"enabled": new_state},
    )
