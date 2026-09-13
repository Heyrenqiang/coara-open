"""Attached CLI chat session: 原主 CLI 完整交互界面骨架，连内核运行。

内核化「原 CLI 界面召回」的核心：界面骨架（spinner / prompt_session /
display_controller / 主循环）逐字照搬 chat_runner.run_chat_session，只把
「本地持内核」换成 RootShim + RemoteEventBus 经 /ws/attach 连内核。

- root 由调用方建好传入（WsAttachTransport 已连接、attached 快照已应用），
  本模块不碰 bootstrap_runtime / create_root_coara。
- 本地单体职责（Matrix / Web / 录像带 / 前端注入 / 配置加载 / 工作空间布局）
  全部剔除——那些是内核侧前端与内核职责，客户端不该有。
- 客户端退出只断开自己（关 transport / 取消 prompt 任务 / 退订），内核继续常驻。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import yaml
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
    """CLI 主题读取本地配置（``cli.theme`` 是客户端渲染设置，不是内核逻辑）。

    候选：bootstrap coara Home 的 config.yaml、工作空间 ``.coara`` 下的
    config.yaml；只取顶层 ``cli`` 段（``COARA_CLI_THEME`` 环境变量由
    apply_theme_from_config 自己处理）。读不到返回 {}——主题回退 dark，
    不阻塞启动。
    """
    candidates: list[Path] = []
    with contextlib.suppress(Exception):
        from src.core.coara_home import resolve_bootstrap_coara_home, user_dir_for_home

        bootstrap = resolve_bootstrap_coara_home()
        if bootstrap is not None:
            candidates.append(user_dir_for_home(bootstrap) / "config.yaml")
    candidates.append(workspace / ".coara" / "users" / "default" / "config.yaml")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and isinstance(data.get("cli"), dict):
            return {"cli": data["cli"]}
    return {}


async def run_attached_chat_session(root: Any, *, workspace: Path, console: Console) -> None:
    """在已接入内核的 RootShim 上跑完整 CLI 界面（照搬 run_chat_session 骨架）。

    root：RootShim（transport 已连接、attached 快照已 apply）。
    workspace：本地提示/补全用的工作空间路径（cwd）。
    console：Rich console（调用方持有，attach_client 等共享）。
    """
    from src.cli import theme as cli_theme
    from src.cli.remote_event_bus import Subscription

    _subscriptions: list[Subscription] = []

    # 配置在内核侧；CLI 主题 token 是客户端渲染设置，读本地 config.yaml 的
    # cli 段（内核 config 不传给客户端；读不到就跳过，回退默认 dark）。
    with contextlib.suppress(Exception):
        cli_theme.apply_theme_from_config(_read_local_cli_theme_config(workspace))
    alias = root.workspace_manager.active_name if root.workspace_manager else "?"
    console.print(f"[dim]workspace {alias} ({workspace})[/dim]")
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

    # delegate 自己的 ✓/✗ 行由折叠块的摘要行取代（一次 delegate 只留一行）。
    # 应用点：流式 chunk 与重放帧两条上屏路径，整条会话共用一个实例
    # （只暂扣「看起来是工具行」的半行，回合收尾 flush）。
    from src.cli.subagent_fold import DelegateLineFilter

    delegate_lines = DelegateLineFilter()

    def _visible_chunk(text: str) -> str:
        """过滤后的可见正文（空串＝整段被折叠块吸收，不该上屏）。"""
        visible = delegate_lines.feed(text)
        return visible

    def _on_replay_frame(frame: dict) -> None:
        """服务端重放帧（断连重连后 buffer 重发）：本地流已终结时直接渲染。

        按帧类型分流：chunk/tool 行进 streaming block；turn_end 只收尾
        （不重开流）；diff 帧走 display 暂存。seq 去重由 root_shim 保证。
        """
        frame_type = str(frame.get("type") or "")
        if frame_type in ("chunk", "chat_chunk"):
            text = _visible_chunk(str(frame.get("text") or frame.get("chunk") or ""))
            if text.strip():
                background_spinner.ensure_streaming()
                background_spinner.append_streaming_chunk(text)
        elif frame_type == "tool":
            text = _visible_chunk(str(frame.get("text") or ""))
            if text.strip():
                background_spinner.ensure_streaming()
                background_spinner.append_streaming_chunk(text)
        elif frame_type == "diff":
            display.queue_diff_frame(frame)

    root._replay_callback = _on_replay_frame

    # 在飞审批任务的登记表：approval_resolved（服务端超时/取消/断连收口）到达时
    # 按 approval_id 取消对应任务——modal 随 prompt_select 的 wait_for 取消而 detach，
    # 不再永挂等满 300s。
    _approval_tasks: dict[str, asyncio.Task] = {}

    def _on_approval_frame(frame: dict) -> None:
        """attach 审批帧：弹 CLI modal 让用户决定，答复回传服务端 approval_reply。

        回调在事件循环上下文被调（root_shim._schedule），prompt_select 是协程，
        包成任务跑。用户取消/超时一律按不同意回执——服务端超时兜底同语义，
        绝不静默放行。"""

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
    # 不挂 wire_remote_sync_display：那是老 CLI 作为主前端「镜像其它端对话」的逻辑。
    # 召回后 CLI 是平等 attach 客户端，各端完全独立零同步——web/手机端的对话
    # 绝不镜像到 CLI。CLI 自己的对话经 process_message 流 + display.wire 渲染。

    # 就绪行：fg._tool_manager/_skills 是内核内部结构，客户端读 attached 快照
    # 镜像的计数（RootShim identity.tools_count/skills_count）。
    console.print(
        f"[green]就绪[/green] [bold]{root.identity.name}[/bold] · "
        f"Tools {root.identity.tools_count} · Skills {root.identity.skills_count}"
    )
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
                root=root,
                on_busy_slash=_on_busy_slash,
            )
        )

    def _interrupt_current_turn_nowait() -> bool:
        """同步打断入口（_prompt_loop / SIGINT 是同步上下文）：RootShim 的
        interrupt_current_turn 是协程，调度为 fire-and-forget 任务；
        无运行循环时调度失败按未打断处理（回合本就无法进行）。

        三态语义（对齐最早 CLI 设计）：
        - 输入区有内容 → 键绑定层已清空输入并消费按键，不会走到这里
        - 回合运行中（spinner 在转）→ 打断所有在跑的，返回 True 继续循环
        - 空闲 → 返回 False，_prompt_loop break → _PromptExit → 退出 CLI
        """
        if not root.has_active_turn():
            return False
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                root.foreground_coara.interrupt_current_turn(
                    "user_ctrl_c",
                    interrupt_source="cli_prompt_ctrl_c",
                )
            )
            return True
        except RuntimeError:
            return False

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
    # Windows: closing the console window / taskkill does not raise SIGINT —
    # do a minimal synchronous flush from the OS handler thread (~5s budget).
    # 客户端替身各清理步都是安全 no-op（会话落盘在内核）。
    _uninstall_console_close = install_windows_console_close_handler(make_minimal_sync_cleanup(root))

    try:
        from src.cli.burst_input import ChatTurnInput, annotate_burst_message

        with install_sigterm_handler(_on_sigterm), install_chat_session_sigint_handler(root):
            # Detached-turn re-attach display state: a turn that lost its CLI
            # consumer (mid-turn workspace switch away) re-opens a streaming
            # block when the user switches back, so its tool lines and text
            # keep reaching the scrollback instead of being drained silently.
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
                                input_queue.put_nowait(ChatTurnInput(text=_msg.text, image_blocks=_msg.image_blocks))
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
                        # Ctrl+C pressed while a turn was running. The actual interrupt was
                        # already applied by on_interrupt() inside _prompt_loop; just consume
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

                    from src.cli.image_paste import (
                        attach_images_from_message_text,
                        await_image_loading,
                        pending_names,
                        strip_markers,
                        take_pending_images,
                    )

                    await await_image_loading()
                    from src.cli.image_paste import sync_cancel_if_marker_missing

                    # 发送前快照当前图片名（take_pending_images 会清空），供精确剔除占位符。
                    names_now = pending_names()
                    # 实时回删：输入栏占位符被删/破坏（如 `[图片2]` 删成残缺）→ 对应图
                    # 从 pending 裁掉，避免发送时把已撤回的图带上。
                    if isinstance(turn_text, str):
                        sync_cancel_if_marker_missing(turn_text)
                    pending_images = take_pending_images()
                    if pending_images:
                        turn_images = [*turn_images, *pending_images]

                    path_blocks: list = []
                    paths_found: list = []
                    if isinstance(turn_text, str) and turn_text.strip():
                        turn_text, path_blocks, paths_found = attach_images_from_message_text(turn_text)
                        if path_blocks:
                            turn_images = [*turn_images, *path_blocks]

                    if turn_images:
                        try:
                            _pname = str(getattr(root.foreground_coara, "provider_name", "") or "")
                            _mname = str(getattr(root.foreground_coara, "model_name", "") or "")
                        except Exception:
                            _pname, _mname = "", ""
                        from src.llm.vision import model_vision_explicit

                        # 视觉门控（客户端侧）：只认显式证据。客户端不持内核 config_manager，
                        # 白名单又只认 *vision* 通用 marker——按白名单判"不支持"会把配置里
                        # 明确 vision: true 的型号（deepseek-flash / k3 / MiniMax-M3）误拦，
                        # 刚贴的图片被本地吃掉。拿不到证据（None）一律放行，交给内核按配置拦
                        # （后端转换层对不支持的模型换成占位文案，不会 400）。
                        if model_vision_explicit(_mname, provider_name=_pname) is not False:
                            console.print(
                                f"[dim]已附加 {len(turn_images)} 张图片 → Vision API（{_pname}/{_mname}）[/dim]"
                            )
                        else:
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

                    turn_text = strip_markers(turn_text, names_now).strip()

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

                        # Slash handlers print via Rich while prompt_async is already
                        # waiting again; without patch_stdout the idle input can look dead
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
                            async for chunk in _collect_chat_turn_streaming(
                                root,
                                turn_text,
                                interrupt_state,
                                image_blocks=turn_images or None,
                                on_detached_chunk=_on_detached_chunk,
                                on_detached_drain_complete=_close_detached_stream,
                            ):
                                visible = _visible_chunk(chunk)
                                if not visible:
                                    # delegate 自己的 ✓ 行：摘要行取代它，不上屏
                                    # （内容已进折叠块）。
                                    continue
                                chunks.append(visible)
                                background_spinner.append_streaming_chunk(visible)
                                if visible.lstrip().startswith("✓"):
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
            # Interactive CLI exit must not return into asyncio.run teardown.
            # Cancelled prompt tasks can sit in sync joins; hard-exit after the
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
    """Dispatch a slash command and render its result（attached 适配版）。

    照搬 commands.handle_chat_command 结构，命令执行换为透传内核
    ``RootShim.execute_command``（服务端命令层 target_coara=pin 空间）。

    ``spinner``：慢命令（/compact）在等待期把 spinner 转到「正在压缩…」——
    内核压缩期间不回帧，不转就是屏幕静默。

    ``/detail`` 是**端上命令**：折叠块数据只在本 CLI 进程里，不走内核透传
    （先于 RPC 拦截）。

    Returns True if the session should exit.
    """
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
