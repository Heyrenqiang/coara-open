"""Loop detection helpers for tool execution."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from src.core.tool_base import ToolResult
from src.core.types import ToolCall
from src.tools.builtin.runtime.shell_support import is_agnes_video_poll_command


@dataclass(slots=True)
class LoopAlert:
    """Describes a repeated tool-call pattern."""

    tool_name: str
    fingerprint: str
    count: int

    @property
    def feedback(self) -> str:
        base = (
            f"Detected a repeated tool-call loop for '{self.tool_name}' after {self.count} attempts. "
            "Do not retry with the same arguments again. "
        )
        if self.tool_name == "shell":
            return base + (
                "If you are waiting for an external condition, run ONE command that sleeps "
                "and re-checks internally (e.g. a loop with Start-Sleep), or start it in the "
                "background and end your turn; otherwise change strategy or respond directly."
            )
        return base + "Use a different tool, change the arguments, or respond directly."


@dataclass(slots=True)
class StagnationAlert:
    """Describes a turn-level lack of progress (repeated tool errors)."""

    reason: str
    repeated_turns: int

    @property
    def feedback(self) -> str:
        return (
            f"Detected {self.repeated_turns} consecutive turns hitting the same tool error. "
            "Stop repeating the same failing action; summarize what failed, change strategy, or ask the user."
        )


# 循环检测阈值配置
# 参照 qwen-code: TOOL_CALL_LOOP_THRESHOLD = 5
DEFAULT_LOOP_THRESHOLD = 5
MAX_HISTORY_LENGTH = 100
DEFAULT_REPEATED_RESPONSE_THRESHOLD = 4
DEFAULT_SHELL_FAILURE_STREAK_THRESHOLD = 10

# 不同工具类型的阈值差异化配置
TOOL_TYPE_THRESHOLDS = {
    # 读取类工具阈值较低（容易误报）
    "read": 3,
    "glob": 3,
    "grep": 3,
    # 执行类工具阈值较高（允许修复-重试模式）
    "shell": 5,
    "write": 5,
    "edit": 5,
    "delete": 5,
    # 搜索类工具使用默认阈值
    "web_search": 5,
    "web_fetch": 5,
    # 媒体队列查/取消：禁止反复 status（同原 task list）
    "media": 2,
}

# Agnes video poll：同轮第二次即拦截（应后台一条命令 + 结束对话）
AGNES_VIDEO_POLL_LOOP_THRESHOLD = 2

# 滑窗累计倍率：连续相同调用的 streak 会被一次不同调用清零，
# "5×A, 1×B, 5×A" 式循环可以永远绕过尾部计数。改为在滑窗内累计
# 同一指纹出现次数，达到 streak 阈值的该倍率即拦截。
# 修复-重试模式仍由 _is_repair_retry 豁免，不误伤正常迭代。
WINDOW_CUMULATIVE_FACTOR = 2

AGNES_VIDEO_POLL_LOOP_FEEDBACK = (
    "同轮已成功发起过一次 Agnes video --poll，请勿再次调用 video --poll / video-poll。"
    "请等待后台任务完成提醒，或仅用 video --json 查询一次后结束本轮。"
)

_FIX_LIKE_COMMAND_NAMES = frozenset({"sed", "patch", "replace", "fix"})


def _is_delegate_wait(tool_call: ToolCall) -> bool:
    """True for delegate(action="wait"): blocking waits are normal workflow, never loops."""
    if tool_call.name != "delegate":
        return False
    arguments = tool_call.arguments
    return isinstance(arguments, dict) and arguments.get("action") == "wait"


def _is_fix_like_shell_command(arguments: Any) -> bool:
    """True when the shell command starts with an in-place fix command.

    Matches the command-start token only, so paths like ``cd prefix`` no
    longer false-positive on the "fix" keyword.
    """
    command = str(arguments.get("command") or "") if isinstance(arguments, dict) else str(arguments)
    stripped = command.strip().lower()
    if not stripped:
        return False
    return stripped.split(None, 1)[0] in _FIX_LIKE_COMMAND_NAMES


class LoopDetector:
    """Detect repeated tool calls with identical arguments.

    Loop detection thresholds:
    - TOOL_CALL_LOOP_THRESHOLD = 5 (default)
    - Different thresholds for different tool types
    - Supports repair-retry pattern detection
    """

    def __init__(self, threshold: int = DEFAULT_LOOP_THRESHOLD, max_history: int = MAX_HISTORY_LENGTH):
        self.threshold = threshold
        self._history: deque[str] = deque(maxlen=max_history)
        self._tool_names: dict[str, str] = {}
        self._agnes_poll_success_count = 0

    def _get_threshold(self, tool_name: str) -> int:
        """Get threshold for specific tool type.

        Allows different thresholds for different tool categories:
        - Read tools: lower threshold (3) to prevent excessive reading
        - Write/Exec tools: higher threshold (5) to allow fix-retry patterns
        """
        return TOOL_TYPE_THRESHOLDS.get(tool_name, self.threshold)

    def should_block(self, tool_call: ToolCall, message_history: list[Any] | None = None) -> LoopAlert | None:
        """Block the next call if the same fingerprint was already repeated.

        Optimized: Detects 'repair-retry' patterns. If the last call failed and
        a modifying tool (like 'write') was called in between, allow retry.
        Uses tool-type-specific thresholds.
        """
        if tool_call.name == "shell":
            command = str((tool_call.arguments or {}).get("command") or "")
            if is_agnes_video_poll_command(command):
                if self._agnes_poll_success_count >= AGNES_VIDEO_POLL_LOOP_THRESHOLD - 1:
                    fingerprint = self._fingerprint(tool_call)
                    return LoopAlert(
                        tool_name=tool_call.name,
                        fingerprint=fingerprint,
                        count=self._agnes_poll_success_count + 1,
                    )
                # First Agnes poll in the turn is always allowed (smoke-test / video --json do not count).
                return None

        fingerprint = self._fingerprint(tool_call)
        threshold = self._get_threshold(tool_call.name)

        # delegate(action="wait") blocks on real events (subagent completion or
        # user input); repeated waits are the intended waiting pattern, not a loop.
        if _is_delegate_wait(tool_call):
            return None

        # Optimization 1: Check for repair-retry pattern
        if self._is_repair_retry(tool_call, message_history):
            return None

        repeated = self._count_trailing_matches(fingerprint)
        if repeated >= threshold - 1:
            return LoopAlert(
                tool_name=tool_call.name,
                fingerprint=fingerprint,
                count=repeated + 1,
            )
        window_count = self._count_window_matches(fingerprint)
        if window_count >= threshold * WINDOW_CUMULATIVE_FACTOR:
            return LoopAlert(
                tool_name=tool_call.name,
                fingerprint=fingerprint,
                count=window_count + 1,
            )
        alternating = self._count_alternating_matches(fingerprint)
        # An alternation shorter than two full A/B cycles is just a tool switch,
        # not a ping-pong loop. Require at least 4 so low thresholds (e.g.
        # media=2) don't block the first call that merely follows a different tool.
        if alternating >= max(threshold, 4):
            return LoopAlert(
                tool_name=tool_call.name,
                fingerprint=fingerprint,
                count=alternating,
            )
        return None

    def _is_repair_retry(self, tool_call: ToolCall, message_history: list[Any] | None) -> bool:
        """Check if this is a retry after fixing an error.

        Pattern: Error → Fix (write/edit/shell) → Retry (shell)
        Only allow once per error to prevent infinite loops.
        """
        if not message_history or len(message_history) < 3:
            return False
        if tool_call.name not in ("shell", "write"):
            return False

        # Scan recent message history to find: fix then error then retry
        recent = message_history[-10:]

        found_fix = False
        found_error_after_fix = False
        for msg in recent:
            if hasattr(msg, "role"):
                role_value = msg.role.value if hasattr(msg.role, "value") else msg.role
                if role_value == "assistant":
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            # Fix tools: write, edit, or shell with sed/patch-like commands
                            if tc.name in ("write", "edit") or (
                                tc.name == "shell" and _is_fix_like_shell_command(tc.arguments)
                            ):
                                found_fix = True
                if found_fix and role_value == "tool":
                    content = str(getattr(msg, "content", ""))
                    if any(err in content.lower() for err in ["error", "exception", "traceback", "failed"]):
                        found_error_after_fix = True

                # If we see another fix after an error, reset the pattern
                if found_error_after_fix and role_value == "assistant":
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            if tc.name in ("write", "edit"):
                                found_fix = True
                                found_error_after_fix = False

        # Only allow retry if we saw: fix → error → (now retrying)
        return found_fix and found_error_after_fix

    def record(self, tool_call: ToolCall, *, successful: bool = True) -> None:
        """Record a completed tool call."""
        if _is_delegate_wait(tool_call):
            return
        if tool_call.name == "shell":
            command = str((tool_call.arguments or {}).get("command") or "")
            if is_agnes_video_poll_command(command):
                if not successful:
                    return
                self._agnes_poll_success_count += 1

        if not successful:
            return

        fingerprint = self._fingerprint(tool_call)
        self._history.append(fingerprint)
        self._tool_names[fingerprint] = tool_call.name

    def clear(self) -> None:
        """Reset loop history."""
        self._history.clear()
        self._tool_names.clear()
        self._agnes_poll_success_count = 0

    def _fingerprint(self, tool_call: ToolCall) -> str:
        if tool_call.name == "shell":
            command = str((tool_call.arguments or {}).get("command") or "")
            if is_agnes_video_poll_command(command):
                payload = {"name": "shell", "arguments": {"agnes_video_poll": True}}
                return hashlib.md5(
                    json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
        payload = {
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        return hashlib.md5(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _count_trailing_matches(self, fingerprint: str) -> int:
        count = 0
        for item in reversed(self._history):
            if item != fingerprint:
                break
            count += 1
        return count

    def _count_window_matches(self, fingerprint: str) -> int:
        """Total occurrences in the sliding window (streaks don't reset it)."""
        return sum(1 for item in self._history if item == fingerprint)

    def _count_alternating_matches(self, fingerprint: str) -> int:
        if len(self._history) < 2:
            return 0
        previous = self._history[-1]
        if previous == fingerprint:
            return 0

        sequence = list(self._history) + [fingerprint]
        count = 0
        expected = fingerprint
        for item in reversed(sequence):
            if item != expected:
                break
            count += 1
            expected = previous if expected == fingerprint else fingerprint
        return count


@dataclass(slots=True)
class ShellFailureStreakAlert:
    """Too many consecutive ``shell`` errors (even with different commands)."""

    streak: int

    @property
    def feedback(self) -> str:
        return (
            f"Detected {self.streak} consecutive failed shell calls. "
            "Stop retrying shell commands; summarize what failed, change strategy, "
            "or ask the user instead of running more shell diagnostics."
        )


class ShellFailureStreakGuard:
    """Track consecutive failed ``shell`` results; other tools do not reset the streak."""

    _GUARD_MARKER = "consecutive failed shell"

    def __init__(self, threshold: int = DEFAULT_SHELL_FAILURE_STREAK_THRESHOLD) -> None:
        self.threshold = threshold
        self._streak = 0

    def should_block(self, tool_name: str) -> ShellFailureStreakAlert | None:
        if tool_name != "shell" or self._streak < self.threshold:
            return None
        return ShellFailureStreakAlert(streak=self._streak)

    def _shell_result_counts_as_failure(self, result: ToolResult) -> bool:
        if result.is_error or getattr(result, "is_cancelled", False):
            return True
        metadata = result.metadata
        return isinstance(metadata, dict) and metadata.get("failed") is True

    def record(self, tool_name: str, result: ToolResult) -> None:
        if tool_name != "shell":
            return
        content = str(result.content or "").lower()
        if self._GUARD_MARKER in content:
            return
        if self._shell_result_counts_as_failure(result):
            self._streak += 1
        else:
            self._streak = 0

    def clear(self) -> None:
        self._streak = 0


class TurnStagnationGuard:
    """Detect consecutive turns that hit the same tool error.

    Previously this guard fired on repeated LLM response signatures, which
    produced many false positives for legitimate iterative workflows
    (edit -> run -> edit -> run). It now only fires when the same tool error
    repeats across turns, keeping a safety net for real failure loops while
    staying out of the way during normal iteration.
    """

    def __init__(
        self,
        repeated_error_threshold: int = DEFAULT_REPEATED_RESPONSE_THRESHOLD,
    ):
        self.repeated_error_threshold = repeated_error_threshold
        self._last_error_signature: str | None = None
        self._consecutive_same_error_turns = 0

    def record(
        self,
        *,
        made_progress: bool,
        error_signature: str | None = None,
    ) -> StagnationAlert | None:
        if made_progress:
            self.reset()
            return None

        if error_signature is None:
            # No failing tool call this turn: reset the error streak.
            self.reset()
            return None

        if error_signature == self._last_error_signature:
            self._consecutive_same_error_turns += 1
        else:
            self._last_error_signature = error_signature
            self._consecutive_same_error_turns = 1

        if self._consecutive_same_error_turns >= self.repeated_error_threshold:
            return StagnationAlert(
                reason="repeated_error",
                repeated_turns=self._consecutive_same_error_turns,
            )

        return None

    def reset(self) -> None:
        self._last_error_signature = None
        self._consecutive_same_error_turns = 0
