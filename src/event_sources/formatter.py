"""Format inbound events into inbox content text."""

from __future__ import annotations

from src.event_sources.types import EventSourceDefinition, InboundEvent

_EVENT_WRAPPER_TAGS = (
    ("<系统提醒>", "</系统提醒>"),
    ("<系统消息>", "</系统消息>"),
)


def _strip_existing_event_wrappers(text: str) -> str:
    body = text.strip()
    changed = True
    while changed:
        changed = False
        for open_tag, close_tag in _EVENT_WRAPPER_TAGS:
            if body.startswith(open_tag) and body.endswith(close_tag):
                inner = body[len(open_tag) : -len(close_tag)].strip()
                if inner != body:
                    body = inner
                    changed = True
    return body


def render_inbound_message(defn: EventSourceDefinition, event: InboundEvent) -> str:
    """事件原始事实 → 收件箱内容正文（纯文本，不带会话标签包装）。"""
    body = defn.message_template
    replacements = {
        "{{source_id}}": event.source_id,
        "{{workspace}}": event.workspace,
        "{{event_type}}": event.event_type,
        "{{details}}": event.details_text(),
    }
    for key, value in replacements.items():
        body = body.replace(key, value)
    return _strip_existing_event_wrappers(body)
