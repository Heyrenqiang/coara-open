"""消息包装标签库单一事实源（core 层，零依赖）.

Unified tag helpers for wrapped message bodies. Avoids hard-coding tag names
across the codebase; if we ever want to rename the tags, we only change them
here. 支撑层（todos/skills/session_log/records 等）与内核层（coara/agent/tools）
共用这套标签包装。标签定义只能放在 core——放在 coara 层会让支撑层反向依赖
内核层。
"""

from __future__ import annotations

import re
from typing import Any


def system_info(content: str) -> str:
    """Wrap content in `<系统消息>` (informational, no behavioral constraint)."""
    return f"<系统消息>\n{content}\n</系统消息>"


def system_reminder(content: str) -> str:
    """Wrap content in `<系统提醒>` (authoritative directive, must be heeded)."""
    return f"<系统提醒>\n{content}\n</系统提醒>"


def situation(content: str) -> str:
    """Wrap content in `<情境>` (per-turn live snapshot, ephemeral, not persisted).

    情境后缀每轮 LLM 调用末尾现取现挂，不写回 message_history。与一次性
    参考资料 `<系统消息>` 区分：情境是实时状态（如当前对话端），多轮并存时
    以最新一轮为准。
    """
    return f"<情境>\n{content}\n</情境>"


LLM_ONLY_OPEN = "<仅模型可见>"
LLM_ONLY_CLOSE = "</仅模型可见>"
_LLM_ONLY_RE = re.compile(
    rf"{re.escape(LLM_ONLY_OPEN)}.*?{re.escape(LLM_ONLY_CLOSE)}\s*",
    re.DOTALL,
)


def llm_only(content: str) -> str:
    """包裹仅给 LLM 看的指令：进 message_history 供模型阅读，显示层（CLI/Web）
    用 [strip_llm_only] 剥除，用户不可见。用于「不要读日志」这类纯行为指令。"""
    return f"{LLM_ONLY_OPEN}{content.strip()}{LLM_ONLY_CLOSE}"


def strip_llm_only(text: str) -> str:
    """剥除所有 `<仅模型可见>` 段，供显示层调用。"""
    return _LLM_ONLY_RE.sub("", text)


CONTINUATION_OPEN = "<接续输入>"
CONTINUATION_CLOSE = "</接续输入>"

TOOL_DECL_OPEN = "<工具声明>"
TOOL_DECL_CLOSE = "</工具声明>"
_TOOL_DECL_BLOCK_RE = re.compile(
    rf"^{re.escape(TOOL_DECL_OPEN)}\s*(?P<body>.*?)\s*{re.escape(TOOL_DECL_CLOSE)}\s*$",
    re.DOTALL,
)


def tool_declaration(payload_json: str) -> str:
    """Wrap dynamic tool declaration payload（K3 dynamic tool loading 的私有载体格式）。

    该消息进 user 角色历史；仅 Kimi OpenAI 驱动（k3）在请求转换时识别并改写成
    ``{"role":"system","tools":[...]}`` 电线消息（dynamic tool loading：工具从该
    位置起生效、纯后缀追加不动前缀缓存）。其它 provider 按普通系统消息透传文本，
    内容本身（"此工具已可用"+schema）语义无害。
    """
    return f"{TOOL_DECL_OPEN}\n{payload_json.strip()}\n{TOOL_DECL_CLOSE}"


def strip_tool_declaration(text: str) -> str | None:
    """Extract declaration payload from a ``<工具声明>`` wrapper; None when not a decl."""
    match = _TOOL_DECL_BLOCK_RE.match(text.strip())
    return match.group("body").strip() if match else None


_CONTINUATION_BLOCK_RE = re.compile(
    rf"^{re.escape(CONTINUATION_OPEN)}\s*(?P<body>.*?)\s*{re.escape(CONTINUATION_CLOSE)}\s*$",
    re.DOTALL,
)


def strip_continuation_input(text: str) -> str:
    """Remove outer `<接续输入>` wrapper; return inner user text."""
    stripped = text.strip()
    match = _CONTINUATION_BLOCK_RE.match(stripped)
    if match:
        return match.group("body").strip()
    return stripped


