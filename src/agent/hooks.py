"""Hook chain for before-tool-call and post-tool-call checks.

Hook engine:
- Matcher-based hook registration (by tool name / pattern)
- PreCompact / PostCompact hooks for context compression lifecycle
- Fire-and-forget post hooks that don't break the main flow
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from src.core.tool_base import BaseTool, ToolResult
from src.core.types import ToolCall

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


@dataclass(slots=True)
class HookDecision:
    """Result of a before-tool-call hook."""

    allowed: bool = True
    reason: str | None = None


class BeforeToolCallHook(Protocol):
    """Protocol for before-tool-call hooks."""

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision: ...


class PostToolCallHook(Protocol):
    """Protocol for post-tool-call hooks."""

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        result: ToolResult,
        is_owner: bool,
    ) -> None: ...


class PreCompactHook(Protocol):
    """Protocol for hooks called before context compression."""

    async def run(
        self,
        message_count: int,
        estimated_tokens: int,
    ) -> None: ...


class PostCompactHook(Protocol):
    """Protocol for hooks called after context compression."""

    async def run(
        self,
        original_count: int,
        compressed_count: int,
        token_reduction: int,
    ) -> None: ...


def match_tool_name(pattern: str, tool_name: str) -> bool:
    """Match a tool name against a glob pattern (e.g. 'read', 'file_*')."""
    return fnmatch.fnmatch(tool_name, pattern)


class MatchedBeforeHook:
    """Wrapper that only invokes the inner hook when the tool name matches a pattern."""

    def __init__(self, pattern: str, inner: BeforeToolCallHook) -> None:
        self.pattern = pattern
        self.inner = inner

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision:
        if not match_tool_name(self.pattern, tool.name):
            return HookDecision()
        return await self.inner.run(coara, tool, tool_call, is_owner)


class MatchedPostHook:
    """Wrapper that only invokes the inner hook when the tool name matches a pattern."""

    def __init__(self, pattern: str, inner: PostToolCallHook) -> None:
        self.pattern = pattern
        self.inner = inner

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        result: ToolResult,
        is_owner: bool,
    ) -> None:
        if not match_tool_name(self.pattern, tool.name):
            return
        await self.inner.run(coara, tool, tool_call, result, is_owner)


class AccessPolicyHook:
    """Block ``owner_only`` tools when the caller is not the owner context.

    Visibility filtering in ``ToolManager`` already hides these tools from the
    LLM schema for non-owner contexts; this hook is the execution-time backstop
    if a tool is somehow invoked anyway.
    """

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision:
        if tool.owner_only and not is_owner:
            return HookDecision(
                allowed=False,
                reason=f"Tool '{tool.name}' is only available to the owner.",
            )
        return HookDecision()


class LoopDetectionHook:
    """Block repeated tool calls with the same arguments."""

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision:
        from src.agent.loop import AGNES_VIDEO_POLL_LOOP_FEEDBACK
        from src.tools.builtin.runtime.shell_support import is_agnes_video_poll_command

        alert = coara.loop_detector.should_block(tool_call, coara.message_history)
        if alert is None:
            return HookDecision()
        if tool_call.name == "shell":
            command = str((tool_call.arguments or {}).get("command") or "")
            if is_agnes_video_poll_command(command):
                return HookDecision(allowed=False, reason=AGNES_VIDEO_POLL_LOOP_FEEDBACK)
        return HookDecision(allowed=False, reason=alert.feedback)


class ShellFailureStreakBeforeHook:
    """Block ``shell`` when too many consecutive shell failures were recorded."""

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision:
        alert = coara.shell_failure_streak_guard.should_block(tool.name)
        if alert is None:
            return HookDecision()
        return HookDecision(allowed=False, reason=alert.feedback)


class ShellFailureStreakPostHook:
    """Record ``shell`` outcomes for the consecutive-failure streak guard."""

    async def run(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        result: ToolResult,
        is_owner: bool,
    ) -> None:
        coara.shell_failure_streak_guard.record(tool.name, result)


class ToolHookRunner:
    """Sequentially run before-tool-call and post-tool-call hooks.

    Supports matcher-based registration so hooks can target specific tools.
    """

    def __init__(self):
        self._before_hooks: list[BeforeToolCallHook] = []
        self._post_hooks: list[PostToolCallHook] = []

    def register_before(self, hook: BeforeToolCallHook, *, pattern: str | None = None) -> None:
        """Register a before-tool-call hook.

        If ``pattern`` is given, the hook only fires for tools whose name
        matches the glob pattern (e.g. ``"read"``, ``"file_*"``).
        """
        if pattern:
            hook = MatchedBeforeHook(pattern, hook)
        self._before_hooks.append(hook)

    def register_post(self, hook: PostToolCallHook, *, pattern: str | None = None) -> None:
        """Register a post-tool-call hook.

        If ``pattern`` is given, the hook only fires for matching tool names.
        """
        if pattern:
            hook = MatchedPostHook(pattern, hook)
        self._post_hooks.append(hook)

    async def run_before(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        is_owner: bool,
    ) -> HookDecision:
        for hook in self._before_hooks:
            try:
                decision = await hook.run(coara, tool, tool_call, is_owner)
            except Exception as exc:
                # 单个 hook 异常只记日志跳过，不炸整批工具调用（与 run_post 同策略）
                from src.core.logger import logger

                logger.warning(f"Before-tool hook '{hook.__class__.__name__}' failed: {exc}")
                continue
            if not decision.allowed:
                return decision

        return HookDecision()

    async def run_post(
        self,
        coara: CoaraBase,
        tool: BaseTool,
        tool_call: ToolCall,
        result: ToolResult,
        is_owner: bool,
    ) -> None:
        """Run post-tool-call hooks for cleanup and logging.

        Hooks are fire-and-forget: failures are logged but never break the main flow.
        """
        for hook in self._post_hooks:
            try:
                await hook.run(coara, tool, tool_call, result, is_owner)
            except Exception as exc:
                from src.core.logger import logger

                logger.warning(f"Post-tool hook '{hook.__class__.__name__}' failed: {exc}")


class CompactHookRunner:
    """Run pre/post context compression hooks.

    These hooks are fire-and-forget; failures are logged but do not block compression.
    """

    def __init__(self) -> None:
        self._pre_hooks: list[PreCompactHook] = []
        self._post_hooks: list[PostCompactHook] = []

    def register_post(self, hook: PostCompactHook) -> None:
        self._post_hooks.append(hook)

    async def run_pre(self, message_count: int, estimated_tokens: int) -> None:
        for hook in self._pre_hooks:
            try:
                await hook.run(message_count, estimated_tokens)
            except Exception as exc:
                from src.core.logger import logger

                logger.warning(f"Pre-compact hook '{hook.__class__.__name__}' failed: {exc}")

    async def run_post(
        self,
        original_count: int,
        compressed_count: int,
        token_reduction: int,
    ) -> None:
        for hook in self._post_hooks:
            try:
                await hook.run(original_count, compressed_count, token_reduction)
            except Exception as exc:
                from src.core.logger import logger

                logger.warning(f"Post-compact hook '{hook.__class__.__name__}' failed: {exc}")
