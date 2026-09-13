"""Tool execution policy — binary call-layer approval gate.

A tool call is either allowed immediately or requires human confirmation.
Approval is decided by the tool itself (via ``requires_approval`` on the
tool class) OR by the LLM (via ``require_approval: true`` in the call
arguments). This is an OR gate — if either side asks for approval, the
call is prompted.

Additionally, tools listed in config ``call_policy.prompt`` always prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.core.abort import AbortSignal


@dataclass(slots=True)
class ToolCallDecision:
    """Resolved call-layer decision for a single tool invocation."""

    allowed: bool
    level: str
    reason: str = ""


# Default timeout for human confirmation prompts (5 minutes).
_DEFAULT_CONFIRM_TIMEOUT_SECONDS: float = 300.0


def _is_system_maintenance_agent(coara: CoaraBase) -> bool:
    """True for janitor/daily — system-dispatched maintenance, never prompt the user."""
    from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES

    persona = getattr(getattr(coara, "identity", None), "persona", None)
    name = str(getattr(persona, "name", "") or "").strip().lower()
    return name in SYSTEM_ONLY_SUBAGENT_TYPES


def _skips_call_approval(coara: CoaraBase) -> bool:
    """非主会话运行时不弹审批：子智能体（delegate_depth≥1）与 janitor/daily。

    子智能体由主会话委派，工具装配不带 require_approval；审批应发生在委派边界
    （用户是否发起任务），而不是子智能体跑到一半卡住等人点确认。
    """
    if _is_system_maintenance_agent(coara):
        return True
    # 只用真实 int（MagicMock 的 int() 默认是 1，不能拿来当深度）
    depth = getattr(coara, "delegate_depth", 0)
    if type(depth) is int and depth >= 1:
        return True
    identity = getattr(coara, "identity", None)
    return identity is not None and getattr(identity, "user_facing", True) is False


class ToolExecutionPolicy:
    """Resolve whether a tool invocation should run or be cancelled."""

    def __init__(self, config_manager: Any) -> None:
        self._config_manager = config_manager
        self.refresh()

    def refresh(self) -> None:
        """Reload call-policy sets from config. Call when config changes."""
        policy = self._config_manager.get_security_config().get("call_policy", {})
        self._force_prompt = set(policy.get("prompt", []))
        # 预批准工具：即使工具/模型请求审批也直接放行（call_policy.prompt 优先）。
        self._auto_allow = set(policy.get("auto_allow", []))

    async def resolve(
        self,
        coara: CoaraBase,
        tool_name: str,
        tool: Any,
        arguments: dict[str, Any],
        invocation: Any,
        signal: AbortSignal | None,
    ) -> ToolCallDecision:
        # 子智能体 / 系统维护：无人值守通道，不能弹审批（含 Matrix）。
        if _skips_call_approval(coara):
            return ToolCallDecision(allowed=True, level="allow")

        # 审批分身份：拥有者本人（默认）不做工具级硬门——审批完全由模型自决，
        # 只有模型自己在调用里写 require_approval 才弹；外部人员的回合硬门全生效
        # （沙箱之外的第二层），审批送到拥有者。
        config_forced = tool_name in self._force_prompt
        tool_needs = (
            False
            if self._is_owner_context(coara, invocation)
            else self._tool_requires_prompt(tool, tool_name, arguments)
        )
        llm_needs = self._llm_requires_prompt(arguments)
        needs_approval = config_forced or tool_needs or llm_needs
        level = "prompt" if needs_approval else "allow"

        if not needs_approval:
            return ToolCallDecision(allowed=True, level=level)

        # 预批准（auto_allow）直接放行；显式配置 prompt 的工具除外。
        if not config_forced and tool_name in self._auto_allow:
            return ToolCallDecision(allowed=True, level="allow")

        llm_reason = self._extract_approval_reason(arguments)

        try:
            confirmed = await self.confirm_tool_execution(
                invocation,
                level=level,
                reason=llm_reason,
                timeout_seconds=_DEFAULT_CONFIRM_TIMEOUT_SECONDS,
                signal=signal,
                coara=coara,
            )
        except TimeoutError:
            return ToolCallDecision(
                allowed=False,
                level=level,
                reason=f"用户未在 5 分钟内审批，{tool_name} 未执行。",
            )
        except Exception as exc:
            from src.agent.approval_baseline import ApprovalFileDriftError
            from src.coara.remote_channel import RemotePromptDeliveryError
            from src.coara.turn_completion import CoaraRunCancelledError
            from src.core.abort import OperationAborted

            if isinstance(exc, ApprovalFileDriftError):
                return ToolCallDecision(
                    allowed=False,
                    level=level,
                    reason=str(exc) or f"文件在审批期间被改动，{tool_name} 未执行。",
                )
            if isinstance(exc, RemotePromptDeliveryError):
                return ToolCallDecision(
                    allowed=False,
                    level=level,
                    reason=str(exc) or f"审批请求发送失败，{tool_name} 未执行。",
                )
            if isinstance(exc, OperationAborted):
                raise CoaraRunCancelledError(exc.reason or "interrupted") from exc
            raise

        if confirmed:
            return ToolCallDecision(allowed=True, level=level)

        return ToolCallDecision(
            allowed=False,
            level=level,
            reason=f"用户拒绝了 {tool_name} 调用。",
        )

    @staticmethod
    def _is_owner_context(coara: Any, invocation: Any) -> bool:
        """当前回合是不是拥有者本人发起的（内网默认）。

        拥有者本人：工具的静态硬门一律不参与，审批完全交给模型自决——机制不
        替模型做「什么危险」的判断，只保留模型自己声明的 require_approval 与
        用户在配置里点名的 call_policy.prompt。
        外部人员：executor 注入 ``trust_level``，硬门全生效（沙箱之后的第二层），
        审批送到拥有者那里。
        拿不到身份时按拥有者处理：当前系统只有用户自己，宁少打扰、不误拦。
        """
        for holder in (invocation, coara):
            trust = str(getattr(holder, "_trust_level", "") or "").strip().lower()
            if trust:
                return trust in {"owner", "trusted"}
        return True

    def _tool_requires_prompt(
        self,
        tool: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        """Whether the tool itself or config policy says this call needs approval."""
        # Tool-declared approval (staticmethod on the tool class, or bool).
        if self._tool_declares_approval(tool, arguments):
            return True
        # Config-forced prompt (call_policy.prompt in config.yaml).
        return tool_name in self._force_prompt

    @staticmethod
    def _tool_declares_approval(tool: Any, arguments: dict[str, Any]) -> bool:
        """Read ``requires_approval`` from the tool class.

        Static tools declare a bool; dynamic tools override with a
        ``@staticmethod`` taking the call arguments and returning a bool.
        """
        if tool is None:
            return False
        req = getattr(tool, "requires_approval", False)
        if isinstance(req, bool):
            return req
        if callable(req):
            try:
                return bool(req(arguments))
            except Exception:
                # fail-closed：审批判定器出 bug 时按「需要审批」处理，
                # 不让一个异常把危险工具静默放行。
                logger.warning(
                    f"requires_approval probe raised for {getattr(tool, 'name', '?')}; defaulting to require approval"
                )
                return True
        return False

    @staticmethod
    def _llm_requires_prompt(arguments: dict[str, Any]) -> bool:
        """Whether the LLM explicitly requested approval for this call."""
        return isinstance(arguments, dict) and arguments.get("require_approval") is True

    @staticmethod
    def _extract_approval_reason(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            return ""
        reason = str(arguments.get("approval_reason") or "").strip()
        return reason

    @staticmethod
    async def confirm_tool_execution(
        invocation: Any,
        level: str = "prompt",
        reason: str = "",
        *,
        timeout_seconds: float = _DEFAULT_CONFIRM_TIMEOUT_SECONDS,
        signal: AbortSignal | None = None,
        coara: Any = None,
    ) -> bool:
        """Ask user to confirm a tool execution.

        Returns ``True`` if the user agrees, ``False`` if they decline.
        Raises ``TimeoutError`` if the user does not respond within the timeout.
        Raises ``RemotePromptDeliveryError`` when no approval channel is
        reachable — fail-closed instead of silently auto-approving.
        """
        # 防御纵深：本函数只在 needs_approval 时被调用（level 恒 prompt）。
        # 出现 allow 说明调用方逻辑被改坏——拒绝比静默放行安全。
        if level != "prompt":
            logger.warning(f"confirm_tool_execution called with non-prompt level={level!r}; fail-closed")
            return False

        from src.agent.approval_baseline import (
            capture_approval_file_baseline,
            verify_approval_file_baseline,
        )

        # Bind target file content before the user sees the prompt / waits.
        # If the file changes during the window, refuse after approval.
        capture_approval_file_baseline(invocation)

        question = invocation.get_description()
        if reason:
            question = f"{question}\n\n审批原因：{reason}"
        options = [
            {"label": "同意", "description": ""},
            {"label": "不同意", "description": ""},
        ]

        from src.coara.turn_context import get_end_channel, get_turn_channel

        # 审批回 origin（方案 D4）：优先取当前回合 EndChannel 承载的交互通道——
        # turn / set_turn_context 均已重建 EndChannel，覆盖正常与 deferred
        # 接续两种路径；取不到再降级旧 ContextVar 与 origin 兜底。
        end_channel = get_end_channel()
        remote_channel = (end_channel.interaction_channel if end_channel is not None else None) or get_turn_channel()
        if remote_channel is None and coara is not None:
            remote_channel = getattr(coara, "_origin_remote_channel", None)

        confirmed = False
        if remote_channel is not None:
            confirmed = await remote_channel.confirm(
                question, options, timeout_seconds=timeout_seconds, signal=signal
            )
        else:
            try:
                from src.coara.frontend import get_frontend
                from src.coara.tool_output.pipeline import render_gate_preview

                preview = await render_gate_preview(invocation)
                result = await get_frontend().prompt_select(
                    question=question, options=options, preview=preview, timeout=timeout_seconds
                )
                confirmed = result is not None and result.get("selection") == "同意"
            except TimeoutError:
                raise
            except RuntimeError:
                # prompt_select 抛 RuntimeError 意味着 modal spinner 未注册（CLI 未初始化，
                # 如 daemon/tray 内核）。回合外无远端回合通道时 fail-closed：审批只发回合
                # 来源端，不设矩阵 standing 逃生门；需要无人值守放行应走
                # config call_policy.auto_allow 显式声明。曾在此按「无 tty 即自动放行」——
                # daemon 内核恒无 tty，导致危险操作被静默执行（手机端收不到审批）。
                from src.coara.remote_channel import RemotePromptDeliveryError

                logger.warning(
                    "Tool approval has no reachable delivery channel; denying instead of auto-approving (reason={})",
                    reason or "no channel",
                )
                raise RemotePromptDeliveryError(
                    "审批请求无法送达：当前没有可用的确认通道（无 CLI / 无远端回合通道）。已拒绝执行以避免静默放行。"
                ) from None

        if not confirmed:
            return False
        drift = verify_approval_file_baseline(invocation)
        if drift:
            from src.agent.approval_baseline import ApprovalFileDriftError

            raise ApprovalFileDriftError(drift)
        return True
