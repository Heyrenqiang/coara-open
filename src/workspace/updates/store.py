"""Filesystem-backed per-workspace updates store for inbound events."""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

from src.core.coara_home import user_paths
from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.time import now_iso
from src.utils.safe_id import safe_storage_key
from src.workspace.updates.display import (
    event_payload_body,
    format_update_display_text,
    format_update_display_title,
)
from src.workspace.updates.rules import NOTE_MAX_CHARS
from src.workspace.updates.types import (
    DISPOSITIONS,
    HANDLE_MODES,
    SALIENCE_LEVELS,
    ReviewEntry,
    WorkspaceUpdate,
)

UpdateFilter = Literal["unread", "read", "archived", "all"]

# 纯规则过目：low 显著未读消息超过该天数自动勾掉（dismissed，留轨迹）
LOW_SALIENCE_MAX_AGE_DAYS = 7


def _normalize_review_note(note: str) -> str:
    """规范化处置备注：去首尾空白与控制字符，截断到 NOTE_MAX_CHARS。"""
    cleaned = "".join(ch for ch in (note or "") if ch.isprintable() or ch in "\n\t")
    cleaned = cleaned.strip()
    if len(cleaned) > NOTE_MAX_CHARS:
        cleaned = cleaned[:NOTE_MAX_CHARS]
    return cleaned


