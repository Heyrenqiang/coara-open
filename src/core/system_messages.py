"""全局系统消息 store（区别于工作空间动态 updates）。

承载「API key 缺失」等全局系统级提醒：持久化在
``<coara_home>/system/messages.jsonl``（append-only），``/message`` 命令读取历史。

与 updates 的分工：
- updates 是「某工作空间的动态」，有红点/未读/呈阅语义，按空间隔离。
- 系统消息是「全局的、面向用户的环境提示」，跨空间，无红点语义，
  只在用户主动打开 ``/message`` 时呈现。
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

_MESSAGES_FILE = "messages.jsonl"
# 历史消息滚动上限：只保留最近 N 条，防无限膨胀。
_MAX_MESSAGES = 500

_lock = threading.Lock()


def _messages_path(coara_home: str | Path) -> Path:
    return Path(coara_home) / "system" / _MESSAGES_FILE


def _read_messages(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    messages: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return messages


def add_system_message(
    coara_home: str | Path,
    *,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str | None = None,
) -> bool:
    """Append a system message; return False when deduped (same key already present).

    ``dedupe_key`` 非空时，若历史里已有同 key 消息则跳过写入——用于
    「状态未变不重复提醒」的兜底（audit 层已做快照对比，这里是二次防线）。
    """
    path = _messages_path(coara_home)
    with _lock:
        messages = _read_messages(path)
        if dedupe_key:
            for existing in messages:
                if existing.get("dedupe_key") == dedupe_key:
                    return False
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "kind": kind,
            "title": title,
            "body": body,
            "dedupe_key": dedupe_key,
        }
        messages.append(record)
        _write_messages(path, messages)
    return True


def list_system_messages(coara_home: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent messages, newest last (chronological)."""
    path = _messages_path(coara_home)
    with _lock:
        messages = _read_messages(path)
    return messages[-limit:] if limit > 0 else messages


def _write_messages(path: Path, messages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(messages) > _MAX_MESSAGES:
        messages = messages[-_MAX_MESSAGES:]
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _now_iso() -> str:
    from src.core.time import now_iso

    return now_iso()
