"""Shell support helpers: command lexing and subprocess output decoding."""

from __future__ import annotations

import os
import shlex

from src.core.process import decode_subprocess_output as decode_subprocess_output


def strip_shell_token(token: str) -> str:
    stripped = token.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def shell_lex_split(command: str) -> list[str]:
    """Split a shell command; Windows-safe for trailing backslashes."""
    stripped = command.strip()
    if not stripped:
        return []
    try:
        parts = shlex.split(stripped, posix=os.name != "nt")
    except ValueError:
        parts = stripped.split()
    return [strip_shell_token(part) for part in parts]


def is_agnes_video_poll_command(command: str) -> bool:
    """True when command runs legacy Agnes video create+poll or video-poll.

    Video generation now goes through ``media`` + ``VideoScheduler``;
    this helper remains for loop-detection against leftover shell poll scripts.
    """
    lower = command.lower()
    if "agnes_api.py" not in lower:
        return False
    if "video-poll" in lower:
        return True
    return "video" in lower and "--poll" in lower
