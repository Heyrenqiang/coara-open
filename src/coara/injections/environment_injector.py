"""Environment context helpers + thin seed entry (delegates to context_modules).

Prefix modules (user_rules / environment / AGENTS.md / ws.md) are built by
``context_modules.build_prefix_seed_messages``. This module keeps shared
constants, git snapshot, ws.md loader, and legacy combined-seed splice.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.core.types import Message
from src.utils.win_proc import no_window_creationflags

# Stable prefixes (also re-exported from context_modules for callers).
ENV_CONTEXT_PREFIX = "环境上下文：\n"
WS_PROTOCOL_PREFIX = "工作空间概况：\n"
WS_OVERVIEW_PLACEHOLDER = "本空间工作空间概况生成中，可用工具自行探索了解本空间"
_WS_MD_MAX_BYTES = 32 * 1024
_GIT_STATUS_MAX_LINES = 12
_GIT_CMD_TIMEOUT_S = 3.0


def load_workspace_protocol(workspace_dir: Path | str) -> str:
    """Load ``<workspace_dir>/.coara/ws.md`` (workspace overview), capped at 32KB.

    Returns empty string when the file is missing, unreadable, or blank.
    Truncated content keeps the last full line so the overview never ends mid-sentence.
    """
    path = Path(workspace_dir) / ".coara" / "ws.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if not raw.strip():
        return ""
    if len(raw.encode("utf-8")) > _WS_MD_MAX_BYTES:
        raw = raw[:_WS_MD_MAX_BYTES]
        cutoff = raw.rfind("\n")
        if cutoff > 0:
            raw = raw[:cutoff]
        raw += "\n…（工作空间概况超出上限，已截断）"
    return raw.strip()


def git_status_snapshot(workspace_dir: Path | str) -> str:
    """Compact ``git status -sb`` snapshot for the workspace (or a short fallback)."""
    cwd = Path(workspace_dir)
    git_cmd = ["git", "-C", str(cwd)]

    try:
        inside = subprocess.run(
            [*git_cmd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_GIT_CMD_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            creationflags=no_window_creationflags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "git 不可用"

    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "不是 git 仓库"

    try:
        status = subprocess.run(
            [*git_cmd, "status", "-sb"],
            capture_output=True,
            text=True,
            timeout=_GIT_CMD_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            creationflags=no_window_creationflags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "git 不可用"

    if status.returncode != 0:
        stderr = (status.stderr or "").strip()
        return f"git status 失败{(': ' + stderr) if stderr else ''}"

    text = status.stdout.strip()
    if not text:
        return "（干净工作区）"

    lines = text.splitlines()
    if len(lines) <= _GIT_STATUS_MAX_LINES:
        return text

    head = "\n".join(lines[:_GIT_STATUS_MAX_LINES])
    return f"{head}\n…（共 {len(lines)} 行，已截断）"


def build_environment_seed_messages(
    workspace_dir: Path | str,
    *,
    include_git: bool = False,
) -> list[Message]:
    """Build prefix context-module messages for a fresh session (may be multiple)."""
    from src.coara.injections.context_modules import build_prefix_seed_messages

    return build_prefix_seed_messages(workspace_dir, include_git=include_git)


def workspace_overview_block(workspace_dir: Path | str) -> str:
    """Overview text for the ws module: ws.md content, or the placeholder."""
    return load_workspace_protocol(workspace_dir) or WS_OVERVIEW_PLACEHOLDER


def replace_overview_in_seed(content: str, workspace_dir: Path | str) -> str | None:
    """Swap only the 工作空间概况 block inside a *legacy* combined env+ws seed.

    Returns the updated content, or None when *content* is not a combined seed or
    the overview block is already current. Prefer
    ``context_modules.replace_ws_overview_in_history`` for live sessions.
    """
    if ENV_CONTEXT_PREFIX not in content:
        return None
    idx = content.find(WS_PROTOCOL_PREFIX)
    if idx < 0:
        return None
    tail = "\n</系统消息>" if content.endswith("\n</系统消息>") else ""
    new_content = content[:idx] + WS_PROTOCOL_PREFIX + workspace_overview_block(workspace_dir) + tail
    if new_content == content:
        return None
    return new_content
