"""Bind mutating file tools to an on-disk baseline for the approval window.

P0-13: approval previews (and the user's decision) are based on file content at
prompt time. If the file changes before execute, applying the approved edit can
write something the user never saw. Capture a content token at confirm time;
refuse execute when the token no longer matches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

_BASELINE_ATTR = "_approval_file_baseline"


class ApprovalFileDriftError(RuntimeError):
    """Target file changed after the approval prompt was shown."""


def file_content_token(path: Path) -> str:
    """Stable fingerprint: size + sha256 of file bytes."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return f"{len(data)}:{digest}"


def _resolve_existing_target(invocation: Any) -> Path | None:
    """Best-effort absolute path for edit/write/delete when the target exists."""
    raw = getattr(invocation, "path", None)
    if raw is None and isinstance(getattr(invocation, "params", None), dict):
        raw = invocation.params.get("path")
    if not raw:
        return None
    try:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            return None
        path = path.resolve()
    except OSError:
        return None
    if not path.is_file():
        return None
    return path


def capture_approval_file_baseline(invocation: Any) -> dict[str, str] | None:
    """Snapshot the target file before the user is prompted; stash on *invocation*."""
    path = _resolve_existing_target(invocation)
    if path is None:
        return None
    try:
        token = file_content_token(path)
    except OSError:
        return None
    baseline = {"path": str(path), "token": token}
    setattr(invocation, _BASELINE_ATTR, baseline)
    return baseline


def verify_approval_file_baseline(invocation: Any) -> str | None:
    """Return an error message if the baseline is present and no longer matches.

    No baseline (new file / tool without path) → ``None`` (allow).
    """
    baseline = getattr(invocation, _BASELINE_ATTR, None)
    if not isinstance(baseline, dict):
        return None
    path_s = str(baseline.get("path") or "")
    expected = str(baseline.get("token") or "")
    if not path_s or not expected:
        return None
    path = Path(path_s)
    if not path.is_file():
        return "文件在审批等待期间已被删除或移动，已取消执行；请重试"
    try:
        current = file_content_token(path)
    except OSError:
        return "无法校验审批期间的文件状态，已取消执行；请重试"
    if current != expected:
        return "文件在审批等待期间已被修改，已取消执行；请重试"
    return None
