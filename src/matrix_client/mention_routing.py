"""@mention routing for multi-agent Matrix rooms.

When multiple agents share a Matrix room, messages are routed by
@mention prefix:

- ``@coara 帮我写代码``  → coara processes (mention stripped)
- ``@somebot 分析代码``  → somebot processes (mention stripped)
- ``帮我写代码``          → default agent processes (coara by default)

Each bot calls :func:`should_process` to decide whether to handle a message,
and :func:`strip_mention` to remove the @mention prefix before forwarding
to the agent runtime.
"""

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
# Localpart follows the Matrix user-ID grammar (lowercase a-z, 0-9, . _ = - / +);
# bot usernames in this repo (e.g. "coara", "gora") are always lowercase.
_MENTION_RE = re.compile(r"^@([a-z0-9._=/+-]+)(?::[a-zA-Z0-9.\-_]+)?\s*")


def parse_mention(body: str) -> str | None:
    """Extract the @mentioned agent localpart from the message body.

    Returns the localpart (e.g. "coara") if the message starts with @name,
    or None if there is no @mention prefix.
    """
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
    """Decide whether this bot should process the message.

    Rules:
    - If body starts with @<bot_localpart> → process (strip mention)
    - If body starts with @<other_agent> → skip (another agent handles it)
    - If no @mention → process only if this bot is the default agent
    """
    mention = parse_mention(body)
    if mention is None:
        # No @mention — only the default agent processes.
        for agent in known_agents:
            if agent.name.lower() == bot_localpart.lower():
                if agent.is_default:
                    return MentionRoute(should_process=True, body=body)
                return MentionRoute(should_process=False, body=body)
        # If we can't find ourselves in the discovered agent list, the bot's
        # localpart does not match the server config — stay silent (a loud
        # warning is logged at discovery time) rather than double-replying to
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
