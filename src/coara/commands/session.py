"""Session-level commands: /help /new /stop /status.

These commands manage the chat session lifecycle and surface runtime status.
They call RootCoara public APIs only — no ``console.print``, no Matrix calls.
Rendering is the frontend's job, driven by :class:`CommandResult`.
"""

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
    # (`^(已开始|已启动)新会话：`[^`]+`$`) so the mobile client hides it.
    # Error log path stays in `data` only — not shown to the user.
    return CommandResult(
        output=f"已开始新会话：`{new_session_id}`",
        action="new_session",
        data={"session_id": new_session_id, "errors_log_path": log_path},
    )


async def _run_janitor_before_new(root: RootCoara, coara: Any) -> None:
    """Dispatch background janitor for the session being cleared (non-blocking).

    绑定对象必须是 ``/new`` 实际作用的会话主体（调用端 pin 的视图空间），
    不是全局前台——手机端 new 掉 nx 的会话时，要维护的是 nx，而非 web 那边的
    前台空间。Persists the session, snapshots history for the janitor, then
    schedules maintenance without waiting. The new session proceeds immediately
    with whatever ws.md is on disk; janitor writes for the next session boundary.
    """
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
            f"janitor before /new skipped: empty workspace name/dir "
            f"(name={workspace_name!r}, dir={workspace_dir!r})"
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
    if resolve_target_coara(root, args).interrupt_current_turn("user_stop", interrupt_source="stop_command"):
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
  /help      帮助
  /status    当前状态（模型、工作空间、偏好）
  /model     列出/切换模型（输入后 ↑↓ 选择；Esc 取消；底部「＋添加模型」填 API key 即可用）
  /new       新开一轮对话
  /compact   手动压缩当前会话历史（LLM 摘要，被替代部分归档可回取）
  /log       工具执行记录（/log 列表；/log <序号> 展开完整输出）
  /report    向开发者提交问题报告（附带本轮会话；可 /report 描述）
  /ws        工作空间（输入后 ↑↓ 切换；Esc 取消）
  /ws <序号> 按列表序号切换
  /ws switch 店名   切换工作空间
  /ws rename 旧名 新名  重命名（路径不动）
  /ws default 店名  设启动默认
  /ws updates list 别名  看未读动态
  /events    事件源（↑↓ 查看/重载）
  /sandbox   切换沙箱（↑↓ 确认）
  /thinking  思考模式（↑↓ on/off/强度）
  /theme     配色主题（dark / light）
  /tools     工具开关（/tools off <名称> 停用；/tools on <名称> 启用）
  /vault     保险柜（↑↓ 状态/锁定）
  /message   系统消息（如 API key 提醒）
  /qrcode    终端显示手机配对二维码（需 gomatrix 隧道就绪）

回复进行中也可立刻执行：/qrcode /help /status /tools /events /vault /ws /login /model /report /message /log
（其余 slash 等本轮结束后再跑；普通文字进接续输入；打断回复用 Ctrl+C）

快捷键（CLI 端）：
  Alt+V      粘贴剪贴板图片
  Ctrl+V     粘贴文字
  Ctrl+U     清空输入
  Ctrl+C     有输入则清空；空闲退出；回复中打断
  Ctrl+Enter 换行（支持 kitty 键盘协议的终端如 Windows Terminal 也可用 Shift+Enter）
  Tab        补全命令

快捷键（Web 端）：
  Shift+Enter  换行
  Enter        发送

启动：
  coara          对话 + 网页 + 手机（三端同启）
  coara -c       仅对话（CLI）
  coara -w       仅网页
  coara -x       仅手机（Matrix）
  coara -cw      对话 + 网页（标志可组合）
  coara status
  coara providers
  coara search "关键词"
  coara ws list
  coara ws add <路径> --name <名>
  coara ws rename <旧名> <新名>
  coara ws default <工作空间名>
"""