class WorkspaceUpdatesStore:
    """Persist inbound events per workspace.

    新布局（有 registry）：``<workspace_dir>/.coara/inbox/*.json``，与 ws.md 同级；
    已读水位线 ``<workspace_dir>/.coara/inbox/read_marker.json``。
    旧布局（无 registry，兼容测试/旧调用）：``<coara_home>/users/default/inbox/{workspace}/``
    + 同级 ``{workspace}.read_marker.json``。未读数 = ``created_at`` 晚于水位线的条目数。
    """

    _ROOT_DIR = "inbox"
    _MAX_MESSAGES_PER_WORKSPACE = 500

    def __init__(self, coara_home: Path, registry: Any | None = None) -> None:
        home = Path(coara_home).expanduser().resolve()
        self._root = user_paths(home).inbox_dir
        self._root.mkdir(parents=True, exist_ok=True)
        # workspace -> known non-archived dedupe keys (lazy-loaded)
        self._dedupe_keys: dict[str, set[str]] = {}
        # 可选 registry：提供 name→目录解析 + 全部工作空间遍历（空间自治布局）
        self._registry = registry

    def _resolve_entry(self, workspace: str) -> Any | None:
        if self._registry is None:
            return None
        try:
            return self._registry.resolve_name_or_id(workspace)
        except Exception:
            return None

    def _workspace_inbox_path(self, workspace: str) -> Path:
        """解析空间动态目录（不创建）。有 registry 走新布局，否则旧布局。"""
        entry = self._resolve_entry(workspace)
        if entry is not None:
            return entry.resolved_path() / ".coara" / "inbox"
        return self._root / safe_storage_key(workspace)

    def _workspace_dir(self, workspace: str) -> Path:
        path = self._workspace_inbox_path(workspace)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _all_inbox_dirs(self) -> list[Path]:
        """跨空间遍历用目录列表：registry 布局扫各空间 .coara/inbox，旧布局扫 inbox_root 子目录。"""
        if self._registry is not None:
            dirs: list[Path] = []
            try:
                for entry in self._registry.list_active():
                    inbox = entry.resolved_path() / ".coara" / "inbox"
                    if inbox.is_dir():
                        dirs.append(inbox)
            except Exception:
                return dirs
            return dirs
        if not self._root.is_dir():
            return []
        return [p for p in self._root.iterdir() if p.is_dir()]

    def _message_path(self, workspace: str, message_id: str) -> Path:
        return self._workspace_dir(workspace) / f"{safe_storage_key(message_id)}.json"

    def _marker_path(self, workspace: str) -> Path:
        entry = self._resolve_entry(workspace)
        if entry is not None:
            return entry.resolved_path() / ".coara" / "inbox" / "read_marker.json"
        return self._root / f"{safe_storage_key(workspace)}.read_marker.json"

    def read_marker(self, workspace: str) -> str | None:
        """已读水位线（ISO 时间戳）；无水位记录时返回 None。"""
        path = self._marker_path(workspace)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        marker = data.get("marker")
        return str(marker) if marker else None

    def unread_count(self, workspace: str) -> int:
        """未归档、未被勾掉/处置且 created_at 晚于已读水位线的条目数（红点语义）。"""
        marker = self.read_marker(workspace)
        count = 0
        for msg in self.list_messages(workspace=workspace, status="all", limit=1000):
            if msg.status == "archived":
                continue
            if msg.disposition in ("dismissed", "resolved"):
                continue
            if marker is not None and msg.created_at <= marker:
                continue
            count += 1
        return count

    def mark_all_read(self, workspace: str) -> None:
        """推进已读水位线到该工作空间最新条目时间。"""
        messages = self.list_messages(workspace=workspace, status="all", limit=1)
        marker = messages[0].created_at if messages else now_iso()
        write_json_atomic(
            self._marker_path(workspace),
            {"workspace": workspace, "marker": marker, "updated_at": now_iso()},
        )

    def latest(self, workspace: str) -> WorkspaceUpdate | None:
        messages = self.list_messages(workspace=workspace, status="all", limit=1)
        return messages[0] if messages else None

    @staticmethod
    def make_title(*, event_type: str, payload: dict[str, Any], text: str) -> str:
        for key in ("content", "title", "summary", "subject"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                line = value.strip().replace("\n", " ")
                return line[:120] + ("…" if len(line) > 120 else "")
        feedback_id = payload.get("feedback_id")
        if feedback_id is not None:
            return f"{event_type} · feedback #{feedback_id}"
        first = text.strip().splitlines()
        for line in first:
            stripped = line.strip()
            if stripped and not stripped.startswith("[") and "workspace:" not in stripped:
                return stripped[:120] + ("…" if len(stripped) > 120 else "")
        return event_type[:120]

    def _ensure_dedupe_index(self, workspace: str) -> set[str]:
        safe = safe_storage_key(workspace)
        cached = self._dedupe_keys.get(safe)
        if cached is not None:
            return cached
        keys: set[str] = set()
        for msg in self.list_messages(workspace=workspace, status="all", limit=1000):
            if msg.dedupe_key and msg.status != "archived":
                keys.add(msg.dedupe_key)
        self._dedupe_keys[safe] = keys
        return keys

    def append(
        self,
        *,
        workspace: str,
        source_id: str,
        event_type: str,
        dedupe_key: str,
        text: str,
        payload: dict[str, Any],
        type: str = "note",  # noqa: A002 — 与 WorkspaceUpdate 字段名保持一致
        payload_ref: str | None = None,
        salience: str = "normal",
        handle_mode: str = "park",
        source_kind: str = "",
        expires_at: str | None = None,
    ) -> WorkspaceUpdate | None:
        if dedupe_key in self._ensure_dedupe_index(workspace):
            logger.debug(f"Updates dedupe skip {workspace}: {dedupe_key}")
            return None

        stored_payload = payload if isinstance(payload, dict) else {}
        message = WorkspaceUpdate(
            message_id=f"upd-{uuid.uuid4().hex[:12]}",
            workspace=workspace,
            source_id=source_id,
            event_type=event_type,
            type=type,
            payload_ref=payload_ref,
            dedupe_key=dedupe_key,
            salience=salience if salience in SALIENCE_LEVELS else "normal",
            handle_mode=handle_mode if handle_mode in HANDLE_MODES else "park",
            source_kind=source_kind,
            expires_at=expires_at,
            title=format_update_display_title(
                payload=stored_payload,
                fallback=self.make_title(
                    event_type=event_type,
                    payload=event_payload_body(stored_payload),
                    text=text,
                ),
            ),
            text=text,
            display_text=format_update_display_text(payload=stored_payload, fallback_text=text),
            payload=stored_payload,
        )
        path = self._message_path(workspace, message.message_id)
        write_json_atomic(path, message.to_dict())
        self._ensure_dedupe_index(workspace).add(dedupe_key)
        self._prune_workspace(workspace)
        logger.info(f"Updates {workspace}: stored {message.message_id} ({message.title[:60]})")
        return message

    def get(self, message_id: str) -> WorkspaceUpdate | None:
        for directory in self._all_inbox_dirs():
            for path in directory.glob("*.json"):
                try:
                    msg = WorkspaceUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if msg.message_id == message_id:
                    return msg
        return None

    def list_workspaces(self) -> list[str]:
        names: list[str] = []
        if not self._root.is_dir():
            return names
        for child in sorted(self._root.iterdir()):
            if child.is_dir() and any(child.glob("*.json")) and child.name not in names:
                names.append(child.name)
        return names

    def list_messages(
        self,
        *,
        workspace: str | None = None,
        status: UpdateFilter = "unread",
        limit: int = 50,
    ) -> list[WorkspaceUpdate]:
        messages: list[WorkspaceUpdate] = []
        if workspace:
            inbox = self._workspace_inbox_path(workspace)
            dirs = [inbox] if inbox.is_dir() else []
        else:
            dirs = self._all_inbox_dirs()
        for directory in dirs:
            for path in directory.glob("*.json"):
                try:
                    msg = WorkspaceUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if status != "all" and msg.status != status:
                    continue
                messages.append(msg)
        messages.sort(key=lambda m: m.created_at, reverse=True)
        return messages[:limit]

    def pending(self, *, limit: int = 30) -> list[WorkspaceUpdate]:
        """前台待处理视图：未读且未被勾掉的高显著条目，以及被呈阅（elevated）的条目。"""
        messages = self.list_messages(status="unread", limit=1000)
        actionable = [
            m
            for m in messages
            if m.disposition not in ("dismissed", "resolved") and (m.salience == "high" or m.disposition == "elevated")
        ]
        return actionable[: max(1, limit)]

    def set_disposition(
        self,
        message_id: str,
        disposition: str,
        *,
        reviewed_by: str,
        note: str = "",
    ) -> WorkspaceUpdate | None:
        """写入处置轨迹：追加 review_history 并同步最近一次快捷字段。

        勾掉（dismissed）时顺带置为已读——不再占红点，但留轨迹可查。
        note 经规范化（截断到 NOTE_MAX_CHARS、去控制字符）；必填校验在工具层。
        """
        if disposition not in DISPOSITIONS:
            return None
        msg = self.get(message_id)
        if msg is None:
            return None
        reviewed_at = now_iso()
        clean_note = _normalize_review_note(note)
        msg.disposition = disposition
        msg.reviewed_by = reviewed_by
        msg.reviewed_at = reviewed_at
        msg.review_note = clean_note
        msg.review_history.append(ReviewEntry(by=reviewed_by, at=reviewed_at, action=disposition, note=clean_note))
        if disposition in ("dismissed", "resolved") and msg.status == "unread":
            msg.status = "read"
            msg.read_at = reviewed_at
        self._save(msg)
        return msg

    def set_salience(
        self,
        message_id: str,
        salience: str,
        *,
        by: str,
    ) -> WorkspaceUpdate | None:
        """重排优先级：写 salience 并记录修改者（review / /ws updates salience）。"""
        if salience not in SALIENCE_LEVELS:
            return None
        msg = self.get(message_id)
        if msg is None:
            return None
        msg.salience = salience
        msg.salience_by = by
        msg.salience_at = now_iso()
        self._save(msg)
        return msg

    def board(
        self,
        workspace: str | None = None,
        *,
        limit: int = 30,
    ) -> tuple[list[WorkspaceUpdate], list[WorkspaceUpdate]]:
        """过目单分组（janitor 视角）：(待处理, 已处置)。

        待处理 = disposition 仍为 pending；已处置 = elevated / resolved / dismissed（含呈阅）。
        归档条目不进过目单。按时间倒序，各取 limit 条。
        """
        messages = self.list_messages(workspace=workspace, status="all", limit=1000)
        pending = [m for m in messages if m.disposition == "pending" and m.status != "archived"]
        reviewed = [
            m for m in messages if m.disposition in ("elevated", "resolved", "dismissed") and m.status != "archived"
        ]
        pending.sort(key=lambda m: m.created_at, reverse=True)
        reviewed.sort(key=lambda m: m.reviewed_at or m.created_at, reverse=True)
        return pending[:limit], reviewed[:limit]

    def sweep(self, *, low_max_age_days: float = LOW_SALIENCE_MAX_AGE_DAYS) -> list[WorkspaceUpdate]:
        """纯规则过目（janitor 的系统职能，不经 LLM）：

        - 过了保质期（expires_at）的未读 pending → dismissed「已过保质期 自动忽略」
        - low 显著超龄未读 pending → dismissed「低显著超龄 自动忽略」

        返回本次被勾掉的条目（供推送红点状态）。
        """
        from datetime import datetime, timedelta

        from src.core.time import parse_iso_to_datetime

        # aware 本地时间：created/expires 历史上有 naive（now_iso）与 aware（外部写入）两种口径，
        # 统一 astimezone() 归一到 aware 再比较，避免混用抛 TypeError 中断整次 sweep
        now = datetime.now().astimezone()
        swept: list[WorkspaceUpdate] = []
        for msg in self.list_messages(status="unread", limit=5000):
            if msg.disposition != "pending":
                continue
            note = ""
            expires = parse_iso_to_datetime(msg.expires_at)
            if expires is not None:
                expires = expires.astimezone()  # naive 视为本地补 tz；aware 换算到本地
            if expires is not None and now >= expires:
                note = "已过保质期 自动忽略"
            elif msg.salience == "low":
                created = parse_iso_to_datetime(msg.created_at)
                if created is not None and now - created.astimezone() >= timedelta(days=low_max_age_days):
                    note = "低显著消息超龄 自动忽略"
            if not note:
                continue
            updated = self.set_disposition(msg.message_id, "dismissed", reviewed_by="janitor", note=note)
            if updated is not None:
                swept.append(updated)
        return swept

    def stats(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        if self._registry is not None:
            # 空间自治布局：只统计已登记 active 空间（alias 为键）
            try:
                entries = self._registry.list_active()
            except Exception:
                entries = []
            for entry in entries:
                alias = str(getattr(entry, "name", "") or "")
                if not alias:
                    continue
                inbox = entry.resolved_path() / ".coara" / "inbox"
                bucket = counts.setdefault(alias, {"unread": 0, "read": 0, "archived": 0, "total": 0})
                if not inbox.is_dir():
                    continue
                for path in inbox.glob("*.json"):
                    try:
                        msg = WorkspaceUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))
                    except Exception:
                        continue
                    bucket[msg.status] = bucket.get(msg.status, 0) + 1
                    bucket["total"] += 1
            return counts
        if not self._root.is_dir():
            return counts
        for directory in self._root.iterdir():
            if not directory.is_dir():
                continue
            alias = directory.name
            bucket = counts.setdefault(alias, {"unread": 0, "read": 0, "archived": 0, "total": 0})
            for path in directory.glob("*.json"):
                try:
                    msg = WorkspaceUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                bucket[msg.status] = bucket.get(msg.status, 0) + 1
                bucket["total"] += 1
        return counts

    def mark_read(self, message_id: str) -> WorkspaceUpdate | None:
        msg = self.get(message_id)
        if msg is None:
            return None
        if msg.status == "unread":
            msg.status = "read"
            msg.read_at = now_iso()
            self._save(msg)
        return msg

    def archive(self, message_id: str) -> WorkspaceUpdate | None:
        msg = self.get(message_id)
        if msg is None:
            return None
        msg.status = "archived"
        msg.archived_at = now_iso()
        if msg.read_at is None:
            msg.read_at = msg.archived_at
        self._save(msg)
        # Archived messages must not block same-key redelivery (event/workflow retries).
        if msg.dedupe_key:
            keys = self._dedupe_keys.get(safe_storage_key(msg.workspace))
            if keys is not None:
                keys.discard(msg.dedupe_key)
        return msg

    def migrate_legacy_inbox(self) -> None:
        """把旧布局 ``<coara_home>/users/default/inbox/{name}/`` 迁到各空间 ``.coara/inbox/``。

        需要 registry 才能解析 name → 工作空间目录；无 registry 时跳过。
        幂等：目标文件已存在则跳过；搬完删旧目录与旧 marker；失败只记日志。
        """
        if self._registry is None:
            return
        try:
            entries = self._registry.list_active()
        except Exception as exc:
            logger.warning(f"Inbox legacy migration: list_active failed: {exc}")
            return
        for entry in entries:
            name = str(getattr(entry, "name", "") or "")
            if not name:
                continue
            safe = safe_storage_key(name)
            old_dir = self._root / safe
            new_dir = entry.resolved_path() / ".coara" / "inbox"
            try:
                if old_dir.is_dir():
                    new_dir.mkdir(parents=True, exist_ok=True)
                    moved = 0
                    for path in sorted(old_dir.glob("*.json")):
                        target = new_dir / path.name
                        if target.exists():
                            continue
                        shutil.move(str(path), str(target))
                        moved += 1
                    if moved:
                        logger.info(f"Inbox legacy migration {name}: moved {moved} entries to {new_dir}")
                    with contextlib.suppress(OSError):
                        old_dir.rmdir()  # 搬完（或已空）删旧目录
                old_marker = self._root / f"{safe}.read_marker.json"
                new_marker = new_dir / "read_marker.json"
                if old_marker.is_file() and not new_marker.exists():
                    new_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_marker), str(new_marker))
                    logger.info(f"Inbox legacy migration {name}: moved read_marker")
            except OSError as exc:
                logger.warning(f"Inbox legacy migration failed for {name}: {exc}")

    def _save(self, msg: WorkspaceUpdate) -> None:
        path = self._message_path(msg.workspace, msg.message_id)
        write_json_atomic(path, msg.to_dict())

    def _prune_workspace(self, workspace: str) -> None:
        directory = self._workspace_dir(workspace)
        files = list(directory.glob("*.json"))
        if len(files) <= self._MAX_MESSAGES_PER_WORKSPACE:
            return
        messages: list[tuple[Path, WorkspaceUpdate]] = []
        for path in files:
            try:
                messages.append((path, WorkspaceUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))))
            except Exception:
                continue
        messages.sort(key=lambda pair: pair[1].created_at)
        removed_keys: set[str] = set()
        to_remove = len(messages) - self._MAX_MESSAGES_PER_WORKSPACE
        for path, msg in messages:
            if to_remove <= 0:
                break
            if msg.status == "archived":
                path.unlink(missing_ok=True)
                if msg.dedupe_key:
                    removed_keys.add(msg.dedupe_key)
                to_remove -= 1
        if to_remove > 0:
            # Second round: oldest first, but never delete unread messages —
            # dropping an unread event would silently lose it. Soft cap may be
            # exceeded while unread piles up; that is intentional (P0-3).
            for path, msg in messages:
                if to_remove <= 0:
                    break
                if msg.status == "unread":
                    continue
                if not path.exists():
                    continue
                path.unlink(missing_ok=True)
                if msg.dedupe_key:
                    removed_keys.add(msg.dedupe_key)
                to_remove -= 1
        if to_remove > 0:
            logger.warning(
                "Updates {}: soft cap {} exceeded by {} unread-only surplus; "
                "not deleting unread — clear via review/janitor",
                workspace,
                self._MAX_MESSAGES_PER_WORKSPACE,
                to_remove,
            )
        if removed_keys:
            keys = self._dedupe_keys.get(safe_storage_key(workspace))
            if keys is not None:
                keys -= removed_keys
