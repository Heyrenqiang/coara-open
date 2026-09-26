"""@mention routing for multi-agent Matrix rooms"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class AgentDescriptor:
    """Describes a known agent in the shared room."""

    name: str  # localpart, e.g. "coara"
    user_id: str  # full MXID, e.g. "@coara:server"
    display_name: str = ""
    is_default: bool = False


@dataclass(slots=True)
class MentionRoute:
    """Result of parsing a message for @mention routing."""

    should_process: bool
    body: str  # mention-stripped body if should_process, original otherwise


# Pattern: @name or @name:server at start of message.
_MENTION_RE = re.compile(r"^@([a-z0-9._=/+-]+)(?::[a-zA-Z0-9.\-_]+)?\s*")


def parse_mention(body: str) -> str | None:
    """Extract the @mentioned agent localpart from the message body"""
    if not body:
        return None
    m = _MENTION_RE.match(body)
    if m is None:
        return None
    return m.group(1).lower()


def should_process(
    body: str,
    bot_localpart: str,
    known_agents: list[AgentDescriptor],
) -> MentionRoute:
    """Decide whether this bot should process the message"""
    mention = parse_mention(body)
    if mention is None:
        # No @mention — only the default agent processes.
        for agent in known_agents:
            if agent.name.lower() == bot_localpart.lower():
                if agent.is_default:
                    return MentionRoute(should_process=True, body=body)
                return MentionRoute(should_process=False, body=body)
        # every unmentioned message in a multi-agent room.
        return MentionRoute(should_process=False, body=body)

    # Has @mention — check if it matches us.
    if mention == bot_localpart.lower():
        return MentionRoute(
            should_process=True,
            body=strip_mention(body),
        )

    # @mention targets another agent — skip.
    return MentionRoute(should_process=False, body=body)


def strip_mention(body: str) -> str:
    """Remove the leading @mention prefix from the message body."""
    if not body:
        return body
    return _MENTION_RE.sub("", body, count=1)


def localpart_from_user_id(user_id: str) -> str:
    """Extract the localpart from a Matrix user ID.

    ``@coara:server.local`` → ``coara``
    """
    if not user_id:
        return ""
    s = user_id
    if s.startswith("@"):
        s = s[1:]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s.lower()
