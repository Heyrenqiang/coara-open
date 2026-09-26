"""Workflow persistence layer using SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from wdl.errors import WDLError as CoaraError
from wdl.logging import logger
from wdl.time import now_iso
from wdl.types import WorkflowInstanceRecord, WorkflowInstanceState


class WorkflowOwnershipLost(CoaraError):  # noqa: N818
    """实例 owner 守卫失配：该实例归另一引擎进程，本进程不得推进

    Raised by fenced writes (persistence created with ``owner_id``) when the
    instance row is owned by a different engine token. Callers must abort the
    local run quietly — no failure events, no state overwrite
    """


class WorkflowPersistence:
    """SQLite-backed workflow state persistence for crash recovery."""

    def __init__(self, db_path: str | Path | None = None, *, owner_id: str | None = None):
        if db_path is None:
            db_path = ".coara/workflows/instances.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        # 串行化建连：并发 _ensure_db 会各建一条连接，落败者的连接线程泄漏
        self._db_lock = asyncio.Lock()
        # 引擎 fencing 令牌（wfengine-<pid>-<随机串>）；None 表示不走 fencing
        # （bridge / CLI / Web UI 等协调方连接不认领实例）
        self.owner_id = owner_id

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._db_lock:
                if self._db is None:
                    db = aiosqlite.connect(self.db_path)
                    try:
                        await db
                        db.row_factory = aiosqlite.Row
                        await db.execute("PRAGMA busy_timeout = 5000")
                        # WAL: readers don't block the writer; workflow engine
                        # emits many small writes interleaved with status reads.
                        await db.execute("PRAGMA journal_mode = WAL")
                        await self._init_schema(db)
                    except BaseException:
                        # 取消打在 connect/建表中途时连接线程已经 start：aiosqlite 只兜
                        # Exception，兜不住 CancelledError（BaseException），线程非守护会
                        # 永远空转拖住进程退出。任何失败都必须收口线程
                        with contextlib.suppress(Exception):
                            await db.close()
                        raise
                    self._db = db
        return self._db

    async def _write(self, *statements: tuple[str, tuple[Any, ...]]) -> aiosqlite.Cursor:
        """execute + commit；中途任何失败都回滚，不留半事务

        aiosqlite 已入队的语句即使 await 侧被取消也仍会在连接线程里执行；
        不回滚的话，共享连接上遗留的未提交事务会被下一次 commit 静默捎带。
        多语句批次普通异常同样会在已执行的语句上留下半事务，故统一回滚
        """
        db = await self._ensure_db()
        cursor: aiosqlite.Cursor | None = None
        try:
            for sql, params in statements:
                cursor = await db.execute(sql, params)
            await db.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                await db.rollback()
            raise
        assert cursor is not None  # statements 至少一条
        return cursor

    @staticmethod
    async def _init_schema(db: aiosqlite.Connection) -> None:
        """WDL-only schema."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_instances (
                instance_id TEXT PRIMARY KEY,
                name TEXT,
                wdl_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                inputs_json TEXT NOT NULL DEFAULT '{}',
                current_step_id TEXT,
                context_json TEXT NOT NULL DEFAULT '{}',
                wake_at TIMESTAMP,
                wait_event TEXT,
                wait_timeout TIMESTAMP,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_wake
            ON workflow_instances(status, wake_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_event
            ON workflow_instances(status, wait_event)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_instances_archive (
                instance_id TEXT PRIMARY KEY,
                name TEXT,
                wdl_text TEXT NOT NULL,
                status TEXT NOT NULL,
                inputs_json TEXT NOT NULL,
                current_step_id TEXT,
                context_json TEXT NOT NULL,
                error TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Additive columns for structured results / origin tagging / engine
        # fencing ownership (idempotent).
        for col, decl in (
            ("result_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("origin_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("workspace", "TEXT"),
            ("owner_id", "TEXT"),
        ):
            with contextlib.suppress(Exception):  # already exists
                await db.execute(f"ALTER TABLE workflow_instances ADD COLUMN {col} {decl}")
        await db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @staticmethod
    def _dt_iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    @staticmethod
    def _parse_dt(val: Any) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            return datetime.fromisoformat(val)
        return None

    async def create_instance(
        self,
        instance_id: str,
        name: str,
        wdl_text: str,
        inputs: dict[str, Any],
    ) -> None:
        await self._write(
            (
                """
            INSERT INTO workflow_instances
            (instance_id, name, wdl_text, status, inputs_json, current_step_id, context_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    instance_id,
                    name,
                    wdl_text,
                    WorkflowInstanceState.PENDING.value,
                    json.dumps(inputs),
                    None,
                    json.dumps({"inputs": inputs}),
                ),
            )
        )
        logger.info(f"Workflow instance created: {instance_id}")

    async def save_instance_origin(
        self,
        instance_id: str,
        origin: dict[str, Any],
        *,
        workspace: str | None = None,
    ) -> None:
        """Persist submit origin (tool/web/event) and optional workspace alias."""
        await self._write(
            (
                """
            UPDATE workflow_instances
            SET origin_json=?, workspace=COALESCE(?, workspace), updated_at=?
            WHERE instance_id=?
            """,
                (json.dumps(origin or {}), workspace, now_iso(), instance_id),
            )
        )

    async def get_instance_origin(self, instance_id: str) -> dict[str, Any]:
        """Read back the persisted submit origin (``{}`` when missing)."""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT origin_json FROM workflow_instances WHERE instance_id=?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["origin_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def save_instance_result(self, instance_id: str, result: dict[str, Any]) -> None:
        """Persist structured WorkflowResultDoc projection."""
        await self._write(
            (
                """
            UPDATE workflow_instances
            SET result_json=?, updated_at=?
            WHERE instance_id=?
            """,
                (json.dumps(result or {}), now_iso(), instance_id),
            )
        )

    async def get_instance_result(self, instance_id: str) -> dict[str, Any] | None:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT result_json, origin_json, workspace, name, status, error"
            " FROM workflow_instances WHERE instance_id=?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            result = {}
        if not isinstance(result, dict) or not result:
            try:
                origin = json.loads(row["origin_json"] or "{}")
            except json.JSONDecodeError:
                origin = {}
            result = {
                "instance_id": instance_id,
                "name": row["name"] or "",
                "status": row["status"],
                "origin": origin if isinstance(origin, dict) else {},
                "final": {"summary": row["error"] or row["status"], "outputs": {}},
                "nodes": [],
                "error": row["error"],
            }
        result.setdefault("instance_id", instance_id)
        if row["workspace"] and isinstance(result.get("origin"), dict):
            result["origin"].setdefault("workspace", row["workspace"])
        return result

    async def save_state(
        self,
        instance_id: str,
        current_step_id: str | None,
        context: dict[str, Any] | None,
        status: WorkflowInstanceState | None = None,
    ) -> None:
        sets = ["current_step_id=?", "updated_at=?"]
        params: list[Any] = [current_step_id, now_iso()]
        if context is not None:
            sets.append("context_json=?")
            params.append(json.dumps(context))
        if status:
            sets.append("status=?")
            params.append(status.value)
        fenced = self.owner_id is not None
        if fenced and status is not None:
            # fencing：写 RUNNING 即认领实例，写其它状态即释放（终态/挂起谁都可接管）；
            # status=None 的纯进度写不动 owner_id 列，保持认领（否则下一次
            # fenced 写会因自己释放的认领失配而抛 WorkflowOwnershipLost 自锁）
            sets.append("owner_id=?")
            params.append(self.owner_id if status == WorkflowInstanceState.RUNNING else None)
        sql = f"UPDATE workflow_instances SET {', '.join(sets)} WHERE instance_id=?"
        params.append(instance_id)
        if fenced:
            # 在跑实例只有 owner 本人能推进；非在跑实例可被认领，但前任 owner
            # （recover 保留在 interrupted 行上的旧令牌）不得夺回——旧引擎孤儿
            # 未死透时它的下一次写入必然失配
            sql += " AND ((status = ? AND owner_id = ?) OR (status != ? AND (owner_id IS NULL OR owner_id != ?)))"
            running = WorkflowInstanceState.RUNNING.value
            params.extend((running, self.owner_id, running, self.owner_id))
        cursor = await self._write((sql, tuple(params)))
        if fenced and cursor.rowcount == 0:
            raise WorkflowOwnershipLost(
                f"Workflow instance {instance_id} is owned by another engine; this process must not advance it"
            )

    async def suspend(
        self,
        instance_id: str,
        status: WorkflowInstanceState,
        wake_at: datetime | None = None,
        wait_event: str | None = None,
        wait_timeout: datetime | None = None,
    ) -> bool:
        """挂起实例；fencing 失配（实例已被夺）时抛 WorkflowOwnershipLost"""
        sql = """
            UPDATE workflow_instances
            SET status=?, wake_at=?, wait_event=?, wait_timeout=?, updated_at=?
            """
        params: list[Any] = [
            status.value,
            self._dt_iso(wake_at),
            wait_event,
            self._dt_iso(wait_timeout),
            now_iso(),
        ]
        fenced = self.owner_id is not None
        if fenced:
            sql += ", owner_id=NULL"
        sql += " WHERE instance_id=?"
        params.append(instance_id)
        if fenced:
            sql += " AND (owner_id IS NULL OR owner_id=?)"
            params.append(self.owner_id)
        cursor = await self._write((sql, tuple(params)))
        if fenced and cursor.rowcount == 0:
            raise WorkflowOwnershipLost(
                f"Workflow instance {instance_id} is owned by another engine; this process must not suspend it"
            )
        logger.info(f"Workflow instance suspended: {instance_id}")

    async def load_instance(self, instance_id: str) -> WorkflowInstanceRecord | None:
        db = await self._ensure_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return WorkflowInstanceRecord(
            instance_id=row["instance_id"],
            wdl_text=row["wdl_text"],
            status=WorkflowInstanceState(row["status"]),
            inputs_json=row["inputs_json"],
            current_step_id=row["current_step_id"],
            context_json=row["context_json"],
            wake_at=self._parse_dt(row["wake_at"]),
            wait_event=row["wait_event"],
            wait_timeout=self._parse_dt(row["wait_timeout"]),
            error=row["error"],
            created_at=self._parse_dt(row["created_at"]) or datetime.now(),
            updated_at=self._parse_dt(row["updated_at"]) or datetime.now(),
        )

    async def find_due_instances(self) -> list[str]:
        db = await self._ensure_db()
        cursor = await db.execute(
            """
            SELECT instance_id FROM workflow_instances
            WHERE status='waiting' AND wake_at <= ?
            """,
            (now_iso(),),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def find_timed_out_event_instances(self) -> list[dict[str, Any]]:
        db = await self._ensure_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT instance_id, wait_event
            FROM workflow_instances
            WHERE status='waiting'
              AND wait_event IS NOT NULL
              AND wait_timeout IS NOT NULL
              AND wait_timeout <= ?
            """,
            (now_iso(),),
        )
        rows = await cursor.fetchall()
        return [
            {
                "instance_id": row["instance_id"],
                "wait_event": row["wait_event"],
            }
            for row in rows
        ]

    async def complete_instance(
        self,
        instance_id: str,
        context: dict[str, Any],
        error: str | None = None,
    ) -> bool:
        """写入终态；fencing 下守卫 owner 并释放认领。返回是否实际写入"""
        status = WorkflowInstanceState.FAILED if error else WorkflowInstanceState.COMPLETED
        sql = """
            UPDATE workflow_instances
            SET status=?, context_json=?, error=?, updated_at=?
            """
        params: list[Any] = [status.value, json.dumps(context), error, now_iso()]
        if self.owner_id is not None:
            sql += ", owner_id=NULL"
        sql += " WHERE instance_id=?"
        params.append(instance_id)
        if self.owner_id is not None:
            sql += " AND (owner_id IS NULL OR owner_id=?)"
            params.append(self.owner_id)
        # 终态守卫：实例已处于任何终态（含 cancel 释放 owner 后的 CANCELLED）
        # 时不再覆盖——cancel 与 complete 竞态下 cancel 先落库后 complete 不得
        # 把 CANCELLED 改写成 COMPLETED
        sql += " AND status NOT IN (?, ?, ?)"
        params.extend(
            [
                WorkflowInstanceState.COMPLETED.value,
                WorkflowInstanceState.FAILED.value,
                WorkflowInstanceState.CANCELLED.value,
            ]
        )
        cursor = await self._write((sql, tuple(params)))
        if cursor.rowcount == 0:
            logger.warning(f"Complete skipped for {instance_id}: instance owned by another engine")
            return False
        logger.info(f"Workflow instance {status.value}: {instance_id}")
        await self.prune_completed_instances()
        return True

    async def cancel_instance(self, instance_id: str, error: str | None = None) -> bool:
        """取消实例；fencing 下守卫 owner 并释放认领。返回是否实际写入"""
        sql = """
            UPDATE workflow_instances
            SET status=?,
                error=?,
                wake_at=NULL,
                wait_event=NULL,
                wait_timeout=NULL,
                updated_at=?
            """
        params: list[Any] = [WorkflowInstanceState.CANCELLED.value, error, now_iso()]
        if self.owner_id is not None:
            sql += ", owner_id=NULL"
        sql += " WHERE instance_id=?"
        params.append(instance_id)
        if self.owner_id is not None:
            sql += " AND (owner_id IS NULL OR owner_id=?)"
            params.append(self.owner_id)
        # 终态守卫：已完成/已失败的实例不可再取消（幂等取消仍可重复执行）
        sql += " AND status NOT IN (?, ?, ?)"
        params.extend(
            [
                WorkflowInstanceState.COMPLETED.value,
                WorkflowInstanceState.FAILED.value,
                WorkflowInstanceState.CANCELLED.value,
            ]
        )
        cursor = await self._write((sql, tuple(params)))
        if cursor.rowcount == 0:
            logger.warning(f"Cancel skipped for {instance_id}: instance owned by another engine")
            return False
        logger.info(f"Workflow instance cancelled: {instance_id}")
        await self.prune_completed_instances()
        return True

    async def recover_orphaned_running(self) -> int:
        """Mark any 'running' instances as 'interrupted' (crash recovery).

        Called on WorkflowEngine startup and when the engine subprocess dies.
        The fencing owner is KEPT on the interrupted row: only a *different*
        engine token can re-claim it, so a not-quite-dead orphan engine cannot
        win its instance back with its next write
        """
        cursor = await self._write(
            (
                "UPDATE workflow_instances SET status=?, updated_at=? WHERE status=?",
                (
                    WorkflowInstanceState.INTERRUPTED.value,
                    now_iso(),
                    WorkflowInstanceState.RUNNING.value,
                ),
            )
        )
        count = cursor.rowcount
        if count > 0:
            logger.info(f"Recovered {count} orphaned running workflow instance(s) → interrupted")
        return count

    async def prune_completed_instances(self, keep_days: int = 30) -> int:
        # 时间口径：updated_at 既有 now_iso() 显式写入（本地时间+'T' 分隔），
        # 也有建表时 sqlite CURRENT_TIMESTAMP（UTC+空格）落的历史值。cutoff
        # 用 datetime(...) 在 SQL 侧归一成 'YYYY-MM-DD HH:MM:SS' 再比较，
        # 两种历史格式的行都能正确参与字符串比较（新行不可能误归档）。
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).isoformat(" ", "seconds")
        terminal_statuses = [
            WorkflowInstanceState.COMPLETED.value,
            WorkflowInstanceState.FAILED.value,
            WorkflowInstanceState.CANCELLED.value,
            WorkflowInstanceState.INTERRUPTED.value,
        ]
        placeholders = ",".join("?" * len(terminal_statuses))
        try:
            cursor = await self._write(
                (
                    f"""
                    INSERT OR REPLACE INTO workflow_instances_archive
                        (instance_id, name, wdl_text, status,
                         inputs_json, current_step_id, context_json,
                         error, created_at, updated_at)
                    SELECT
                        instance_id, name, wdl_text, status,
                        inputs_json, current_step_id, context_json,
                        error, created_at, updated_at
                    FROM workflow_instances
                    WHERE status IN ({placeholders})
                      AND datetime(replace(updated_at, 'T', ' ')) < ?
                    """,
                    (*terminal_statuses, cutoff),
                ),
                (
                    f"""
                    DELETE FROM workflow_instances
                    WHERE status IN ({placeholders})
                      AND datetime(replace(updated_at, 'T', ' ')) < ?
                    """,
                    (*terminal_statuses, cutoff),
                ),
            )
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"Archived and pruned {deleted} old workflow instances (>{keep_days} days)")
            return deleted
        except Exception as exc:
            logger.warning(f"Workflow instance pruning failed: {exc}")
            return 0

    async def get_instance_status(self, instance_id: str) -> dict[str, Any] | None:
        db = await self._ensure_db()
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT status, current_step_id, error, created_at, updated_at
            FROM workflow_instances
            WHERE instance_id=?
            """,
            (instance_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "current_step_id": row["current_step_id"],
            "error": row["error"],
            "created_at": self._parse_dt(row["created_at"]),
            "updated_at": self._parse_dt(row["updated_at"]),
        }

    async def get_instance_run_view(self, instance_id: str) -> dict[str, Any] | None:
        """Live run view for workflow editor highlighting."""
        record = await self.load_instance(instance_id)
        if record is None:
            return None
        try:
            context = json.loads(record.context_json)
        except json.JSONDecodeError:
            context = {}
        if not isinstance(context, dict):
            context = {}
        skip = {"inputs", "_last_output"}
        completed_steps = sorted(str(key) for key in context if str(key) not in skip and not str(key).startswith("_"))

        # Derive progress from persisted context if progress tracking is present.
        progress = context.get("_progress") if isinstance(context, dict) else None
        if isinstance(progress, dict):
            completed = max(0, int(progress.get("completed", 0)))
            total = max(1, int(progress.get("total", 1)))
            percentage = min(100, int(100 * completed / total))
        else:
            completed = len(completed_steps)
            total = max(1, completed)
            percentage = 0 if record.status.value in ("pending", "running", "waiting") else 100

        return {
            "instance_id": instance_id,
            "status": record.status.value,
            "current_step_id": record.current_step_id,
            "wait_event": record.wait_event,
            "completed_steps": completed_steps,
            "error": record.error,
            "updated_at": self._dt_iso(record.updated_at),
            "progress": {
                "completed": completed,
                "total": total,
                "percentage": percentage,
            },
            "result": await self.get_instance_result(instance_id),
        }

    async def list_instances(
        self,
        status_filter: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        db = await self._ensure_db()
        db.row_factory = aiosqlite.Row
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            sql = f"""
                SELECT instance_id, name, status, current_step_id,
                       wait_event, error, created_at, updated_at
                FROM workflow_instances
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """
            cursor = await db.execute(sql, (*status_filter, limit, offset))
        else:
            sql = """
                SELECT instance_id, name, status, current_step_id,
                       wait_event, error, created_at, updated_at
                FROM workflow_instances
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """
            cursor = await db.execute(sql, (limit, offset))
        rows = await cursor.fetchall()
        return [
            {
                "instance_id": row["instance_id"],
                "name": row["name"] or "Unnamed",
                "status": row["status"],
                "current_step_id": row["current_step_id"],
                "wait_event": row["wait_event"],
                "error": row["error"],
                "created_at": self._dt_iso(self._parse_dt(row["created_at"])),
                "updated_at": self._dt_iso(self._parse_dt(row["updated_at"])),
            }
            for row in rows
        ]
