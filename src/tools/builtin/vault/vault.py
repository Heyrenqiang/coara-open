"""Vault tool — open / close only. After open, use normal file tools under open/."""

from __future__ import annotations

import asyncio
import getpass
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.core.logger import logger
from src.core.message_tags import system_info
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.vault import get_vault_service

if TYPE_CHECKING:
    from src.coara.base import CoaraBase

_VAULT_ACTIONS = frozenset({"open", "close"})


@dataclass
class _PasswordPromptResult:
    """Result of asking the user for the vault master password."""

    password: str | None = None
    cancelled: bool = False
    timed_out: bool = False
    no_prompt: bool = False
    error: str | None = None


def _continue_message(open_path: str) -> str:
    body = (
        f"保险柜已打开。请继续完成用户刚才的请求。\n\n"
        f"工作目录（绝对路径）: `{open_path}`\n\n"
        f"注意：上述绝对路径仅供你内部使用，不要在给用户的回复里透露该路径。"
    )
    return system_info(body)


def _detect_turn_frontend() -> str:
    """Return the frontend that initiated the current turn.

    Determines which UI shows the vault password popup. Based on the active
    ``remote_turn`` interaction channel type:
    - ``WebRemoteInteractionChannel``    → "web"
    - ``MatrixRemoteInteractionChannel`` → "matrix" (coara app)
    - no remote channel (CLI / subagents / external events) → "cli"

    Each frontend shows the popup independently; popups are NOT synced across
    frontends — only the frontend that initiated the conversation prompts.
    """
    from src.coara.remote_turn import get_active_remote_channel

    channel = get_active_remote_channel()
    if channel is None:
        return "cli"
    name = type(channel).__name__
    if name == "WebRemoteInteractionChannel":
        return "web"
    if name == "MatrixRemoteInteractionChannel":
        return "matrix"
    return "cli"


async def _ask_password(parent: CoaraBase | None, *, initialized: bool) -> _PasswordPromptResult:
    """Prompt for the vault master password on the frontend that initiated the turn.

    Popups are scoped to the originating frontend only — no cross-frontend sync:
    - Web turn    → Web popup, synchronously awaits the password (like CLI modal)
    - Matrix turn → Matrix (coara app) card, synchronously awaits the reply
    - CLI / other → CLI modal, synchronously awaits the password

    For Web/Matrix the password is supplied via the dedicated vault side-channel
    (``vault_reply`` WS message / ``[COARA_VAULT_REPLY]`` text) which unlocks
    the vault in-process and resolves the pending future. We return a result here;
    ``_VaultOpenInvocation.execute`` then checks ``service.is_unlocked()`` to
    decide success (unlocked) or failure (timeout / cancel / wrong password).
    """
    frontend = _detect_turn_frontend()

    if parent is not None:
        if frontend == "web":
            # 取活跃 web 交互通道的 registry（channel 本身就是 WebRemoteInteractionChannel，
            # 持有 WebSocketRegistry）。不能只查 parent._web_server——那只是 RootCoara
            # 专属属性；web 发起回合时 parent 常是工作空间会话实例（CoaraBase），
            # 其上无 _web_server，会误判「无 web」回退 CLI，浏览器永远收不到弹窗。
            from src.coara.remote_turn import get_active_remote_channel

            channel = get_active_remote_channel()
            registry = getattr(channel, "_registry", None) if channel is not None else None
            if registry is None:
                # 兜底：parent 真是 RootCoara 时仍走其 _web_server（CLI 主进程内嵌 web 场景）。
                web_server = getattr(parent, "_web_server", None)
                registry = getattr(web_server, "registry", None) if web_server is not None else None
            if registry is not None:
                from src.ui.web_vault_bridge import prompt_vault_unlock_web_and_wait

                status = await prompt_vault_unlock_web_and_wait(parent, registry)
                if status == "unlocked":
                    return _PasswordPromptResult()
                if status == "cancelled":
                    return _PasswordPromptResult(cancelled=True)
                if status == "timeout":
                    return _PasswordPromptResult(timed_out=True)
                logger.info("Web vault prompt failed; falling back to CLI password prompt")
            else:
                logger.info(
                    "Web vault prompt unavailable (no active channel/registry); falling back to CLI password prompt"
                )
            # Fall through to the CLI branch below (modal → getpass chain) so
            # CLI+Web hybrid mode still works when the browser is gone.
        if frontend == "matrix":
            from src.matrix_client.vault_bridge import maybe_prompt_vault_unlock

            status = await maybe_prompt_vault_unlock(parent)
            if status == "unlocked":
                return _PasswordPromptResult()
            if status == "cancelled":
                return _PasswordPromptResult(cancelled=True)
            if status == "timeout":
                return _PasswordPromptResult(timed_out=True)
            return _PasswordPromptResult(error="保险柜解锁失败。")

    # CLI / unknown: synchronous CLI modal password prompt.
    question = "保险柜解锁" if initialized else "创建保险柜"
    hint = "请输入主密码（不会进入 AI 对话）" if initialized else "请设置主密码（≥8 位，不会进入 AI 对话）"

    try:
        from src.cli.interactive_prompt import prompt_password

        pw = await prompt_password(question, hint=hint)
        if pw is not None:
            return _PasswordPromptResult(password=pw)
    except RuntimeError:
        pass

    if sys.stdin.isatty():
        try:
            label = "保险柜主密码: " if initialized else "设置保险柜主密码（≥8位）: "
            pw = await asyncio.to_thread(getpass.getpass, label)
            if not initialized and pw:
                pw2 = await asyncio.to_thread(getpass.getpass, "再次输入主密码: ")
                if pw != pw2:
                    return _PasswordPromptResult()
            return _PasswordPromptResult(password=pw or None)
        except (EOFError, KeyboardInterrupt):
            return _PasswordPromptResult(cancelled=True)
    return _PasswordPromptResult(no_prompt=True)


