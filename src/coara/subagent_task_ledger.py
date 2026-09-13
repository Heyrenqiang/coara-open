"""后台子智能体任务台账（append-only JSONL）。

后台子智能体是主进程内的 asyncio 协程，进程被杀时协程随之消失且无人知晓。
启动/终态各落一行到 ``<coara_home>/workspaces/<workspace_id>/subagent_tasks.jsonl``，
重启时扫描仍为 running 的记录写入工作空间动态，提示用户任务中断了（不自动恢复执行）。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths
from src.core.logger import logger
from src.core.time import now_iso

FILE_NAME = "subagent_tasks.jsonl"


def _ledger_path(workspace_dir: str | Path, coara_home: str | Path | None) -> Path:
    # migrate=False：扫描保持只读，不替从未跑过子智能体的空间建目录
    return CoaraHomePaths.for_workspace(workspace_dir, coara_home, migrate=False).workspace_home / FILE_NAME


def _append(workspace_dir: str | Path, coara_home: str | Path | None, record: dict[str, Any]) -> None:
    try:
        path = _ledger_path(workspace_dir, coara_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"Subagent task ledger append failed ({record.get('task_id')}): {exc}")


def record_started(
    workspace_dir: str | Path,
    coara_home: str | Path | None,
    *,
    task_id: str,
    subagent_type: str,
    description: str,
    session_id: str,
) -> None:
    """后台子智能体启动落一行 running 记录。"""
    _append(
        workspace_dir,
        coara_home,
        {
            "task_id": task_id,
            "subagent_type": subagent_type,
            "description": description,
            "workspace_dir": str(workspace_dir),
            "session_id": session_id,
            "started_at": now_iso(),
            "status": "running",
        },
    )


def record_terminal(
    workspace_dir: str | Path,
    coara_home: str | Path | None,
    *,
    task_id: str,
    status: str,
) -> None:
    """终态落一行（completed/failed/interrupted），折叠时以最后一条为准。"""
    _append(workspace_dir, coara_home, {"task_id": task_id, "status": status, "ended_at": now_iso()})


def running_records(workspace_dir: str | Path, coara_home: str | Path | None) -> list[dict[str, Any]]:
    """折叠台账（task_id → 最后一条记录），返回仍为 running 的记录。"""
    path = _ledger_path(workspace_dir, coara_home)
    if not path.is_file():
        return []
    latest: dict[str, dict[str, Any]] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_id = record.get("task_id")
            if task_id:
                latest[str(task_id)] = record
    except OSError as exc:
        logger.warning(f"Subagent task ledger read failed for {workspace_dir}: {exc}")
        return []
    return [record for record in latest.values() if record.get("status") == "running"]


def report_interrupted_tasks(root: Any, workspace_dir: str | Path, coara_home: str | Path | None = None) -> int:
    """把进程被杀时仍在跑的后台子智能体写入工作空间动态（只提示，不恢复执行）。

    先把 running 折成 interrupted 终态，保证每次中断只提示一次。
    janitor/daily 等系统自恢复角色会自行重跑，只落终态不提示（与重启清算通知同口径）。
    """
    from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES

    records = running_records(workspace_dir, coara_home)
    if not records:
        return 0

    workspace_name: str | None = None
    wm = getattr(root, "workspace_manager", None)
    if wm is not None:
        with contextlib.suppress(Exception):
            workspace_name = wm.name_for_path(workspace_dir)
    store = None
    getter = getattr(root, "_updates_store", None)
    if callable(getter):
        with contextlib.suppress(Exception):
            store = getter()

    reported = 0
    for record in sorted(records, key=lambda r: str(r.get("started_at") or "")):
        task_id = str(record.get("task_id") or "")
        # 无论动态是否写成，先落 interrupted 终态，防止每次重启重复提示
        record_terminal(workspace_dir, coara_home, task_id=task_id, status="interrupted")
        subagent_type = str(record.get("subagent_type") or "")
        if store is None or not workspace_name or not task_id or subagent_type in SYSTEM_ONLY_SUBAGENT_TYPES:
            continue
        description = str(record.get("description") or "")
        text = f"中断的子智能体任务 [{task_id}]（{subagent_type}）：{description}\n进程意外中断，该任务未跑完。"
        try:
            store.append(
                workspace=workspace_name,
                source_id=f"subagent-task:{task_id}",
                event_type="subagent_interrupted",
                dedupe_key=f"subagent_interrupted:{task_id}",
                text=text,
                payload={
                    "title": f"中断的子智能体任务 · {subagent_type}",
                    "task_id": task_id,
                    "subagent_type": subagent_type,
                    "description": description,
                },
                type="note",
                salience="high",
                source_kind="internal_report",
            )
            reported += 1
        except Exception as exc:
            logger.warning(f"Interrupted subagent task update write failed ({task_id}): {exc}")
    return reported