SUBAGENT_MSG_OPEN = "<子智能体消息>"
SUBAGENT_MSG_CLOSE = "</子智能体消息>"

MIDRUN_MSG_OPEN = "<途中消息>"
MIDRUN_MSG_CLOSE = "</途中消息>"

BACKGROUND_RESULT_OPEN = "<后台结果>"
BACKGROUND_RESULT_CLOSE = "</后台结果>"

# Payloads already tagged for history — do not wrap again as user follow-ups.
_PREFORMATTED_INJECTION_PREFIXES = (
    "<系统消息>",
    "<系统提醒>",
    "<情境>",
    CONTINUATION_OPEN,
    SUBAGENT_MSG_OPEN,
    MIDRUN_MSG_OPEN,
    BACKGROUND_RESULT_OPEN,
)


def is_preformatted_injection(text: str) -> bool:
    """True when *text* is already a system/event/continuation injection block."""
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in _PREFORMATTED_INJECTION_PREFIXES)


def background_result(content: str) -> str:
    """Wrap bash / background-subagent completion payloads in ``<后台结果>``.

    Distinct from ``<系统提醒>`` (behavioral directives) and ``<子智能体消息>``
    (mid-run report pushes). Root prompt: use naturally, do not paraphrase.
    """
    return f"{BACKGROUND_RESULT_OPEN}\n{content.strip()}\n{BACKGROUND_RESULT_CLOSE}"


def continuation_input(content: str) -> str:
    """Wrap content in `<接续输入>` (user follow-up sent during an active turn).

    Behavioral rules are defined in Root ``root.md``.
    Only for user follow-ups（正文裸文本，来源标签已废弃）.
    System payloads must be injected as-is via ``format_continuation_for_history``.
    """
    return f"{CONTINUATION_OPEN}\n{content.strip()}\n{CONTINUATION_CLOSE}"


def subagent_message(content: str, *, task_id: str, description: str = "") -> str:
    """Wrap a delegated subagent's report for the parent session.

    Sub→主 messages are explicit: a subagent calls the ``report`` tool, which
    pushes this tagged block into the parent's continuation queue. The parent
    LLM sees it at its next iteration boundary. Intermediate chunks are no
    longer auto-forwarded (that caused the parent context to be re-fed to the
    LLM on every chunk — see the former auto-forward design in #330).
    """
    head = f"[{task_id}]"
    if description:
        head += f" {description}"
    return f"{SUBAGENT_MSG_OPEN}\n{head}\n{content.strip()}\n{SUBAGENT_MSG_CLOSE}"


def midrun_message(content: str) -> str:
    """Wrap a mid-run instruction from the parent session to a running subagent."""
    return f"{MIDRUN_MSG_OPEN}\n{content.strip()}\n{MIDRUN_MSG_CLOSE}"


TASK_INSTRUCTION_OPEN = "<任务指令>"
TASK_INSTRUCTION_CLOSE = "</任务指令>"


def task_instruction(content: str) -> str:
    """Wrap the initial task assignment from the main session to a subagent (spawn).

    与途中消息 `<主会话消息>` 区分：初始任务是子智能体的出生证明与最高约束。
    """
    return f"{TASK_INSTRUCTION_OPEN}\n{content.strip()}\n{TASK_INSTRUCTION_CLOSE}"


def format_continuation_for_history(text: str, *, is_mid_turn: bool = True) -> str:
    """Prepare a drained continuation queue item for ``message_history``.

    - Preformatted system/event/continuation payloads → unchanged
    - User follow-ups → ``<接续输入>`` when ``is_mid_turn=True``

    接续队列里的项只有在回合仍进行、于迭代头被 drain 进历史时才应
    ``is_mid_turn=True``（打 ``<接续输入>``）。回合已退出后的 leftover 走新回合
    ``process_message``，不经本函数。``is_mid_turn=False`` 仅保留给测试/旁路。
    """
    if is_preformatted_injection(text):
        return text
    if not is_mid_turn:
        return text.strip()
    return continuation_input(text.strip())