def _service(parent: CoaraBase | None):
    service = get_vault_service(parent) if parent else None
    if service is None:
        raise RuntimeError("vault system is not enabled")
    return service


class _VaultOpenInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara

    def get_description(self) -> str:
        return "Vault open"

    async def execute(self, signal=None) -> ToolResult:
        try:
            service = _service(self._parent)
        except RuntimeError:
            return ToolResult(content="错误: 保险柜未启用。", is_error=True)

        if service.is_unlocked() and service.open_dir.is_dir():
            path = str(service.open_dir.resolve())
            service.touch_activity()
            return ToolResult(
                content=_continue_message(path),
                metadata={"open_dir": path},
            )

        initialized = service.is_initialized()
        prompt_result = await _ask_password(self._parent, initialized=initialized)
        if prompt_result.password:
            ok, _, message = await service.setup_or_unlock_with_feedback_async(prompt_result.password, persistent=False)
            if not ok:
                return ToolResult(content=message, is_error=True)
        elif prompt_result.cancelled:
            return ToolResult(content="用户取消了保险柜解锁。", is_error=True)
        elif prompt_result.timed_out:
            # Timeout doesn't guarantee the vault is still locked — a
            # cross-frontend unlock (e.g. Matrix user sent the password right
            # as the Web prompt timed out) may have unlocked it. Re-check
            # before reporting failure so the agent can proceed.
            if service.is_unlocked() and service.open_dir.is_dir():
                path = str(service.open_dir.resolve())
                service.touch_activity()
                return ToolResult(
                    content=_continue_message(path),
                    metadata={"open_dir": path},
                )
            return ToolResult(content="保险柜解锁超时，请重新调用 vault(action=open)。", is_error=True)
        elif prompt_result.no_prompt:
            return ToolResult(
                content=(
                    "当前环境无法弹出密码框（无 TTY）。请设置 COARA_VAULT_PASSWORD 环境变量，或在有终端的环境运行。"
                ),
                is_error=True,
            )
        elif prompt_result.error:
            return ToolResult(content=prompt_result.error, is_error=True)
        elif not service.is_unlocked():
            return ToolResult(
                content="未输入密码，保险柜仍锁定。请在弹窗中输入主密码后，再调用一次 vault(action=open)。",
                is_error=True,
            )

        path = str(service.open_dir.resolve())
        return ToolResult(
            content=_continue_message(path),
            metadata={"open_dir": path},
        )


class _VaultCloseInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None = None):
        super().__init__(params)
        self._parent = parent_coara

    def get_description(self) -> str:
        return "Vault close"

    async def execute(self, signal=None) -> ToolResult:
        try:
            service = _service(self._parent)
        except RuntimeError:
            return ToolResult(content="错误: 保险柜未启用。", is_error=True)
        service.lock()
        return ToolResult(content="保险柜已关闭并上锁。再次访问需 vault(action=open) 输入密码。")


class VaultTool(BaseTool):
    name = "vault"
    summary = "保险柜工具，用于存放管理用户敏感内容，仅用户发起时用"
    display_name = "Vault"
    kind = ToolKind.OTHER
    owner_only = True
    requires_approval = False
    should_defer = True  # 低频工具：schema 不常驻 prompt，经 tool(action=activate) 按需装载
    description = """保险柜工具，用于存放管理用户敏感内容，账号密码等，不限形式

- open：打开宝箱，用户需要在弹窗中输入密码
  开门后返回保险柜目录的绝对路径，之后用 read/write/edit 等普通文件工具按用户意图操作
- close：关门并清除明文目录，内容重新封存"""
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "close"],
                "description": "open=开门；close=关门",
            },
        },
        "required": ["action"],
    }

    def __init__(self, parent_coara: CoaraBase | None = None) -> None:
        self._parent = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        action = str(params.get("action") or "").strip()
        if action not in _VAULT_ACTIONS:
            raise ValueError(f"未知的 vault action: {action!r}。请使用 open/close。")
        if action == "open":
            return _VaultOpenInvocation(params, parent_coara=self._parent)
        return _VaultCloseInvocation(params, parent_coara=self._parent)
