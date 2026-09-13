"""Subagent instance persistence (SubagentStore).

Persist subagent state under coara Home so workspace trees stay clean.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths
from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.types import Message, MessageRole


@dataclass
class SubagentRecord:
    """Serialized state of a subagent instance."""

    agent_id: str
    subagent_type: str
    description: str
    message_history: list[dict[str, Any]]
    status: str
    created_at: str
    updated_at: str
    session_id: str
    child_coara_id: str
    parent_coara_id: str | None = None
    mode: str = "foreground"  # "foreground" | "background"
    result_preview: str = ""
    error: str | None = None
    # 取消/失败时未消化的接续输入（如主会话途中消息），resume 时重新入队
    pending_inputs: list[str] = field(default_factory=list)


def subagent_store_for_workspace(
    workspace_dir: Path | str,
    *,
    coara_home: Path | str | None = None,
) -> SubagentStore:
    """Build a store scoped to one workspace under coara Home."""
    return SubagentStore(workspace_dir, coara_home=coara_home)


class SubagentStore:
    """Persist subagent instance state for trace, CLI, and task bookkeeping.

    Storage layout: ``<coara_home>/workspaces/<workspace_id>/subagents/{agent_id}.json``
    """

    _MAX_RECORDS = 500
    # Prune only when the record count exceeds the cap by this margin, then
    # trim back to _MAX_RECORDS. Avoids a full directory rescan (with content
    # reads for the idle-first sort) on every save once the cap is reached.
    _PRUNE_MARGIN = 50

    def __init__(
        self,
        workspace_dir: Path | str | None = None,
        *,
        coara_home: Path | str | None = None,
    ) -> None:
        if workspace_dir is None:
            workspace_dir = Path.cwd()
        paths = CoaraHomePaths.for_workspace(workspace_dir, coara_home)
        self._workspace_dir = paths.workspace_dir
        self._coara_home = paths.root
        self._storage_dir = paths.subagents_dir
        # 目录惰性创建：启动全空间对账只读不写，若在此 mkdir 会给从未跑过
        # 子智能体的空间留下空 subagents/；首次写入时 write_json_atomic 自建父目录

    def _path_for(self, agent_id: str) -> Path:
        return self._storage_dir / f"{agent_id}.json"

    def save(self, record: SubagentRecord) -> None:
        """Persist a subagent record to disk (upsert)."""
        path = self._path_for(record.agent_id)
        try:
            write_json_atomic(path, asdict(record))
            logger.debug(f"SubagentStore saved {record.agent_id} ({record.status})")
        except Exception as exc:
            logger.warning(f"SubagentStore failed to save {record.agent_id}: {exc}", exc_info=True)
        self._prune_records()

    def load(self, agent_id: str) -> SubagentRecord | None:
        """Load a subagent record from disk."""
        path = self._path_for(agent_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SubagentRecord(**data)
        except Exception as exc:
            logger.warning(f"SubagentStore failed to load {agent_id}: {exc}")
            return None

    def delete(self, agent_id: str) -> bool:
        """Remove a persisted subagent record."""
        path = self._path_for(agent_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.debug(f"SubagentStore deleted {agent_id}")
            return True
        except Exception as exc:
            logger.warning(f"SubagentStore failed to delete {agent_id}: {exc}")
            return False

    def list_all(self) -> list[SubagentRecord]:
        """List all persisted subagent records."""
        by_id: dict[str, SubagentRecord] = {}
        if not self._storage_dir.is_dir():
            return []
        for path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = SubagentRecord(**data)
                by_id[record.agent_id] = record
            except Exception as exc:
                logger.warning(f"SubagentStore skipped corrupt file {path.name}: {exc}")
                with suppress(OSError):
                    path.unlink()
        return list(by_id.values())

    def recover_stale_running(self) -> int:
        """Mark ``running_*`` records as ``failed`` after a process restart.

        A kill/power-loss leaves records stuck in a running state forever,
        while ``delegate(action=resume)`` only accepts cancelled/failed —
        converging them here makes the persisted breakpoints resumable again.

        回收的子智能体写入本空间的重启清算通知（系统自恢复角色 janitor/daily 除外，
        它们会自行重跑，不污染注记），下次该空间会话恢复时注入历史。
        """
        from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES
        from src.core.time import now_iso
        from src.core.types import SubagentStatus

        stale_statuses = {
            SubagentStatus.RUNNING_FOREGROUND.value,
            SubagentStatus.RUNNING_BACKGROUND.value,
        }
        recovered = 0
        notice_items: list[str] = []
        for record in self.list_all():
            if record.status not in stale_statuses:
                continue
            record.status = SubagentStatus.FAILED.value
            record.error = "Process restarted while subagent was running"
            record.updated_at = now_iso()
            self.save(record)
            recovered += 1
            logger.info(f"Recovered stale running subagent record: {record.agent_id}")
            if record.subagent_type in SYSTEM_ONLY_SUBAGENT_TYPES:
                continue
            desc = (record.description or "").strip()
            if len(desc) > 60:
                desc = desc[:60] + "…"
            notice_items.append(
                f"后台子智能体 {record.agent_id}（{record.subagent_type}）：{desc}"
                if desc
                else f"后台子智能体 {record.agent_id}（{record.subagent_type}）"
            )
        if notice_items:
            try:
                from src.coara.workspace_state import append_restart_notice

                append_restart_notice(self._workspace_dir, notice_items, coara_home=self._coara_home)
            except Exception as exc:
                logger.warning(f"Failed to write restart notice for {self._workspace_dir}: {exc}")
        return recovered

    def _prune_records(self) -> None:
        """Limit persisted records; evict oldest idle files first."""
        paths = list(self._storage_dir.glob("*.json"))
        if len(paths) <= self._MAX_RECORDS + self._PRUNE_MARGIN:
            return

        def _sort_key(path: Path) -> tuple[int, float]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                is_idle = 0 if data.get("status") == "idle" else 1
                return (is_idle, path.stat().st_mtime)
            except Exception:
                return (-1, 0.0)

        paths.sort(key=_sort_key)
        for path in paths[: len(paths) - self._MAX_RECORDS]:
            try:
                path.unlink()
                logger.debug(f"SubagentStore pruned {path.name}")
            except Exception as exc:
                logger.warning(f"SubagentStore failed to prune {path.name}: {exc}")

    @staticmethod
    def serialize_message_history(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert Message objects to plain dicts for JSON serialization."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {
                "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                "content": msg.content,
            }
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.name:
                entry["name"] = msg.name
            if msg.reasoning_content:
                entry["reasoning_content"] = msg.reasoning_content
            if msg.provider_wire_blocks:
                entry["provider_wire_blocks"] = msg.provider_wire_blocks
            result.append(entry)
        return result

    @staticmethod
    def deserialize_message_history(data: list[dict[str, Any]]) -> list[Message]:
        """Convert plain dicts back to Message objects."""
        result: list[Message] = []
        for entry in data:
            role_str = entry.get("role", "user")
            try:
                role = MessageRole(role_str)
            except ValueError:
                role = MessageRole.USER

            tool_calls = None
            raw_tool_calls = entry.get("tool_calls")
            if raw_tool_calls:
                from src.core.types import ToolCall

                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]) for tc in raw_tool_calls
                ]

            result.append(
                Message(
                    role=role,
                    content=entry.get("content") or "",
                    tool_calls=tool_calls,
                    tool_call_id=entry.get("tool_call_id"),
                    name=entry.get("name"),
                    reasoning_content=entry.get("reasoning_content"),
                    provider_wire_blocks=entry.get("provider_wire_blocks"),
                )
            )
        return result