def continuation_user_display_text(text: str) -> str | None:
    """Plain user-visible text for a continuation queue item, or None if system/event."""
    if not isinstance(text, str) or not text.strip():
        return None
    if is_preformatted_injection(text):
        return None
    display = text.strip()
    return display or None


def continuation_followup_display_line(
    text: str,
    *,
    image_blocks: list[dict[str, Any]] | None = None,
) -> str | None:
    """User-visible one-liner for scrollback / queue hint (incl. image-only follow-ups)."""
    display = continuation_user_display_text(text)
    if display:
        return display
    count = len(image_blocks or [])
    if count == 1:
        return "[图片]"
    if count > 1:
        return f"[图片×{count}]"
    return None


_SYSTEM_INFO_RE = re.compile(
    r"^<系统消息>\s*(?P<body>.*?)\s*</系统消息>\s*$",
    re.DOTALL,
)
_FG_SUBAGENT_DONE_RE = re.compile(
    r"^\[前台子智能体已完成\]\s*\[(?P<tid>[^\]]+)\]\s*\n任务：(?P<desc>.*?)\n结果：(?P<body>.*)$",
    re.DOTALL,
)
_SUBAGENT_MSG_RE = re.compile(
    rf"^{re.escape(SUBAGENT_MSG_OPEN)}\s*(?P<body>.*?)\s*{re.escape(SUBAGENT_MSG_CLOSE)}\s*$",
    re.DOTALL,
)
# Keep CLI scrollback readable when a subagent dumps a long final report.
_SUBAGENT_DISPLAY_MAX_CHARS = 4000

# task_id shape from DelegateTool: ``sa-{subagent_type}-{8hex}``
_SA_TASK_ID_RE = re.compile(r"\[sa-([a-zA-Z0-9_一-鿿]+)-[0-9a-fA-F]+\]")


def subagent_scrollback_label(display_text: str) -> str:
    """CLI prefix for subagent scrollback lines, e.g. ``coaras子智能体``."""
    match = _SA_TASK_ID_RE.search(display_text or "")
    if match:
        return f"{match.group(1)}子智能体"
    return "子智能体"


def subagent_task_id(display_text: str) -> str:
    """回显文本里的子智能体 task_id（``sa-{类型}-{8hex}``）；无则空串。

    CLI 折叠块靠它把「子智能体完成」回显行归位到对应块（块 key = delegate
    工具行 call_id，task_id 是块上的别名）。
    """
    match = _SA_TASK_ID_RE.search(display_text or "")
    if not match:
        return ""
    return match.group(0).strip("[]")


def continuation_subagent_display_text(text: str) -> str | None:
    """CLI-visible body for subagent→parent continuation items, or None.

    Covers:
    - ``<系统消息>[前台子智能体已完成]…`` (foreground delegate final result)
    - ``<子智能体消息>…`` (explicit ``report`` tool push)

    Other system/event injections stay silent on the scrollback (model-only).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()

    info = _SYSTEM_INFO_RE.match(stripped)
    if info:
        inner = info.group("body").strip()
        done = _FG_SUBAGENT_DONE_RE.match(inner)
        if not done:
            return None
        tid = done.group("tid").strip()
        desc = done.group("desc").strip()
        body = done.group("body").strip()
        head = f"[{tid}]"
        if desc:
            head = f"{head} {desc}"
        if not body:
            return head
        if len(body) > _SUBAGENT_DISPLAY_MAX_CHARS:
            body = body[:_SUBAGENT_DISPLAY_MAX_CHARS].rstrip() + "…"
        return f"{head}\n{body}"

    msg = _SUBAGENT_MSG_RE.match(stripped)
    if msg:
        body = msg.group("body").strip()
        if not body:
            return None
        if len(body) > _SUBAGENT_DISPLAY_MAX_CHARS:
            body = body[:_SUBAGENT_DISPLAY_MAX_CHARS].rstrip() + "…"
        return body
    return None
