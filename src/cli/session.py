"""Chat session helpers for the coara CLI."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import SimpleLexer

from src.cli.completers import (
    build_chat_completers,
    is_slash_picker_submit_line,
    slash_picker_cancel_stem,
)
from src.cli.theme import get_prompt_style
from src.coara.turn_completion import CoaraRunCancelledError
from src.core.logger import logger
from src.llm.vision import model_vision_explicit
from src.matrix_client.remote_vision import MAX_IMAGE_BATCH_COUNT


def input_placeholder(spinner: Any | None = None) -> Any:
    """空输入时跟在「你：」后面的淡字。一打字就消失，不写进发送内容。"""

    def _text() -> FormattedText | str:
        if spinner is not None and getattr(spinner, "_active_modal_delegate", lambda: None)() is not None:
            return ""
        return FormattedText([("class:input.placeholder", "消息，或 / 看命令")])

    return _text


def apply_prompt_session_input_tweaks(session: PromptSession[str]) -> None:
    """Reduce input lag for CJK IME / busy prompt redraws"""
    app = session.app
    # Cap redraw storms when spinner invalidate() races with keystrokes / IME.
    if getattr(app, "min_redraw_interval", None) in (None, 0):
        app.min_redraw_interval = 0.05
    if sys.platform != "win32":
        return
    # 尽量打开 VT 处理：差分重绘只改变动单元格，减轻整页闪（Win32Output 更重）
    with contextlib.suppress(Exception):
        from ctypes import byref, windll
        from ctypes.wintypes import DWORD

        from prompt_toolkit.win32_types import STD_OUTPUT_HANDLE

        h = windll.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = DWORD(0)
        if windll.kernel32.GetConsoleMode(h, byref(mode)):
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # noqa: N806 - Win32 API 常量惯例大写
            windll.kernel32.SetConsoleMode(h, DWORD(mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    # and can delay or swallow the normal redraw path.
    reader = getattr(app.input, "console_input_reader", None)
    if reader is not None and hasattr(reader, "recognize_paste"):
        reader.recognize_paste = False


@dataclass(slots=True)
class ChatTurnInterruptState:
    """Track per-turn CLI interrupt handling."""

    interrupt_requested: bool = False


# Set while ``_install_turn_sigint_handler`` is active (CLI local turn).
# Session-level SIGINT uses this when Rich/prompt_toolkit overwrote the per-turn handler.
_cli_turn_interrupt_state: ContextVar[ChatTurnInterruptState | None] = ContextVar(
    "cli_turn_interrupt_state",
    default=None,
)


def _should_complete_while_typing(text: str) -> bool:
    """Auto-complete only for slash commands / @mentions"""
    return text.startswith(("/", "@"))


@Condition
def _complete_on_command_prefix() -> bool:
    from prompt_toolkit.application.current import get_app_or_none

    app = get_app_or_none()
    if app is None:
        return False
    return _should_complete_while_typing(app.current_buffer.text)


def _chat_input_modal_inactive(spinner) -> bool:
    return spinner is None or spinner._active_modal_delegate() is None


def _set_erase_if_turn_active(event, *, spinner) -> None:
    """Avoid flushing the dynamic prompt to scrollback when queuing during a turn."""
    if spinner is not None and spinner._root is not None and spinner._root.foreground_coara.has_active_turn():
        event.app.erase_when_done = True


def _clear_prompt_input_on_ctrl_c(buffer: Any) -> bool:
    """Clear non-empty prompt input on Ctrl+C. Returns True if the key was consumed."""
    text = getattr(buffer, "text", "") or ""
    if not text:
        return False
    reset = getattr(buffer, "reset", None)
    if callable(reset):
        reset()
    else:
        buffer.text = ""
    return True


def _esc_queue_pop_available(spinner) -> bool:
    """Esc 取消注入仅在：无补全菜单、无 modal、回合运行中且队列有用户跟话时激活。"""
    if spinner is None or not _chat_input_modal_inactive(spinner):
        return False
    from prompt_toolkit.application.current import get_app_or_none

    app = get_app_or_none()
    if app is not None and app.current_buffer.complete_state is not None:
        return False
    from src.cli.input_queue_display import has_popable_queued_followup

    return has_popable_queued_followup(getattr(spinner, "_root", None))


def _build_chat_prompt_session(
    workspace: Path,
    *,
    message_factory=None,
    bottom_toolbar=None,
    spinner=None,
    pt_input=None,
    pt_output=None,
) -> PromptSession[str]:
    bindings = KeyBindings()

    @bindings.add(
        "enter",
        filter=Condition(lambda: _chat_input_modal_inactive(spinner)),
        eager=True,
    )
    def _submit_chat_input(event) -> None:
        """Submit input; eager handler must call validate_and_handle (default Enter is bypassed).

        Slash pickers (`/ws`, `/model`): applying a completion also submits so one Enter switches.
        """
        _set_erase_if_turn_active(event, spinner=spinner)
        buffer = event.current_buffer
        if buffer.complete_state and buffer.complete_state.current_completion:
            buffer.apply_completion(buffer.complete_state.current_completion)
            if is_slash_picker_submit_line(buffer.text or ""):
                buffer.validate_and_handle()
            return
        buffer.validate_and_handle()

    @bindings.add("escape", filter=has_completions, eager=True)
    def _abort_completion(event) -> None:
        """Cancel ↑↓ picker: restore pre-menu text (usually bare `/ws` or `/model`)."""
        buffer = event.current_buffer
        state = buffer.complete_state
        original = None
        if state is not None:
            doc = getattr(state, "original_document", None)
            if doc is not None:
                original = doc.text
        buffer.complete_state = None
        if original is not None:
            buffer.text = original
            buffer.cursor_position = len(original)
            return
        stem = slash_picker_cancel_stem(buffer.text or "")
        if stem is not None:
            buffer.text = stem
            buffer.cursor_position = len(stem)

    # Ctrl+Enter / Ctrl+J: conhost 把 Ctrl+Enter 映射为 c-j (\n)，这是换行的主通道。
    @bindings.add("c-j", eager=True)
    def _insert_newline_ctrl_j(event) -> None:
        """Ctrl+Enter / Ctrl+J inserts a newline (conhost maps Ctrl+Enter to c-j)."""
        event.current_buffer.insert_text("\n")

    # Shift+Enter：支持 kitty CSI-u 的终端走下列序列换行；其余终端按 Enter 处理。
    # 绑定拆成 escape 起头的多键序列（单键只能是单字符）。
    @bindings.add("escape", "[", "1", "3", ";", "2", "u", eager=True)
    def _insert_newline_shift_enter(event) -> None:
        """Shift+Enter inserts a newline (terminals reporting kitty CSI-u)."""
        event.current_buffer.insert_text("\n")

    @bindings.add(
        "escape",
        filter=Condition(lambda: _esc_queue_pop_available(spinner)),
        eager=True,
    )
    def _cancel_queued_continuation(event) -> None:
        """Esc：取消注入一条排队中的接续输入（队尾最新一条用户跟话），显示随之消失"""
        from src.cli.input_queue_display import pop_latest_queued_followup

        removed = pop_latest_queued_followup(getattr(spinner, "_root", None))
        if removed is None:
            return
        if not removed.text and not removed.client_msg_id:
            return
        _root = getattr(spinner, "_root", None)
        fg = getattr(_root, "foreground_coara", None)
        cancel_rpc = getattr(fg, "cancel_queued_continuation", None)
        if callable(cancel_rpc):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(cancel_rpc(removed.text, client_msg_id=removed.client_msg_id))
            except RuntimeError:
                pass

    @bindings.add("c-v", eager=True)
    def _paste_from_clipboard(event) -> None:
        """Ctrl+V：资源管理器文件→绝对路径；长文本→落盘后贴路径；否则贴剪贴板文本。"""

        async def _do_paste() -> None:
            from src.utils.clipboard_image import get_clipboard_text
            from src.utils.clipboard_paths import (
                format_paths_for_input,
                get_clipboard_file_paths,
                is_long_paste_text,
                save_pasted_long_text,
            )

            try:
                paths = await get_clipboard_file_paths()
            except Exception:
                paths = []
            if paths:
                event.current_buffer.insert_text(format_paths_for_input(paths))
                event.app.invalidate()
                return
            try:
                text = await get_clipboard_text()
            except Exception:
                text = None
            if text and is_long_paste_text(text):
                ws_dir = None
                root = getattr(spinner, "_root", None) if spinner is not None else None
                fg = getattr(root, "foreground_coara", None) if root is not None else None
                raw_ws = getattr(fg, "workspace_dir", None) if fg is not None else None
                if raw_ws:
                    from pathlib import Path

                    from src.core.workspace_layout import UPLOAD_DIR_NAME

                    ws_dir = Path(raw_ws) / UPLOAD_DIR_NAME
                try:
                    saved = await asyncio.to_thread(save_pasted_long_text, text, directory=ws_dir)
                except Exception:
                    saved = None
                if saved is not None:
                    event.current_buffer.insert_text(format_paths_for_input([str(saved)]))
                    event.app.invalidate()
                    return
            if text:
                event.current_buffer.insert_text(text)
                event.app.invalidate()
                return
            data = event.app.clipboard.get_data()
            if data is not None and getattr(data, "text", None):
                event.current_buffer.paste_clipboard_data(data)
                event.app.invalidate()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_do_paste())

    @bindings.add("escape", "v", eager=True)
    def _paste_image_from_clipboard(event) -> None:
        """Alt+V: paste clipboard image as Vision attachment (not plain text/path)."""

        async def _do_paste() -> None:
            from src.cli.image_paste import (
                ack_latest_marker,
                begin_paste,
                composer_epoch,
                discard_unsent_images,
                finish_paste,
                is_submitting,
                paste_marker,
                try_queue_clipboard_image,
            )

            def _hint(text: str) -> None:
                # 局部建 console：不依赖调用方持有的 console（召回后 session 会被 attached_chat_runner 复用）。
                from rich.console import Console

                Console().print(text)
                event.app.invalidate()

            epoch = begin_paste()
            queue_task = asyncio.create_task(try_queue_clipboard_image(epoch))
            try:
                try:
                    ok = await asyncio.wait_for(asyncio.shield(queue_task), timeout=15.0)
                except TimeoutError:
                    discard_unsent_images()
                    _hint("[yellow]读取剪贴板图片超时，请重试[/yellow]")
                    return
                if composer_epoch() != epoch:
                    return
                # 回车已经在等这次贴图：图跟那次发送走，不再往下一次输入框插占位符。
                if ok and not is_submitting():
                    event.current_buffer.insert_text(paste_marker())
                    ack_latest_marker()
                    return
                if not ok:
                    _hint(
                        "[yellow]剪贴板没有检测到图片[/yellow]\n"
                        "[dim]Alt+V 粘贴剪贴板位图；或直接粘贴图片文件路径（如微信复制）[/dim]"
                    )
            except Exception as exc:
                _hint(f"[yellow]读取剪贴板图片失败：{exc}[/yellow]")
            finally:
                finish_paste()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_do_paste())

    @bindings.add("c-u", eager=True)
    def _clear_input_buffer(event) -> None:
        """Ctrl+U: clear the entire input line and drop images not yet sent."""
        event.current_buffer.reset()
        from src.cli.image_paste import discard_unsent_images

        discard_unsent_images()

    # Ctrl+O：在输入区上方展开/收起子智能体折叠块（可重绘，不往滚动区刷提醒）。
    # 无折叠块/无绑定对象时无动作——不影响输入，也不吞按键；模态框打开时让位。
    @bindings.add("c-o", filter=Condition(lambda: _chat_input_modal_inactive(spinner)), eager=True)
    def _toggle_subagent_fold(event) -> None:
        """Ctrl+O: toggle subagent fold overlay above the prompt (no scrollback spam)."""
        if spinner is None:
            return
        toggle = getattr(spinner, "toggle_latest_fold", None)
        if callable(toggle):
            toggle()
        event.app.invalidate()

    _arrow_cursor_only = Condition(lambda: _chat_input_modal_inactive(spinner)) & ~has_completions

    @bindings.add("up", filter=_arrow_cursor_only, eager=True)
    def _cursor_up(event) -> None:
        """↑ 仅移动光标（不翻 prompt 历史；补全菜单打开时仍由 picker 接管 ↑↓）。"""
        event.current_buffer.cursor_up(count=event.arg)

    @bindings.add("down", filter=_arrow_cursor_only, eager=True)
    def _cursor_down(event) -> None:
        """↓ 仅移动光标。"""
        event.current_buffer.cursor_down(count=event.arg)

    @bindings.add("left", filter=_arrow_cursor_only, eager=True)
    def _cursor_left(event) -> None:
        event.current_buffer.cursor_left(count=event.arg)

    @bindings.add("right", filter=_arrow_cursor_only, eager=True)
    def _cursor_right(event) -> None:
        event.current_buffer.cursor_right(count=event.arg)

    @bindings.add("c-c", eager=True)
    def _handle_ctrl_c(event) -> None:
        """Ctrl+C while prompt is active"""
        if spinner is not None and spinner._active_modal_delegate() is not None:
            spinner._handle_modal_key("c-c", event)
            return
        if _clear_prompt_input_on_ctrl_c(event.current_buffer):
            return
        event.app.exit(exception=KeyboardInterrupt)

    # Modal delegate key bindings — intercept keys when a modal is active.
    if spinner is not None:
        for _key in ("up", "down", "left", "right", "tab", "space", "enter", "escape", "c-c"):
            bindings.add(
                _key,
                eager=True,
                filter=Condition(lambda k=_key: spinner._should_handle_modal_key(k)),
            )(lambda event, k=_key: spinner._handle_modal_key(k, event))
        for _num in range(1, 10):
            bindings.add(
                str(_num),
                eager=True,
                filter=Condition(lambda k=str(_num): spinner._should_handle_modal_key(k)),
            )(lambda event, k=str(_num): spinner._handle_modal_key(k, event))

    completer, handles = build_chat_completers(workspace)
    kwargs: dict[str, Any] = {
        "completer": completer,
        # prompt redraw on every composition update).
        "complete_while_typing": _complete_on_command_prefix,
        # 输入框正文统一走主题里 ``input`` 类（user_content 色）：用户新回合第一条
        # 输入的正文与后续接续/排队回显保持同一颜色，不再默认白。
        "lexer": SimpleLexer(style="class:input"),
        "placeholder": input_placeholder(spinner),
        "history": DummyHistory(),
        "key_bindings": bindings,
        "style": get_prompt_style(),
    }
    if message_factory is not None:
        kwargs["message"] = message_factory
    if bottom_toolbar is not None:
        kwargs["bottom_toolbar"] = bottom_toolbar
    if pt_input is not None:
        kwargs["input"] = pt_input
    if pt_output is not None:
        kwargs["output"] = pt_output

    session: PromptSession[str] = PromptSession(**kwargs)
    apply_prompt_session_input_tweaks(session)
    # Used by CliDisplayController after workspace_switched / picker binding.
    session.coara_desk_completer = handles["desk"]  # type: ignore[attr-defined]
    session.coara_ws_completer = handles["ws"]  # type: ignore[attr-defined]
    session.coara_model_completer = handles["model"]  # type: ignore[attr-defined]
    session.coara_detail_completer = handles["detail"]  # type: ignore[attr-defined]
    return session


class _PromptExit:
    """Sentinel placed on the input queue when the prompt loop exits."""


class _PromptCtrlC:
    """Sentinel placed on the input queue when the user presses Ctrl+C while prompt is active."""


def _print_cli_hint(markup: str) -> None:
    from rich.console import Console

    Console().print(markup)


def _prepared_turn(text: str, images: list):
    from src.cli.burst_input import ChatTurnInput

    return ChatTurnInput(text=text, image_blocks=list(images), images_resolved=True)


def _gate_cli_images(root: Any, images: list) -> list:
    """视觉门控（三端同尺）：显式声明不支持才丢图提示；超上限部分丢弃提示"""
    if not images or root is None:
        return images
    if len(images) > MAX_IMAGE_BATCH_COUNT:
        dropped = len(images) - MAX_IMAGE_BATCH_COUNT
        images = images[:MAX_IMAGE_BATCH_COUNT]
        _print_cli_hint(f"[yellow]一次最多发送 {MAX_IMAGE_BATCH_COUNT} 张图片，超出的 {dropped} 张已忽略。[/yellow]")
    try:
        model_name = str(getattr(root.foreground_coara, "model_name", "") or "")
        provider_name = str(getattr(root.foreground_coara, "provider_name", "") or "")
    except Exception:
        return images
    from src.core.config import config_manager

    if model_vision_explicit(model_name, provider_name=provider_name, config_manager=config_manager) is False:
        _print_cli_hint(
            f"[yellow]当前模型 {model_name or '未知'} 不支持图像输入，图片已省略。[/yellow]\n"
            "[dim]可用 /model 切换到支持图像的模型后再发送图片。[/dim]"
        )
        return []
    return images


async def _bind_composer_message(user_input: str) -> tuple[str, list] | None:
    """等在飞贴图结束，把图绑到这次回车。没有正文也没有图时返回 None。"""
    from src.cli.image_paste import resolve_composer_attachments

    text, images, paths_found = await resolve_composer_attachments(user_input)
    if not images and paths_found:
        _print_cli_hint(
            "[yellow]检测到图片路径，但文件不存在或无法读取。[/yellow]\n"
            "[dim]微信临时目录可能已清理。请重新复制图片后按 Alt+V，"
            "或先把图片保存到工作区再发送。[/dim]"
        )
        return None
    if not str(text or "").strip() and not images:
        return None
    return text, images


async def _prompt_loop(
    session: PromptSession[str],
    queue: asyncio.Queue[Any],
    *,
    on_interrupt: Any = None,
    root: Any = None,
    on_busy_slash: Any = None,
    on_new_session: Any = None,
) -> None:
    """Keep prompt alive in background so input area stays stable during agent runs."""
    from prompt_toolkit.patch_stdout import patch_stdout

    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    raw = await session.prompt_async()
                    user_input = (raw or "").strip()

                    # in scrollback exactly once when injected
                    if (
                        root is not None
                        and root.foreground_coara.has_active_turn()
                        and on_busy_slash is not None
                        and user_input.startswith("/")
                    ):
                        from src.coara.commands.registry import is_run_while_busy_command

                        if is_run_while_busy_command(user_input):
                            await on_busy_slash(user_input)
                            continue
                    if not user_input.startswith("/"):
                        bound = await _bind_composer_message(user_input)
                        if bound is None:
                            continue
                        text, images = bound
                        if root is not None and root.foreground_coara.has_active_turn():
                            had_images = bool(images)
                            images = _gate_cli_images(root, images)
                            if not str(text or "").strip() and not images:
                                if had_images:
                                    _print_cli_hint(
                                        "[yellow]跟话未入队：当前模型不支持图片，且没有文字。[/yellow]\n"
                                        "[dim]切换支持图像的模型，或带上文字说明再发。[/dim]"
                                    )
                                continue
                            submit = root.foreground_coara.submit_continuation_input(
                                text,
                                image_blocks=images or None,
                                source="cli-attached",
                                client_msg_id=uuid.uuid4().hex,
                            )
                            # attached 模式 fg 是 RootShim（协程）；内嵌 CoaraBase 是同步。
                            import inspect as _inspect

                            if _inspect.isawaitable(submit):
                                await submit
                            continue
                        had_images = bool(images)
                        images = _gate_cli_images(root, images) if root is not None else images
                        if images:
                            await queue.put(_prepared_turn(text, images))
                            continue
                        if not str(text or "").strip():
                            if had_images:
                                _print_cli_hint(
                                    "[yellow]消息未发送：当前模型不支持图片，且没有文字。[/yellow]\n"
                                    "[dim]切换支持图像的模型，或带上文字说明再发。[/dim]"
                                )
                            continue
                        user_input = text
            except KeyboardInterrupt:
                interrupted = False
                if on_interrupt is not None:
                    # Interrupt callback may fail; continue with default behavior.
                    with contextlib.suppress(Exception):
                        interrupted = on_interrupt()
                if interrupted:
                    await queue.put(_PromptCtrlC())
                    continue
                break
            except EOFError:
                break

            await queue.put(user_input)
            if user_input.strip().lower() == "/new" and on_new_session is not None:
                # `/new` 只停进行中的回合：后台任务（bash / aide）是独立长活，
                # 换会话不该连带取消（旧实现复用 on_interrupt，全给杀了）
                with contextlib.suppress(Exception):
                    on_new_session()
    except Exception:
        # Catch-all to ensure _PromptExit is sent even for unexpected errors.
        logger.exception("Unexpected error in CLI prompt loop")
    finally:
        # Notify the main loop that the prompt has exited.
        await queue.put(_PromptExit())


async def _collect_chat_turn_streaming(
    root,
    user_input: str,
    state: ChatTurnInterruptState,
    *,
    image_blocks: list | None = None,
    on_detached_chunk=None,
    on_detached_drain_complete=None,
):
    """Stream one chat turn into the CLI display path"""
    try:
        with _install_turn_sigint_handler(root, state):
            from src.coara.turn_context import turn
            from src.coara.turn_detach import foreground_workspace_matcher, iter_while_foreground

            turn_coara = root.foreground_coara
            turn_ws_id = getattr(root, "cli_view_workspace_id", None) or root._foreground_session_id
            async with turn("cli-attached"):
                agen = turn_coara.process_message(user_input, image_blocks=image_blocks, source="cli-attached")
                async for chunk in iter_while_foreground(
                    agen,
                    foreground_workspace_matcher(root, turn_ws_id, end="cli"),
                    drain_name="cli-detached-workspace-turn",
                    on_detached_item=on_detached_chunk,
                    on_drain_complete=on_detached_drain_complete,
                ):
                    yield chunk
    except KeyboardInterrupt:
        if not state.interrupt_requested:
            root.foreground_coara.interrupt_current_turn(
                "user_ctrl_c",
                interrupt_source="cli_keyboard_interrupt_turn",
            )
            state.interrupt_requested = True
        raise
    except CoaraRunCancelledError:
        state.interrupt_requested = True


def _has_running_work(root) -> bool:
    """Ctrl+C 三态的判定源（与键盘路径同一把尺）：活跃回合或后台任务任一在跑"""
    has_work = getattr(root, "has_running_work", None)
    if callable(has_work):
        return bool(has_work())
    return bool(root.foreground_coara.has_active_turn())


def _resolve_running_sigint_action(root, state: ChatTurnInterruptState) -> str:
    """Resolve how CLI should react to Ctrl+C while a turn may be running."""
    if not _has_running_work(root):
        return "propagate"
    if state.interrupt_requested:
        return "pending"
    if root.foreground_coara.interrupt_current_turn("user_ctrl_c", interrupt_source="cli_sigint_turn"):
        state.interrupt_requested = True
        return "interrupt"
    return "propagate"


def _propagate_sigint(previous_handler, signum: int, frame) -> None:
    if previous_handler == signal.SIG_IGN:
        return
    if callable(previous_handler):
        previous_handler(signum, frame)
        return
    raise KeyboardInterrupt


def _handle_cli_sigint(
    root,
    state: ChatTurnInterruptState | None,
    previous_handler,
    *,
    signum: int,
    frame,
) -> bool:
    """Apply CLI interrupt policy. Returns True if SIGINT was fully handled."""
    if state is not None:
        action = _resolve_running_sigint_action(root, state)
        if action != "propagate":
            return True
        _propagate_sigint(previous_handler, signum, frame)
        return True
    if _has_running_work(root):
        root.foreground_coara.interrupt_current_turn("user_ctrl_c", interrupt_source="cli_sigint_session")
        return True
    return False


@contextlib.contextmanager
def _install_turn_sigint_handler(root, state: ChatTurnInterruptState):
    token = _cli_turn_interrupt_state.set(state)
    previous_handler = signal.getsignal(signal.SIGINT)

    def handle_sigint(signum: int, frame) -> None:
        if _handle_cli_sigint(root, state, previous_handler, signum=signum, frame=frame):
            return
        _propagate_sigint(previous_handler, signum, frame)

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        _cli_turn_interrupt_state.reset(token)


@contextlib.contextmanager
def install_chat_session_sigint_handler(root) -> Any:
    """While the CLI waits for input, Ctrl+C interrupts an active turn instead of exiting"""
    previous_handler = signal.getsignal(signal.SIGINT)

    def handle_sigint(signum: int, frame) -> None:
        state = _cli_turn_interrupt_state.get()
        if _handle_cli_sigint(root, state, previous_handler, signum=signum, frame=frame):
            return
        _propagate_sigint(previous_handler, signum, frame)

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)


async def await_chat_turn_input(
    queue: asyncio.Queue[Any],
    *,
    root,
    on_recreate_prompt: Any,
    burst_state: Any = None,
) -> Any:
    """Await the next CLI input; map stray Ctrl+C during background turns to interrupt."""
    from src.cli.burst_input import next_chat_turn_input

    try:
        if burst_state is not None:
            return await burst_state.next_chat_turn_input(queue)
        return await next_chat_turn_input(queue)
    except KeyboardInterrupt:
        if root.foreground_coara.has_active_turn():
            root.foreground_coara.interrupt_current_turn(
                "user_ctrl_c", interrupt_source="cli_keyboard_interrupt_await_input"
            )
            on_recreate_prompt()
            return _PromptCtrlC()
        raise
