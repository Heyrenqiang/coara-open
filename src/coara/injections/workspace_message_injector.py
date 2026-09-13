"""Inject workspace updates reminders when user message contains workspace update quotes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.message_tags import system_reminder
from src.core.types import Message, MessageRole

_PROJECT_MSG_OPEN = re.compile(
    r"<工作空间消息(?P<attrs>[^>]*)>(?P<body>.*?)</工作空间消息>",
    re.DOTALL,
)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


@dataclass(frozen=True, slots=True)
class WorkspaceMessageRef:
    workspace: str
    message_id: str | None
    body: str


def _parse_attrs(raw: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _ATTR.finditer(raw or "")}


def extract_workspace_message_refs(content: str) -> list[WorkspaceMessageRef]:
    """Parse ``<工作空间消息>`` blocks."""
    refs: list[WorkspaceMessageRef] = []
    for match in _PROJECT_MSG_OPEN.finditer(content):
        attrs = _parse_attrs(match.group("attrs"))
        body = match.group("body").strip()
        if not body:
            continue
        workspace = (attrs.get("workspace") or "").strip()
        refs.append(
            WorkspaceMessageRef(
                workspace=workspace,
                message_id=(attrs.get("message_id") or "").strip() or None,
                body=body,
            )
        )
    return refs


def build_workspace_message_reminder(refs: list[WorkspaceMessageRef]) -> str:
    workspaces = sorted({r.workspace for r in refs if r.workspace})
    ws_label = ", ".join(f"{w}" for w in workspaces) if workspaces else "<工作空间名>"
    id_lines = [f"- {r.message_id} ({r.workspace})" for r in refs if r.message_id]
    ids_block = "\n".join(id_lines)

    lines = [
        "用户在工作空间消息上写了批示（见上一条用户消息），按批示执行（答复/处理/忽略/搁置）。若指示是处理，你必须：",
        "",
        f"1. **`ws(action=list)`** 确认 {ws_label} 磁盘路径",
        "2. **`read(<工作空间路径>/AGENTS.md)`** — 必做（不存在则说明，仍继续）",
        "3. 结合上方工作空间消息与用户指示，**直接**用工具处理（必要时可 `delegate(coaras)` 并行分支）",
        "4. 处理完成经用户确认后归档：`edit` 对应 `<工作空间目录>/.coara/inbox/<message_id>.json`"
        " 把 `status` 改为 `archived`，或请用户用 `/ws updates archive`",
        "",
        "上方 `<工作空间消息>` 已是展示用摘要；"
        "完整 webhook payload 在 `{工作空间目录}/.coara/inbox/` 落盘，无需再 read 动态原文。",
    ]
    if ids_block:
        lines.extend(["", "关联 message_id：", ids_block])
    if len(workspaces) > 1:
        lines.extend(
            [
                "",
                f"涉及多个 workspace：{ws_label}；按空间分别处理或分别委派。",
            ]
        )
    return system_reminder("\n".join(lines))


def build_workspace_message_reminder_messages(refs: list[WorkspaceMessageRef]) -> list[Message]:
    if not refs:
        return []
    return [Message(role=MessageRole.USER, content=build_workspace_message_reminder(refs))]
