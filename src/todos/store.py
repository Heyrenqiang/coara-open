"""Persistent todo store for session-scoped self-managed execution lists."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.time import now_iso
from src.todos.types import TodoItem, TodoState, TodoStatus, is_allowed_status_transition


class TodoStoreError(RuntimeError):
    """Todo persistence or validation failure."""


class TodoStore:
    """Session-scoped todo persistence."""

    def __init__(self, *, workspace_dir: Path | None, session_id: str):
        base_dir = Path(workspace_dir) if workspace_dir else Path.home()
        self.storage_dir = base_dir / ".coara" / "todos"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.storage_path = self.storage_dir / f"{session_id}.json"
        self.session_id = session_id
        self._state = TodoState(session_id=session_id, updated_at=now_iso(), todos=[])
        self._corrupted = False
        self._load()

    def get_all(self) -> list[TodoItem]:
        return list(self._state.todos)

    def merge_updates(self, updates: list[TodoItem]) -> list[TodoItem]:
        current = {todo.id: todo for todo in self._state.todos}
        order = [todo.id for todo in self._state.todos]

        for todo in updates:
            if todo.id not in current:
                order.append(todo.id)
            else:
                self._warn_status_transition(current[todo.id].status, todo.status, todo.id)
            current[todo.id] = todo

        merged = [current[todo_id] for todo_id in order]
        self._state = TodoState(
            session_id=self.session_id,
            updated_at=now_iso(),
            todos=merged,
        )
        self._save()
        return self.get_all()

    def replace_all(self, items: list[TodoItem]) -> list[TodoItem]:
        """Replace the entire todo list (order = *items* order).

        终态自动清理：整表全为 completed/failed 时视为本批任务完结，直接清空
        （持久化为空表），避免已完成项无限堆积越加越长。空数组显式清空不变。
        """
        previous = {todo.id: todo for todo in self._state.todos}
        for todo in items:
            old = previous.get(todo.id)
            if old is not None:
                self._warn_status_transition(old.status, todo.status, todo.id)
        if items and all(todo.status in (TodoStatus.COMPLETED, TodoStatus.FAILED) for todo in items):
            items = []
        self._state = TodoState(
            session_id=self.session_id,
            updated_at=now_iso(),
            todos=list(items),
        )
        self._save()
        return self.get_all()

    def remove_ids(self, ids: list[str]) -> list[TodoItem]:
        drop = set(ids)
        self._state = TodoState(
            session_id=self.session_id,
            updated_at=now_iso(),
            todos=[todo for todo in self._state.todos if todo.id not in drop],
        )
        self._save()
        return self.get_all()

    def clear(self) -> None:
        self._state = TodoState(session_id=self.session_id, updated_at=now_iso(), todos=[])
        self._save()

    def _warn_status_transition(self, old: TodoStatus, new: TodoStatus, todo_id: str) -> None:
        if old == new or is_allowed_status_transition(old, new):
            return
        logger.warning(f"Todo {todo_id}: unusual status transition {old.value} -> {new.value} (allowed, logged only)")

    def _load(self) -> None:
        if not self.storage_path.exists():
            return

        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self._state = TodoState.from_dict(payload)
        except OSError as exc:
            # 瞬时 OS 错误（文件锁/杀软）不误伤健康文件
            logger.warning(f"Failed to read todo store from {self.storage_path}: {exc}")
        except Exception as exc:
            # 内容不可解析：隔离原文件并拒绝后续写入，防止空状态静默覆盖既有待办
            logger.warning(f"Failed to load todo store from {self.storage_path}: {exc}")
            self._quarantine_corrupt_file()

    def _quarantine_corrupt_file(self) -> None:
        """Rename the unreadable file aside and block further saves on this instance."""
        self._corrupted = True
        corrupt_path = self.storage_path.with_name(f"{self.storage_path.name}.corrupt")
        try:
            os.replace(self.storage_path, corrupt_path)
        except OSError as exc:
            logger.warning(f"Failed to quarantine corrupt todo store {self.storage_path}: {exc}")

    def _save(self) -> None:
        if self._corrupted:
            raise TodoStoreError(
                f"Todo store was corrupt at load ({self.storage_path.name}.corrupt kept); "
                "refusing to overwrite — fix or remove the .corrupt file and restart"
            )
        try:
            write_json_atomic(self.storage_path, self._state.to_dict())
        except Exception as exc:
            logger.error(f"Failed to save todo store to {self.storage_path}: {exc}")
            raise TodoStoreError(f"Failed to save todo store: {exc}") from exc
