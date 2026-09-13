"""Prompt toolkit completers for the Coara CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.completion import Completer, Completion, merge_completers

# Re-export picker helpers used by session / tests
from src.cli.slash_pickers import (  # noqa: F401
    PICKER_COMMANDS,
    SlashOptionPicker,
    WorkspaceSwitchCompleter,
    build_static_pickers,
    is_slash_picker_submit_line,
    is_ws_switch_submit_line,
    slash_picker_cancel_stem,
)

CHAT_COMMANDS = [
    "/help",
    "/status",
    "/model",
    "/new",
    "/compact",
    "/detail",
    "/report",
    "/ws",
    "/events",
    "/sandbox",
    "/thinking",
    "/tools",
    "/message",
    "/vault",
    "/qrcode",
    "/login",
    "/exit",
]


class SlashCommandCompleter(Completer):
    """Offer slash command completions with meta descriptions and fuzzy matching."""

    _DESCRIPTIONS: dict[str, str] = {
        "/help": "显示可用命令",
        "/status": "显示 Coara 状态",
        "/model": "列出或切换 LLM 模型",
        "/new": "开始新会话",
        "/compact": "手动压缩当前会话历史",
        "/detail": "回看某个子智能体折叠块明细（/detail [关键词]）",
        "/report": "向开发者提交问题报告（附带本轮会话）",
        "/ws": "列出/切换工作空间或查看动态",
        "/events": "查看外部事件源",
        "/sandbox": "切换执行沙箱开关",
        "/thinking": "LLM 思考 / reasoning 模式",
        "/tools": "工具开关（/tools off <名> 停用，/tools on <名> 启用）",
        "/message": "查看系统消息（如 API key 提醒）",
        "/vault": "保险柜状态 / 锁定",
        "/qrcode": "终端显示手机配对二维码",
        "/login": "登录账户（打开 Web 登录页）",
        "/exit": "退出对话",
    }

    @staticmethod
    def should_complete(document) -> bool:
        """Return True if slash completion should be active."""
        text = document.text_before_cursor
        if document.text_after_cursor.strip():
            return False
        return not text.strip() or text.startswith("/")

    def get_completions(self, document, complete_event):
        if not self.should_complete(document):
            return

        text = document.text_before_cursor
        text_l = text.lower()
        for command in CHAT_COMMANDS:
            if command in PICKER_COMMANDS and (text_l == command or text_l.startswith(command + " ")):
                continue
            if command.startswith(text_l):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=self._DESCRIPTIONS.get(command, ""),
                )


class AtServiceDeskCompleter(Completer):
    """``@`` 只补全服务台名（daily / 配置等），不再补文件路径。"""

    def __init__(self, desks: list[dict[str, str]] | None = None) -> None:
        self._desks: list[dict[str, str]] = list(desks or [])

    def set_desks(self, desks: list[dict[str, str]] | None) -> None:
        self._desks = list(desks or [])

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        # 仅行首 @…（服务台投递语法）；正文中间的 @ 不弹补全
        if not text.startswith("@"):
            return
        if " " in text or "\t" in text or "\n" in text:
            return
        partial = text[1:].lower()
        for desk in self._desks:
            name = str(desk.get("name") or "").strip()
            if not name:
                continue
            if partial and not name.lower().startswith(partial):
                continue
            summary = str(desk.get("summary") or "").strip()
            yield Completion(
                text=f"@{name} ",
                start_position=-len(text),
                display=f"@{name}",
                display_meta=summary or "服务台",
            )


CHAT_COMMAND_COMPLETER = SlashCommandCompleter()


def build_chat_completers(workspace: Path) -> tuple[Any, dict[str, Any]]:
    """Build merged completer + named picker handles for Root binding.

    Returns ``(merged_completer, handles)`` where *handles* has keys:
    ``desk``, ``ws``, ``model``, ``detail``.
    """
    del workspace  # 服务台名单来自握手帧，不再按工作空间路径补文件
    desk_completer = AtServiceDeskCompleter()
    ws_completer = WorkspaceSwitchCompleter()
    model_completer = SlashOptionPicker("/model")
    # 候选源在建好 display_controller 之后才存在（bind_detail_picker 注入）。
    detail_completer = SlashOptionPicker("/detail")
    static = build_static_pickers()

    merged = merge_completers(
        [
            CHAT_COMMAND_COMPLETER,
            ws_completer,
            model_completer,
            detail_completer,
            *static,
            desk_completer,
        ]
    )
    handles = {
        "desk": desk_completer,
        "ws": ws_completer,
        "model": model_completer,
        "detail": detail_completer,
    }
    return merged, handles
