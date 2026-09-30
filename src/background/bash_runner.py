"""Bash background task runner — independent subprocess execution"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.background.task_store import TaskRecord, TaskStatus, TaskStore
from src.background.task_store_paths import default_task_store, task_store_for_workspace
from src.core.logger import logger
from src.core.subprocess_cleanup import await_with_timeout, terminate_subprocess_tree
from src.utils.log_noise import collapse_log_noise
from src.utils.win_proc import no_window_creationflags

# 后台任务输出持久化上限常量（与 coara.injections.background_injector.INJECT_FULL_MAX
# 同值；该模块含 coara 注入逻辑，background 层只需此常量，本地定义避免反向依赖内核）。
_INJECT_FULL_MAX = 100_000

# 后台任务默认硬上限（秒）：宽松的兜底，防止子进程挂死导致任务永远 running
# config.yaml 顶层 background_task_timeout_seconds 可覆盖；设为 0 关闭兜底
DEFAULT_BACKGROUND_TIMEOUT_SECONDS = 2 * 3600
TASK_ARTIFACT_RETENTION_DAYS = 14

# 兜底巡检：监控协程（_wrapped）卡死后，磁盘记录永远 running、完成通知
# 永不发出（bash-ede88b97 事故形态——内核重启清空内存任务、子进程跑完，
# finally 随协程销毁不再执行）。巡检对「子进程 returncode 已落定但监控
# 协程仍未 done」的任务强制 cancel，逼其走 finally 收官落终态 + 发完成事件
GHOST_TASK_GRACE_SECONDS = 120.0
_GHOST_PATROL_INTERVAL_SECONDS = 30.0


def resolve_background_task_timeout() -> float | None:
    """Default timeout (seconds) for background bash tasks; 0 or negative disables the fallback."""
    from src.core.config import config_manager

    raw = getattr(config_manager, "_raw_config", None) or {}
    value = raw.get("background_task_timeout_seconds", DEFAULT_BACKGROUND_TIMEOUT_SECONDS)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_BACKGROUND_TIMEOUT_SECONDS)
    return seconds if seconds > 0 else None


class BashBackgroundRunner:
    """Singleton runner that manages background bash subprocess tasks"""

    _instance: BashBackgroundRunner | None = None
    _singleton_lock = threading.Lock()

    _tasks: dict[str, asyncio.Task | None]
    _task_stores: dict[str, TaskStore]
    # Launch-workspace stamp per task so interrupt can scope cancel_all to the
    # pinning workspace instead of killing other workspaces' background work.
    _task_workspaces: dict[str, str]
    # Launch-end stamp per task（cli/web/matrix）so cancel can scope by originating end.
    _task_origins: dict[str, str]
    # Launch-session stamps (coara_id / session_id) so completion events can be
    # routed back to the originating workspace session after a switch.
    _task_sessions: dict[str, dict[str, str | None]]
    # Placeholders cancelled before asyncio.Task is assigned (Ctrl+C race).
    _cancel_requested: set[str]
    # Cap total records to prevent unbounded growth in long-running sessions.
    _MAX_RECORDS = 500
    # 幽灵任务巡检协程（按事件循环惰性拉起，实例属性在 __new__ 中不初始化）
    _ghost_patrol: asyncio.Task | None = None

    def __new__(cls) -> BashBackgroundRunner:
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._tasks = {}
                    instance._task_stores = {}
                    instance._task_workspaces = {}
                    instance._task_sessions = {}
                    instance._task_origins = {}
                    instance._cancel_requested = set()
                    cls._instance = instance
        return cls._instance

    def _store_for(self, task_id: str) -> TaskStore:
        return self._task_stores.get(task_id) or default_task_store()

    def _task_dir(self, task_id: str, workspace_dir: Path) -> Path:
        path = workspace_dir / ".coara" / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    async def _reap_orphaned_pipes(process: asyncio.subprocess.Process, *, grace: float = 10.0) -> None:
        """进程退出但 wait() 仍挂时强制收官（Windows 孙进程握管场景）"""
        try:
            while process.returncode is None:
                await asyncio.sleep(1.0)
            await asyncio.sleep(grace)
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — 尽力而为
            return

    @staticmethod
    def _prune_terminal_task_artifacts(store: TaskStore, workspace_dir: Path) -> int:
        """Remove old output directories only for persisted terminal bash tasks."""
        cutoff = datetime.now().astimezone() - timedelta(days=TASK_ARTIFACT_RETENTION_DAYS)
        # 全局 home 下多空间共用一个 TaskStore：task_root 是「当前空间」的产物根，
        # 只清本空间的记录（record.workspace_dir 打戳于创建时刻）；跨空间误删曾把
        # 空间 B 的产物在 A 启动任务时清掉（09-28 审计 P1）。
        task_root = (workspace_dir / ".coara" / "tasks").resolve()
        try:
            current_ws = os.path.normcase(str(workspace_dir.resolve()))
        except OSError:
            current_ws = os.path.normcase(str(workspace_dir))
        removed = 0
        terminal = {
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.KILLED.value,
            TaskStatus.TIMED_OUT.value,
        }
        for record in store.list_all():
            if (
                record.kind != "bash"
                or record.status not in terminal
                or not record.completed_at
                or not record.output_path
            ):
                continue
            try:
                rec_ws = str(record.workspace_dir or "").strip()
                if rec_ws:
                    try:
                        rec_ws_cmp = os.path.normcase(str(Path(rec_ws).resolve()))
                    except OSError:
                        rec_ws_cmp = os.path.normcase(rec_ws)
                    if rec_ws_cmp != current_ws:
                        continue
                completed = datetime.fromisoformat(record.completed_at)
                if completed.tzinfo is None:
                    completed = completed.astimezone()
                task_dir = Path(record.output_path).resolve().parent
                if completed > cutoff or task_dir.parent != task_root or not task_dir.name.startswith("bash-"):
                    continue
                if (task_dir / "watch_meta.json").exists():
                    # watch 任务的 output.log / watch_meta.json 是命中审计的唯一
                    # 证据，不受 14 天产物清理约束（09-28 审计 P1）
                    continue
                shutil.rmtree(task_dir)
                # 产物已删：清掉记录上的 output_path，消除指向已删路径的悬空
                store.update(record.task_id, output_path=None)
                removed += 1
            except FileNotFoundError:
                # 目录已被其它清理路径移除（幂等竞态）：目标已达成，静默跳过
                continue
            except (OSError, ValueError) as exc:
                logger.warning(f"Failed to prune background task artifacts for {record.task_id}: {exc}")
        if removed:
            logger.info(f"Pruned {removed} old background task artifact director{'y' if removed == 1 else 'ies'}")
        return removed

    def _default_shell(self) -> tuple[str, str]:
        """Return (shell_name, shell_path)."""
        if os.name == "nt":
            from src.core.process import resolve_windows_powershell

            ps = resolve_windows_powershell()
            if ps:
                return ("powershell", ps)
            return ("cmd", "cmd.exe")
        bash = shutil.which("bash") or shutil.which("sh")
        return ("bash", bash or "/bin/sh")

    async def create_task(
        self,
        command: str,
        description: str,
        *,
        workspace_dir: Path,
        timeout: float | None = None,
        cwd: str | None = None,
        output_watch: Any = None,
        coara_id: str | None = None,
        session_id: str | None = None,
        coara_home: Path | str | None = None,
        origin_source: str = "",
        argv: list[str] | tuple[str, ...] | None = None,
        _process: asyncio.subprocess.Process | None = None,
        _temp_scripts: list[Path] | None = None,
    ) -> str:
        """Start a background bash task and return its task_id immediately"""
        from src.background.output_watch import (
            OutputWatchConfig,
            OutputWatchMatcher,
            is_shell_notify_enabled,
            merge_output_watch_config,
        )
        from src.core.time import now_iso

        task_id = f"bash-{uuid.uuid4().hex[:8]}"
        store = task_store_for_workspace(workspace_dir, configured_home=coara_home)
        self._prune_terminal_task_artifacts(store, workspace_dir)
        task_dir = self._task_dir(task_id, workspace_dir)
        output_path = task_dir / "output.log"
        now = now_iso()
        self._task_stores[task_id] = store
        self._task_sessions[task_id] = {"coara_id": coara_id, "session_id": session_id}
        self._task_workspaces[task_id] = str(workspace_dir)
        self._task_origins[task_id] = str(origin_source or "").strip()

        watch_config: OutputWatchConfig | None = None
        if output_watch is not None and is_shell_notify_enabled():
            watch_config = merge_output_watch_config(output_watch)
            if watch_config is not None and not watch_config.pattern:
                watch_config = None

        # 无显式超时且非输出监控任务时套用宽松默认上限，防止进程挂死后任务永远 running
        if timeout is None and watch_config is None:
            timeout = resolve_background_task_timeout()

        record = TaskRecord(
            task_id=task_id,
            kind="bash",
            description=description,
            status=TaskStatus.RUNNING.value,
            created_at=now,
            updated_at=now,
            command=command,
            output_path=str(output_path),
            origin_source=str(origin_source or "").strip(),
            workspace_dir=str(workspace_dir),
        )
        store.save(record)

        self._tasks[task_id] = None

        # 幽灵巡检的进程引用盒：spawn 完成后 _wrapped 回填，巡检据此判断
        # 「子进程已退但监控协程未收官」的卡死形态（box 可随协程一起取消）
        process_ref: list[asyncio.subprocess.Process | None] = [_process]

        exec_cwd = cwd or str(workspace_dir)
        direct_argv = tuple(str(a) for a in argv) if argv else ()

        async def _wrapped() -> None:
            process: asyncio.subprocess.Process | None = _process
            matcher: OutputWatchMatcher | None = None
            try:
                if process is None:
                    spawn_kwargs: dict[str, Any] = {
                        "stdin": subprocess.DEVNULL,
                        "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.PIPE,
                        "cwd": exec_cwd,
                    }
                    if os.name == "nt":
                        spawn_kwargs["creationflags"] = no_window_creationflags(subprocess.CREATE_NEW_PROCESS_GROUP)
                    else:
                        spawn_kwargs["start_new_session"] = True

                    if direct_argv:
                        args = direct_argv
                    elif os.name == "nt":
                        from src.core.process import build_powershell_exec_cmd

                        ps_exec = build_powershell_exec_cmd(command)
                        if ps_exec is not None:
                            ps_path, ps_args = ps_exec
                            args = (ps_path, *ps_args)
                        else:
                            args = ("cmd.exe", "/c", command)
                    else:
                        _, shell_path = self._default_shell()
                        args = (shell_path, "-c", command)

                    process = await asyncio.create_subprocess_exec(*args, **spawn_kwargs)
                process_ref[0] = process
                store.update(task_id, pid=process.pid)

                # （任务滞留「状态待对账」）。宽限 10s 后强制关闭本地管道端收官。
                reaper = asyncio.create_task(self._reap_orphaned_pipes(process))

                if watch_config is not None:

                    def _on_match(payload: dict[str, Any]) -> None:
                        payload["coara_id"] = coara_id or "bash-background-runner"
                        payload["session_id"] = session_id
                        self._publish_shell_output_matched(payload)

                    matcher = OutputWatchMatcher(
                        config=watch_config,
                        task_id=task_id,
                        log_path=output_path,
                        on_match=_on_match,
                    )
                    matcher.write_meta(task_dir)

                with output_path.open("ab") as out_file:

                    async def _read_stream(stream: asyncio.StreamReader | None) -> None:
                        if stream is None:
                            return
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            try:
                                out_file.write(line)
                                out_file.flush()
                            except ValueError:
                                break
                            if matcher is not None:
                                from src.core.process import decode_subprocess_output

                                matcher.feed_line(decode_subprocess_output(line))

                    drain_task = asyncio.gather(
                        _read_stream(process.stdout),
                        _read_stream(process.stderr),
                    )
                    timed_out = False
                    returncode: int | None = None
                    try:
                        if timeout is None:
                            returncode = await process.wait()
                        else:
                            try:
                                returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
                            except TimeoutError:
                                timed_out = True
                                await terminate_subprocess_tree(process)
                                store.update(
                                    task_id,
                                    status=TaskStatus.TIMED_OUT.value,
                                    timed_out=True,
                                    exit_code=process.returncode,
                                )
                    finally:
                        reaper.cancel()
                        await await_with_timeout(
                            drain_task,
                            timeout=5.0,
                            label=f"bash output drain [{task_id}]",
                        )

                    if timed_out:
                        return

                store.update(
                    task_id,
                    status=TaskStatus.FAILED.value if returncode != 0 else TaskStatus.COMPLETED.value,
                    exit_code=returncode,
                )

            except asyncio.CancelledError:
                if process is not None and process.returncode is None:
                    # 用户主动停止（task stop / Ctrl+C）：优雅终止给进程收尾机会
                    await terminate_subprocess_tree(process, graceful=True)
                store.update(
                    task_id,
                    status=TaskStatus.KILLED.value,
                    interrupted=True,
                )
                raise
            except Exception as exc:
                logger.error(f"Background bash task [{task_id}] failed: {exc}")
                store.update(
                    task_id,
                    status=TaskStatus.FAILED.value,
                    error=str(exc),
                )
            finally:
                from src.core.time import now_iso

                if matcher is not None:
                    matcher.write_meta(task_dir)
                store.update(task_id, completed_at=now_iso())
                if output_path.exists():
                    # 完整输出持久化（result_full 用于注入主会话；完整日志仍在 output.log 可查）。
                    # 注入侧做噪音折叠：进度墙/刷屏行折成摘要行，省词元；原日志不动。
                    raw_full = await self._read_output_preview(output_path, max_chars=_INJECT_FULL_MAX)
                    store.update(
                        task_id,
                        result_preview=await self._read_output_preview(output_path),
                        result_full=collapse_log_noise(raw_full),
                    )
                self._tasks.pop(task_id, None)
                self._task_stores.pop(task_id, None)
                self._task_workspaces.pop(task_id, None)
                self._task_origins.pop(task_id, None)
                self._cancel_requested.discard(task_id)

                # never accumulate in the workspace.
                for script in _temp_scripts or []:
                    try:
                        Path(script).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.debug(f"Failed to remove temp script {script} for [{task_id}]: {exc}")

                rec = store.load(task_id)
                if rec is not None:
                    self._publish_completion(rec)

        task = asyncio.create_task(_wrapped(), name=task_id)
        self._tasks[task_id] = task
        task._coara_process_ref = process_ref  # type: ignore[attr-defined]
        self._ensure_ghost_patrol()
        if task_id in self._cancel_requested:
            self._cancel_requested.discard(task_id)
            task.cancel()
            logger.info(f"Background bash task [{task_id}] cancelled immediately (cancel requested before start)")

        logger.info(f"Background bash task [{task_id}] started: {description}")
        return task_id

    async def stop_task(self, task_id: str, *, force: bool = False) -> bool:
        """Request a running background task to stop."""
        task = self._tasks.get(task_id)
        if task is None:
            if task_id in self._tasks:
                self._cancel_requested.add(task_id)
                from src.core.time import now_iso

                self._store_for(task_id).update(
                    task_id,
                    status=TaskStatus.KILLED.value,
                    interrupted=True,
                    completed_at=now_iso(),
                )
            return False
        if task.done():
            return False
        task.cancel()
        from src.core.time import now_iso

        self._store_for(task_id).update(
            task_id,
            status=TaskStatus.KILLED.value,
            interrupted=True,
            completed_at=now_iso(),
        )
        return True

    def cancel_all(self, workspace_dir: str | None = None, origin_source: str | None = None) -> int:
        """Hard-cancel registered bash background tasks (Ctrl+C / /stop)"""
        from src.core.time import now_iso

        def _in_scope(tid: str) -> bool:
            if workspace_dir is not None:
                launch = self._task_workspaces.get(tid, "")
                # 无归属记录（老占位/竞态）按在范围内处理——打断语义宁多勿漏
                if launch and launch != str(workspace_dir):
                    return False
            if origin_source is not None:
                origin = self._task_origins.get(tid, "")
                if origin and origin != str(origin_source):
                    return False
            return True

        cancelled = 0
        now = now_iso()
        for task_id, task in list(self._tasks.items()):
            if not _in_scope(task_id):
                continue
            if task is None:
                self._cancel_requested.add(task_id)
                self._store_for(task_id).update(
                    task_id,
                    status=TaskStatus.KILLED.value,
                    interrupted=True,
                    completed_at=now,
                )
                cancelled += 1
                logger.debug(f"Background bash [{task_id}] cancel-all before start")
                continue
            if task.done():
                continue
            task.cancel()
            self._store_for(task_id).update(
                task_id,
                status=TaskStatus.KILLED.value,
                interrupted=True,
                completed_at=now,
            )
            cancelled += 1
        if cancelled:
            logger.warning(f"Hard-cancelled {cancelled} bash background task(s)")
        return cancelled

    def set_event_bus(self, event_bus: Any) -> None:
        """Set the EventBus for publishing task completion notifications."""
        self._event_bus = event_bus

    def _publish_completion(self, record: TaskRecord) -> None:
        """Publish a background task completion event to the EventBus."""
        if not hasattr(self, "_event_bus") or self._event_bus is None:
            return
        try:
            from src.core.events import TraceEvent

            # Pop the launch-session stamp so Root can route the notification
            # back to the originating workspace session (not the foreground).
            stamps = self._task_sessions.pop(record.task_id, None) or {}

            self._event_bus.publish(
                TraceEvent(
                    coara_id="bash-background-runner",
                    coara_name="BashBackgroundRunner",
                    event_type="background_task_complete",
                    message=f"Background bash task completed: {record.task_id}",
                    payload={
                        "task_id": record.task_id,
                        "kind": "bash",
                        "status": record.status,
                        "description": record.description,
                        "terminal_reason": self._terminal_reason(record),
                        "has_error": self._has_error(record),
                        "error": record.error,
                        "exit_code": record.exit_code,
                        "result_preview": record.result_preview or "",
                        "result_full": record.result_full or "",
                        "log_path": str(record.output_path or ""),
                        "origin_source": record.origin_source or "",
                        "coara_id": stamps.get("coara_id") or "",
                        "session_id": stamps.get("session_id") or "",
                    },
                )
            )
        except Exception as exc:
            logger.warning(f"Failed to publish bash task completion: {exc}")

    def _publish_shell_output_matched(self, payload: dict[str, Any]) -> None:
        """Publish a shell output pattern match to wake Root."""
        if not hasattr(self, "_event_bus") or self._event_bus is None:
            return
        try:
            from src.core.events import TraceEvent

            task_id = payload.get("task_id", "unknown")
            self._event_bus.publish(
                TraceEvent(
                    coara_id=str(payload.get("coara_id") or "bash-background-runner"),
                    coara_name="BashBackgroundRunner",
                    event_type="shell_output_matched",
                    message=f"Shell output matched sentinel: {task_id}",
                    payload=payload,
                )
            )
        except Exception as exc:
            logger.warning(f"Failed to publish shell output match: {exc}")

    @staticmethod
    def _terminal_reason(record: TaskRecord) -> str:
        if record.status == TaskStatus.KILLED.value:
            return "killed"
        if record.status == TaskStatus.TIMED_OUT.value:
            return "timed_out"
        if record.status == TaskStatus.FAILED.value or record.error:
            return "failed"
        if record.exit_code is not None and record.exit_code != 0:
            return "failed"
        return "completed"

    def _ensure_ghost_patrol(self) -> None:
        """按需拉起幽灵任务巡检协程（当前循环单例，跑空自止）。"""
        patrol = self.__dict__.get("_ghost_patrol")
        if patrol is not None and not patrol.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ghost_patrol = asyncio.create_task(self._ghost_patrol_loop(), name="bash-ghost-patrol")

    async def _ghost_patrol_loop(self) -> None:
        # 至少跑一轮再按需续跑——_create_task 的协程可能尚未拿循环就被巡检
        try:
            while True:
                await asyncio.sleep(_GHOST_PATROL_INTERVAL_SECONDS)
                pending = 0
                now = asyncio.get_running_loop().time()
                for task_id, task in list(self._tasks.items()):
                    if task is None or task.done():
                        continue
                    pending += 1
                    box = getattr(task, "_coara_process_ref", None)
                    process = box[0] if box else None
                    if process is None or process.returncode is None:
                        continue
                    settled_at = getattr(task, "_coara_rc_settled_at", None)
                    if settled_at is None:
                        task._coara_rc_settled_at = now  # type: ignore[attr-defined]
                        continue
                    if now - settled_at < GHOST_TASK_GRACE_SECONDS:
                        continue
                    if getattr(task, "_coara_ghost_cancelled", False):
                        # 已取消过一次：二次 cancel 会打断正在跑的 finally 收尾
                        # （落终态/发完成事件被截断，幽灵反而坐实），等它自己收官
                        continue
                    # 子进程已退出超过宽限期，监控协程仍未收官——典型挂点是
                    # Windows 孙进程握管导致 process.wait() 被管道 EOF 吊死；
                    # _reap_orphaned_pipes 的 transport.close() 在 Proactor 上
                    # 不可靠地唤醒 wait()。硬取消逼其走 finally 落终态发事件，
                    # 否则磁盘记录永 running、完成通知永不发出、web 徽标永转
                    logger.warning(
                        f"Ghost background task [{task_id}]: subprocess exited "
                        f"(rc={process.returncode}) but monitor stuck for {now - settled_at:.0f}s; force-cancelling"
                    )
                    task._coara_ghost_cancelled = True  # type: ignore[attr-defined]
                    task.cancel()
                if not pending:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检失败绝不能炸事件循环
            logger.warning(f"Ghost task patrol failed: {exc}")

    @staticmethod
    def _has_error(record: TaskRecord) -> bool:
        if record.error:
            return True
        if record.status in {TaskStatus.FAILED.value, TaskStatus.TIMED_OUT.value}:
            return True
        return record.exit_code is not None and record.exit_code != 0

    @staticmethod
    async def _read_output_preview(output_path: Path, max_chars: int = 8000) -> str:
        from src.core.file_tail import read_tail_text

        if not output_path.exists():
            return ""
        try:
            return await asyncio.to_thread(read_tail_text, output_path, max_chars=max_chars)
        except Exception:
            return ""
