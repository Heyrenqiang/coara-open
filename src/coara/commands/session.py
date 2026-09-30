"""Session-level commands: /help /new /stop /status"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.coara.commands.registry import CommandArgs, register, resolve_target_coara
from src.coara.commands.types import CommandResult
from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("help")
async def handle_help(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show available commands and shortcuts."""
    return CommandResult(output=_HELP_TEXT, data={"help_text": _HELP_TEXT})


@register("exit")
@register("quit")
async def handle_exit(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Exit the CLI session."""
    return CommandResult.exit_("已退出")


@register("new")
async def handle_new(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Start a new session, clearing message history."""
    target = resolve_target_coara(root, args)
    await _run_janitor_before_new(root, target)
    # 发起端标识随 interrupt_source 下发（web/matrix/cli-attached 区分于本端 CLI），
    # 供同空间其它端的 session_started 订阅区分「自己 new 的」与「他端 new 的」。
    source = args.origin_source
    interrupt_source = f"{source}_new_command" if source else "new_command"
    if args.target_coara is not None:
        # 端 pin 视图空间：直接重开该空间会话（不漂移到全局前台）。
        new_session_id = await target.start_new_session(interrupt_source=interrupt_source)
    else:
        new_session_id = await root.start_new_session(interrupt_source=interrupt_source)
    # 手动 /new：释放本会话主体的 flow（短生命周期探索编排，新会话从头开始）。
    # 不覆盖 idle / workspace_stale 自动新会话——避免误杀其他空间正在跑的 flow。
    try:
        await target.flow_coordinator.reset()
    except Exception as exc:
        logger.warning(f"flow coordinator reset after /new failed: {exc}")

    log_path = root.get_status().get("errors_log_path")
    # Ack text must match Android's SESSION_BOUNDARY_ACK_REGEX
    return CommandResult(
        output=f"已开始新会话：`{new_session_id}`",
        action="new_session",
        data={"session_id": new_session_id, "errors_log_path": log_path},
    )


async def _run_janitor_before_new(root: RootCoara, coara: Any) -> None:
    """Dispatch background janitor for the session being cleared (non-blocking)."""
    if coara is None:
        logger.info("janitor before /new skipped: no target coara")
        return
    from src.coara.workspace_state import workspace_session_has_conversation

    if not workspace_session_has_conversation(coara):
        logger.info("janitor before /new skipped: session has no real conversation")
        return
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        logger.info("janitor before /new skipped: no workspace manager")
        return
    workspace_dir = str(getattr(coara, "workspace_dir", "") or "")
    workspace_name = _workspace_name_for_dir(root, workspace_dir)
    if not workspace_name:
        # 降级兜底：目标会话恰好是全局前台时用前台名（兼容旧测试夹具与未登记目录）。
        try:
            if coara is root.foreground_coara:
                workspace_name = root.foreground_active_name() or ""
        except Exception:  # noqa: BLE001
            pass
    if not workspace_name or not workspace_dir:
        logger.info(
            f"janitor before /new skipped: empty workspace name/dir (name={workspace_name!r}, dir={workspace_dir!r})"
        )
        return

    # Ensure disk holds the conversation janitor should summarize.
    try:
        await asyncio.to_thread(coara.persist_session_to_disk)
    except Exception:
        logger.warning("janitor: persist before /new failed; dispatching anyway")

    from src.coara.workspace_protocol import dispatch_janitor_background

    await dispatch_janitor_background(
        root,
        workspace_name=workspace_name,
        workspace_dir=workspace_dir,
        coara_home=str(wm.coara_home),
        force=True,  # 手动 /new 是显式意图，绕过冷却
    )


def _workspace_name_for_dir(root: RootCoara, workspace_dir: str) -> str:
    """按会话的 workspace_dir 反查登记名（会话主体自带目录，不依赖全局前台）。"""
    if not workspace_dir:
        return ""
    try:
        wm = getattr(root, "workspace_manager", None)
        if wm is None:
            return ""
        from src.core.coara_home import workspace_id_for

        entry = wm.registry.get_by_id(workspace_id_for(str(Path(workspace_dir).expanduser().resolve())))
        return str(getattr(entry, "name", "") or "") if entry is not None else ""
    except Exception:  # noqa: BLE001
        return ""


@register("stop")
async def handle_stop(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Interrupt the current turn."""
    target = resolve_target_coara(root, args)
    if target.interrupt_current_turn(
        "user_stop",
        interrupt_source="stop_command",
        take_ownership_source=str(args.origin_source or ""),
    ):
        return CommandResult(output="已中断当前回合", data={"interrupted": True})
    return CommandResult(output="当前没有运行中的回合", data={"interrupted": False})


def _status_label_zh(raw: str) -> str:
    return {
        "idle": "空闲",
        "running": "工作中",
        "terminated": "已结束",
        "error": "异常",
    }.get(str(raw or "").strip().lower(), str(raw or "未知"))


def _turn_phase_line(root: RootCoara, args: CommandArgs | None = None) -> str:
    """回合中的相位描述（等 API / 收 API / 跑工具 / 本地处理 + 耗时）；取不到返回空串."""
    try:
        fg = resolve_target_coara(root, args) if args is not None else root.foreground_coara
        if not fg.has_active_turn():
            return ""
        return fg._turn_phase.describe()
    except Exception:
        return ""


@register("status")
async def handle_status(root: RootCoara, args: CommandArgs) -> CommandResult:
    """Show coara runtime status (user-facing Chinese summary)."""
    status_data = resolve_target_coara(root, args).get_status() if args.target_coara is not None else root.get_status()
    data: dict = dict(status_data)

    name = str(status_data.get("name") or "考拉")
    provider = str(status_data.get("provider") or "").strip()
    model = str(status_data.get("model") or "").strip()
    model_line = f"{provider} / {model}" if provider and model else (model or provider or "未配置")

    workspace = str(status_data.get("workspace_dir") or "").strip()
    msg_count = int(status_data.get("message_count") or 0)
    tool_count = len(status_data.get("tools") or [])
    skill_count = len(status_data.get("skills") or [])
    vis = status_data.get("tool_visibility") or {}
    hidden_n = len(vis.get("hidden") or {})

    lines = [
        "考拉状态",
        "",
        f"当前：{_status_label_zh(str(status_data.get('status') or ''))}",
        f"名称：{name}",
        f"模型：{model_line}",
    ]
    # 回合中追加相位行：等 API / 收 API / 跑工具 / 本地处理 + 已耗时
    phase_desc = _turn_phase_line(root, args)
    if phase_desc:
        data["turn_phase"] = phase_desc
        lines.append(f"回合阶段：{phase_desc}")
    if workspace:
        lines.append(f"工作空间：{workspace}")
    lines.append(f"本轮对话：{msg_count} 条消息")
    tools_line = f"工具：{tool_count} 个可用"
    if skill_count:
        tools_line += f" · 技能 {skill_count} 个"
    if hidden_n:
        tools_line += f" · 另有 {hidden_n} 个已隐藏（详看 /tools）"
    lines.append(tools_line)

    lines.append("")
    lines.append("偏好")

    from src.llm.active_context import resolve_active_llm
    from src.llm.thinking_mode import describe_thinking_status

    llm_ctx = resolve_active_llm(root)
    thinking_enabled, thinking_source, thinking_note = describe_thinking_status(
        llm_ctx.model,
        base_url=llm_ctx.base_url,
        driver=llm_ctx.driver,
    )
    data["thinking_enabled"] = thinking_enabled
    data["thinking_source"] = thinking_source
    data["thinking_note"] = thinking_note
    lines.append(f"· 思考模式：{'开' if thinking_enabled else '关'}（{thinking_source}）")
    if thinking_note and "不支持" in thinking_note:
        lines.append(f"  {thinking_note}")

    return CommandResult(output="\n".join(lines), data={"status": data})


_HELP_TEXT = """常用命令：
  /help       帮助
  /status     当前状态（模型、工作空间、偏好）
  /model      列出 / 切换模型（无可用模型时打开配置页「模型」添加密钥）
  /new        新开一轮对话
  /compact    压缩当前会话历史（摘要可回取）
  /usage [N]  本轮或最近 N 天用量
  /log        工具执行记录（/log 列表；/log <序号> 展开）
  /report     向开发者提交问题报告（可附描述）
  /ws         工作空间（↑↓ 切换；也可 /ws <序号> 或 /ws switch <名>）
  /ws rename  旧名 新名   重命名（路径不动）
  /ws default <名>        设启动默认
  /ws updates list [名]   看未读动态
  /events     事件源（↑↓；/events reload 热重载）
  /tools      工具开关（/tools off|on <名称>）
  /sandbox    切换沙箱
  /thinking   思考模式
  /theme      配色（dark / light）
  /message    系统消息
  /login      登录
  /qrcode     终端显示手机配对二维码
  /email      配置邮箱
  /restart    重启内核（仅托管进程可用）

回复进行中也可立刻执行：/qrcode /help /status /tools /events /ws /login /model /report /message /log
（其余 slash 等本轮结束后再跑；普通文字进接续输入；打断用 Ctrl+C）

快捷键：
  Alt+V       粘贴剪贴板图片
  Ctrl+V      粘贴文字
  Ctrl+U      清空输入
  Ctrl+C      有输入则清空；空闲退出；回复中打断
  Ctrl+Enter  换行（Windows Terminal 等亦可用 Shift+Enter）
  Tab         补全命令

启动与子命令：
  coara                 确保内核常驻，本终端作为 CLI 端接入
  coara tray            托盘常驻内核（Web / 手机由内核托管）
  coara attach <空间名>  另开终端接入指定工作空间
  coara status          运行时状态
  coara providers       已配置的 provider
  coara ws list|add|rename|default|remove
"""
