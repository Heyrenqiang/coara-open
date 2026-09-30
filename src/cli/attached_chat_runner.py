"""CLI 会话：经 /ws/attach 连内核，跑完整交互界面"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from rich.console import Console

from src.cli.display_controller import CliDisplayController
from src.cli.interactive_prompt import set_modal_spinner
from src.cli.session import (
    ChatTurnInterruptState,
    _build_chat_prompt_session,
    _collect_chat_turn_streaming,
    _prompt_loop,
    _PromptCtrlC,
    _PromptExit,
    await_chat_turn_input,
    install_chat_session_sigint_handler,
)
from src.cli.spinner import BackgroundSpinner, SubagentSpinnerManager
from src.core.logger import logger


def _drain_ctrl_messages(queue: asyncio.Queue[Any]) -> None:
    """Remove pending Ctrl+C / exit sentinels sent while a turn was running."""
    sentinel_types = (_PromptCtrlC, _PromptExit)
    pending: list[Any] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            if not isinstance(item, sentinel_types):
                pending.append(item)
        except asyncio.QueueEmpty:
            break
    for item in pending:
        queue.put_nowait(item)


def _read_local_cli_theme_config(workspace: Path) -> dict[str, Any]:
    """CLI 主题读取本地配置（``cli.theme`` 是客户端渲染设置，不是内核逻辑）"""
    from src.ui.web_link import iter_local_user_config_yaml

    for data in iter_local_user_config_yaml(workspace):
        if isinstance(data.get("cli"), dict):
            return {"cli": data["cli"]}
    return {}


async def run_attached_chat_session(root: Any, *, workspace: Path, console: Console) -> None:
    """在已接入内核的 RootShim 上跑完整 CLI 界面（照搬 run_chat_session 骨架）"""
    from src.cli import theme as cli_theme
    from src.cli.remote_event_bus import Subscription

    _subscriptions: list[Subscription] = []

    # 配置在内核侧；CLI 主题 token 是客户端渲染设置，读本地 config.yaml 的
    # cli 段（内核 config 不传给客户端；读不到就跳过，回退默认 dark）。
    with contextlib.suppress(Exception):
        cli_theme.apply_theme_from_config(_read_local_cli_theme_config(workspace))
    # 否则 Thinking 行永远只播常驻池，daily 当日定制词在 CLI attach 端不上屏。
    with contextlib.suppress(Exception):
        from src.records import loading_phrases
        from src.ui.web_link import resolve_web_coara_home

        agent_dir = resolve_web_coara_home(workspace) / "users" / "default" / "records" / "agent"
        loading_phrases.set_custom_phrases_path(agent_dir / loading_phrases.CUSTOM_PHRASES_FILENAME)
        loading_phrases.reshuffle()
    alias = root.workspace_manager.active_name if root.workspace_manager else "?"
    console.print(f"[dim]workspace {alias} ({workspace})[/dim]")
    # 无可用 key 时打开配置页「模型」（走 open_or_focus：有标签只导航，没有才开窗）。
    # 有模型时不自动弹浏览器——托盘常驻 / CLI attach 都安静。
    with contextlib.suppress(Exception):
        fg = getattr(root, "foreground_coara", root)
        has_key = getattr(fg, "_provider_has_key", None)
        if has_key is False:
            import os

            from src.ui.dashboard_tokens import load_or_create_dashboard_token
            from src.ui.web_link import resolve_web_coara_home
            from src.ui.web_server import open_or_focus_web_ui

            home = resolve_web_coara_home(workspace)
            token = load_or_create_dashboard_token(workspace, home) if home else ""
            port = int(os.environ.get("COARA_WEB_PORT", "8080"))
            # 浏览器已弹到配置页，终端不再重复提示（09-25 用户口径：启动期噪音）
            open_or_focus_web_ui(
                host="127.0.0.1",
                port=port,
                token=token,
                path="/config?focus=models",
            )
    subagent_spinner = SubagentSpinnerManager()
    background_spinner = BackgroundSpinner()
    background_spinner.bind_subagent_spinner(subagent_spinner)
    background_spinner.bind_root(root)

    prompt_session = _build_chat_prompt_session(
        workspace,
        message_factory=background_spinner,
        bottom_toolbar=background_spinner.bottom_toolbar,
        spinner=background_spinner,
    )
    background_spinner.bind_session(prompt_session)
    background_spinner.bind_root(root)
    set_modal_spinner(background_spinner)
    background_spinner.start()

    from src.cli.slash_pickers import bind_root_pickers

    bind_root_pickers(prompt_session, root)

    # 握手快照里的服务台名单 → @ 补全
    desk_completer = getattr(prompt_session, "coara_desk_completer", None)
    desks = getattr(root, "service_desks", None)
    if desk_completer is not None and hasattr(desk_completer, "set_desks"):
        with contextlib.suppress(Exception):
            desk_completer.set_desks(list(desks or []))

    from src.cli.frontend_hooks import wire_cli_frontend_hooks

    wire_cli_frontend_hooks(console=console)

    display = CliDisplayController(
        root=root,
        console=console,
        subagent_spinner=subagent_spinner,
        background_spinner=background_spinner,
    )
    # diff 统一走通道帧：服务端 EndRegistry.route → cli-attached sender →
    # TurnStream diff 帧 → 客户端 _feed_turn_frame → display.queue_diff_frame。
    root._diff_callback = display.queue_diff_frame
    # 子智能体正文/最终结果帧（kind 保留的独立帧型）：前台滚动区不认它们，
    # 交折叠块消费（根 shim 已挡下不进前台回合流）。
    root._subagent_frame_callback = display.ingest_subagent_frame

    # `/detail` 补全候选＝折叠块摘要（display 建好后才有数据源，故在此绑定）。
    from src.cli.slash_pickers import bind_detail_picker

    bind_detail_picker(prompt_session, display)

    # （只暂扣「看起来是工具行」的半行，回合收尾 flush）。
    from src.cli.subagent_fold import DelegateLineFilter

    delegate_lines = DelegateLineFilter()

    def _visible_chunk(text: str) -> str:
        """过滤后的可见正文（空串＝整段被折叠块吸收，不该上屏）。"""
        visible = delegate_lines.feed(text)
        return visible

    def _on_replay_frame(frame: dict) -> None:
        """服务端重放 / 唤醒直渲帧：本地无活跃回合流时直接渲染"""
        frame_type = str(frame.get("type") or "")
        if frame_type in ("chunk", "chat_chunk"):
            text = _visible_chunk(str(frame.get("text") or frame.get("chunk") or ""))
            if text.strip():
                background_spinner.ensure_streaming()
                background_spinner.append_streaming_chunk(text)
        elif frame_type == "tool":
            text = str(frame.get("text") or "")
            if text.strip():
                if not text.endswith("\n"):
                    text = f"{text}\n"
                visible = _visible_chunk(text)
                if visible.strip():
                    is_error = bool(frame.get("is_error")) or frame.get("ok") is False
                    background_spinner.ensure_streaming()
                    background_spinner.append_streaming_chunk(visible, is_error=is_error)
        elif frame_type == "diff":
            display.queue_diff_frame(frame)
        elif frame_type in ("turn_end", "chat_end"):
            display.flush_pending_fg_diffs()
            background_spinner.stop_streaming()

    root._replay_callback = _on_replay_frame

    # 不再永挂等满 300s。
    _approval_tasks: dict[str, asyncio.Task] = {}

    def _on_approval_frame(frame: dict) -> None:
        """attach 审批帧：弹 CLI modal 让用户决定，答复回传服务端 approval_reply"""

        async def _ask() -> None:
            from src.cli.interactive_prompt import prompt_select

            question = str(frame.get("question") or "确认执行？")
            options = frame.get("options") if isinstance(frame.get("options"), list) else None
            if not options:
                options = [{"label": "同意", "description": ""}, {"label": "不同意", "description": ""}]
            try:
                result = await prompt_select(question, options, timeout=300.0)
            except Exception:
                result = None
            approve_label = str(options[0].get("label") or "")
            approved = bool(result and str(result.get("selection") or "") == approve_label)
            label = str(result.get("selection") or "") if isinstance(result, dict) else ""
            try:
                await root._send(
                    {
                        "type": "approval_reply",
                        "approval_id": str(frame.get("approval_id") or ""),
                        "approved": approved,
                        "label": label,
                    }
                )
            except Exception:  # noqa: BLE001
                logger.debug("attach approval reply send failed", exc_info=True)

        task = asyncio.create_task(_ask())
        approval_id = str(frame.get("approval_id") or "")
        if approval_id:
            _approval_tasks[approval_id] = task
            task.add_done_callback(lambda _t, aid=approval_id: _approval_tasks.pop(aid, None))

    root._approval_callback = _on_approval_frame

    def _on_approval_resolved(frame: dict) -> None:
        """服务端已收口（超时/取消/断连）：取消本地挂着的 modal 任务。"""
        approval_id = str(frame.get("approval_id") or "")
        task = _approval_tasks.pop(approval_id, None)
        if task is not None and not task.done():
            task.cancel()

    root._approval_resolved_callback = _on_approval_resolved

    # 断连重连后的活动树整树重建（丢帧兜底）；切空间 chrome 走完整 resync。
    root._display_resync_callback = display._resync_activity_tree
    root._display_chrome_resync_callback = display._resync_runtime_chrome

    _subscriptions.extend(display.wire(root.event_bus))
    # 召回后 CLI 是平等 attach 客户端，各端完全独立零同步——web/手机端的对话

    console.print("[dim]输入 /help 看命令，或直接问我任何事。[/dim]")

    input_queue: asyncio.Queue[Any] = asyncio.Queue()
    # Burst batching state is owned by this chat loop (no module-global sharing).
    from src.cli.burst_input import BurstInputState

    burst_state = BurstInputState()

    async def _on_busy_slash(command: str) -> None:
        """Run mid-turn-safe slash commands (e.g. /qrcode, /stop) without waiting."""
        await handle_attached_chat_command(root, command, workspace, display, background_spinner)

    def _start_prompt_loop() -> asyncio.Task[Any]:
        return asyncio.create_task(
            _prompt_loop(
                prompt_session,
                input_queue,
                on_interrupt=_interrupt_current_turn_nowait,
                on_new_session=_interrupt_active_turn_nowait,
                root=root,
                on_busy_slash=_on_busy_slash,
            )
        )

    def _interrupt_current_turn_nowait() -> bool:
        """同步打断入口（_prompt_loop / SIGINT 是同步上下文）"""
        has_work = getattr(root, "has_running_work", None)
        if callable(has_work):
            if not has_work():
                return False
        elif not root.has_active_turn():
            return False
        root.foreground_coara.interrupt_current_turn(
            "user_ctrl_c",
            interrupt_source="cli_prompt_ctrl_c",
        )
        return True

    def _interrupt_active_turn_nowait() -> bool:
        """`/new` 前的清场打断：只停进行中的回合，后台任务不动"""
        if not root.has_active_turn():
            return False
        root.foreground_coara.interrupt_current_turn(
            "user_new_session",
            interrupt_source="cli_new_session",
        )
        return True

    prompt_task = _start_prompt_loop()

    def _ensure_prompt_task() -> None:
        nonlocal prompt_task
        if prompt_task.done():
            prompt_task = _start_prompt_loop()

    from src.cli.shutdown_signals import (
        install_sigterm_handler,
        install_windows_console_close_handler,
        make_minimal_sync_cleanup,
        make_sigterm_exit_request,
    )

    _loop = asyncio.get_running_loop()
    _on_sigterm = make_sigterm_exit_request(root, input_queue, _loop)
    # 客户端替身各清理步都是安全 no-op（会话落盘在内核）。
    _uninstall_console_close = install_windows_console_close_handler(make_minimal_sync_cleanup(root))

    try:
        from src.cli.burst_input import ChatTurnInput, annotate_burst_message

        with install_sigterm_handler(_on_sigterm), install_chat_session_sigint_handler(root):
            # block when the user switches back, so its tool lines and text
            detached_stream = {"owned": False}

            def _close_detached_stream() -> None:
                if detached_stream["owned"]:
                    background_spinner.stop_streaming()
                    detached_stream["owned"] = False

            def _on_detached_chunk(chunk: str) -> None:
                # 自愈：owned 标志与 spinner 的 block 可能被 reset_after_runtime_change
                # 清得失步（切回时序竞争），每次按 block 实际状态重开，防 chunk 静默丢弃。
                visible = _visible_chunk(chunk)
                if not visible:
                    return
                background_spinner.ensure_streaming()
                detached_stream["owned"] = True
                background_spinner.append_streaming_chunk(visible)

            try:
                while True:
                    _overflow = await root.foreground_coara.drain_continuation_inputs()
                    if _overflow:
                        for _msg in _overflow:
                            if _msg.image_blocks:
                                input_queue.put_nowait(
                                    ChatTurnInput(
                                        text=_msg.text,
                                        image_blocks=_msg.image_blocks,
                                        images_resolved=True,
                                    )
                                )
                            else:
                                input_queue.put_nowait(_msg.text)

                    user_input = await await_chat_turn_input(
                        input_queue,
                        root=root,
                        on_recreate_prompt=_ensure_prompt_task,
                        burst_state=burst_state,
                    )

                    if isinstance(user_input, _PromptExit):
                        console.print("\n[yellow]Goodbye![/yellow]")
                        break

                    if isinstance(user_input, _PromptCtrlC):
                        # the sentinel and continue.
                        continue

                    turn_images: list = []
                    if isinstance(user_input, ChatTurnInput):
                        turn_text = annotate_burst_message(
                            user_input.text,
                            burst_index=user_input.burst_index,
                            burst_total=user_input.burst_total,
                        )
                        turn_images = list(user_input.image_blocks)
                    else:
                        turn_text = user_input

                    # Windows 控制台常把 emoji 收成 UTF-16 代理对（如 \\ud83d\\udc4d）。
                    if isinstance(turn_text, str):
                        from src.utils.text_utils import sanitize_surrogates

                        turn_text = sanitize_surrogates(turn_text)

                    resolved = isinstance(user_input, ChatTurnInput) and user_input.images_resolved
                    paths_found: list = []
                    if not resolved and isinstance(turn_text, str) and turn_text.strip():
                        from src.cli.image_paste import attach_images_from_message_text

                        turn_text, path_blocks, paths_found = attach_images_from_message_text(turn_text)
                        if path_blocks:
                            turn_images = [*turn_images, *path_blocks]

                    if turn_images:
                        try:
                            _pname = str(getattr(root.foreground_coara, "provider_name", "") or "")
                            _mname = str(getattr(root.foreground_coara, "model_name", "") or "")
                        except Exception:
                            _pname, _mname = "", ""
                        from src.core.config import config_manager as _cli_cfg
                        from src.llm.vision import model_vision_explicit

                        # （后端转换层对不支持的模型换成占位文案，不会 400）。
                        if model_vision_explicit(_mname, provider_name=_pname, config_manager=_cli_cfg) is False:
                            console.print(
                                f"[yellow]当前模型 {_mname or '未知'} 不支持图像输入，图片已省略。[/yellow]\n"
                                f"[dim]可用 /model 切换到支持图像的模型后再发送图片。[/dim]"
                            )
                            turn_images = []
                    elif paths_found:
                        console.print(
                            "[yellow]检测到图片路径，但文件不存在或无法读取。[/yellow]\n"
                            "[dim]微信临时目录可能已清理。请重新复制图片后按 Alt+V，"
                            "或先把图片保存到工作区再发送。[/dim]"
                        )
                        continue

                    if isinstance(turn_text, str):
                        turn_text = turn_text.strip()

                    if not turn_text and not turn_images:
                        continue

                    from src.cli.commands import _render_result
                    from src.coara.commands.types import CommandResult as _CmdResult

                    # 斜杠先本地处理，不占 Thinking、也不先打 pending_report RPC。
                    if turn_text.startswith("/"):
                        from prompt_toolkit.patch_stdout import patch_stdout

                        # 若正等 /report 描述，斜杠应清掉 pending——仍走一次轻量探测。
                        pending_result = await root.try_consume_pending_report(turn_text)
                        if isinstance(pending_result, _CmdResult):
                            with patch_stdout(raw=True):
                                _render_result(pending_result)
                            _ensure_prompt_task()
                            background_spinner.reset_after_runtime_change()
                            continue

                        # (esp. right after a mid-turn workspace switch).
                        with patch_stdout(raw=True):
                            should_exit = await handle_attached_chat_command(
                                root, turn_text, workspace, display, background_spinner
                            )
                        _ensure_prompt_task()
                        background_spinner.reset_after_runtime_change()
                        if should_exit:
                            break
                        continue

                    # 普通对话：先亮 Thinking，再 pending_report / chat（避免回车后空等 RPC）。
                    # 斜杠不走这里。活动钟：仅真实对话刷新（CONVENTIONS §活动计时）。
                    root.record_user_activity()

                    turn_completed_normally = False
                    try:
                        interrupt_state = ChatTurnInterruptState()
                        _close_detached_stream()
                        display.begin_turn()
                        try:
                            pending_result = await root.try_consume_pending_report(turn_text)
                            if isinstance(pending_result, _CmdResult):
                                from prompt_toolkit.patch_stdout import patch_stdout

                                with patch_stdout(raw=True):
                                    _render_result(pending_result)
                                _ensure_prompt_task()
                                background_spinner.reset_after_runtime_change()
                                continue

                            chunks: list[str] = []
                            from src.cli.streaming import coerce_stream_piece

                            async for chunk in _collect_chat_turn_streaming(
                                root,
                                turn_text,
                                interrupt_state,
                                image_blocks=turn_images or None,
                                on_detached_chunk=_on_detached_chunk,
                                on_detached_drain_complete=_close_detached_stream,
                            ):
                                text, is_error = coerce_stream_piece(chunk)
                                visible = _visible_chunk(text)
                                if not visible:
                                    # delegate 自己的 ✓ 行：摘要行取代它，不上屏
                                    # （内容已进折叠块）。
                                    continue
                                chunks.append(visible)
                                background_spinner.append_streaming_chunk(
                                    visible, is_error=is_error if is_error else None
                                )
                                if visible.lstrip().startswith(("•", "∙", "·")):
                                    display.flush_pending_fg_diffs()

                            turn_completed_normally = True

                            if interrupt_state.interrupt_requested:
                                if any("当前会话已打断" in c for c in chunks):
                                    console.print("[dim][系统] 当前会话已打断。[/dim]")
                                _drain_ctrl_messages(input_queue)
                                continue
                        finally:
                            # 暂扣的最后半行（跨 chunk 切断的 ✓ 行）随回合收尾吐出
                            tail = delegate_lines.flush()
                            if tail:
                                background_spinner.append_streaming_chunk(tail)
                            # pending_report 命中 / 流式结束 / 中途异常：有序收尾
                            # （原子落盘 → spinner 退 → 静止；diff 已在收尾内）
                            await display.finish_turn_async()
                            _ensure_prompt_task()
                    except KeyboardInterrupt:
                        if turn_completed_normally:
                            # Turn finished but Ctrl+C hit before next input — treat as idle exit.
                            console.print("\n[yellow]Goodbye![/yellow]")
                            break
                        # Ctrl+C propagated during an active turn despite the custom handler.
                        # Treat as an interrupt request rather than an exit.
                        _drain_ctrl_messages(input_queue)
                        _ensure_prompt_task()
                        continue
                    except Exception as exc:
                        # 回合失败已在流内以 Error: … chunk 呈现时不再二次红字+堆栈
                        msg = str(exc).strip()
                        if msg and not msg.lower().startswith("error:"):
                            console.print(f"\n[red]Error: {exc}[/red]")
                        logger.warning("Error processing message: %s", exc)
            except KeyboardInterrupt:
                # Idle SIGINT bubbles from await_chat_turn_input; swallow so Click never prints Aborted!
                console.print("\n[yellow]Goodbye![/yellow]")
    finally:
        if _uninstall_console_close is not None:
            _uninstall_console_close()
        # 退出看门狗：清理体挂死时硬退（与原 CLI 同一套）。
        import os as _os

        _exit_deadline = asyncio.get_event_loop().call_later(12.0, lambda: _os._exit(0))
        try:
            # Cancel all foreground tasks in parallel.
            prompt_task.cancel()

            # Wait for cancelled tasks with a tight timeout.
            pending = [t for t in [prompt_task] if t is not None]
            if pending:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=3.0,
                    )
                # 超时仍未结束的任务继续跑只会拖住 asyncio.run；放弃等待。
                for t in pending:
                    if not t.done():
                        t.cancel()

            background_spinner.stop()
            for sub in _subscriptions:
                sub.unsubscribe()
            # 客户端退出只断开自己：关到内核的 transport（内核继续常驻）。
            transport = getattr(root, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(transport.close(), timeout=2.0)
        except KeyboardInterrupt:
            # Swallow Ctrl+C during cleanup so the user doesn't see "Aborted!".
            pass
        finally:
            # bounded cleanup above（与原 CLI 同一套看门狗语义）。
            with contextlib.suppress(Exception):
                _exit_deadline.cancel()
            _os._exit(0)


# 慢命令（内核要跑 LLM、几十秒才回执）在等待期的 spinner 文案。内核压缩期间
# 不回任何帧——没有这行，用户看到的就是「敲了 /compact 屏幕上什么都不动」。
_SLOW_COMMAND_LABELS = {
    "compact": "正在压缩会话历史…",
}


def _slow_command_label(user_input: str) -> str:
    """慢命令的等待文案（非慢命令返回空串）。"""
    from src.cli.root_shim import _command_name

    return _SLOW_COMMAND_LABELS.get(_command_name(user_input), "")


def _is_detail_command(user_input: str) -> bool:
    """`/detail [关键词]`：子智能体折叠块回看（端上命令，不透传内核）。"""
    text = str(user_input or "").strip()
    return text == "/detail" or text.startswith("/detail ")


def _detail_keyword(user_input: str) -> str:
    """`/detail` 后面的关键词（无参＝空串→取最近一块）。"""
    return str(user_input or "").strip()[len("/detail") :].strip()


def _render_fold_detail(display: Any, user_input: str) -> None:
    """按类型/任务摘要定位一块折叠块并补打明细；定位不到给一行黄字提示。"""
    from src.cli.scrollback import CliScrollback

    if display is None:
        return
    keyword = _detail_keyword(user_input)
    if display.print_fold_detail(keyword):
        return
    reason = f"没有匹配「{keyword}」的子智能体折叠块" if keyword else "还没有子智能体折叠块可回看"
    CliScrollback.write(f"{reason} —— 输入 /detail 后按 Tab 从候选里挑一个。", style="yellow")


async def handle_attached_chat_command(
    root: Any,
    user_input: str,
    workspace: Path,
    display: Any = None,
    spinner: Any = None,
) -> bool:
    """Dispatch a slash command and render its result（attached 适配版）"""
    from src.cli.commands import _render_result

    if _is_detail_command(user_input):
        _render_fold_detail(display, user_input)
        return False

    label = _slow_command_label(user_input)
    waiting = spinner is not None and bool(label)
    if waiting:
        spinner.begin_local_wait(label)
    try:
        result = await root.execute_command(user_input)
    finally:
        if waiting:
            spinner.end_local_wait()
    if result is None:
        # Not a slash command — shouldn't happen (caller checks startswith "/"),
        # but treat as "not handled" so the caller forwards to LLM.
        return False

    _render_result(result)
    return result.exit_session
