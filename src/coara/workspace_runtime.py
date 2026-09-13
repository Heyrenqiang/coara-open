"""Track the single locally-active coara CLI workspace (remote ingress binds here).

One running ``coara`` process publishes ``{coara_home}/runtime/active.json``.
Transport adapters (e.g. Matrix) use this file to route messages to the correct
workspace directory — remote clients are not separate workspaces.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.core.coara_home import ensure_workspace_layout
from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.time import utc_now_iso


@dataclass(slots=True)
class ActiveWorkspaceRuntime:
    workspace_id: str
    workspace_path: str
    workspace_name: str
    pid: int
    session_id: str
    coara_id: str
    coara_name: str
    started_at: str
    matrix_enabled: bool = False
    matrix_room_id: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ActiveWorkspaceRuntime:
        return cls(
            workspace_id=str(payload.get("workspace_id", "")),
            workspace_path=str(payload.get("workspace_path", "")),
            workspace_name=str(payload.get("workspace_name", "")),
            pid=int(payload.get("pid", 0)),
            session_id=str(payload.get("session_id", "")),
            coara_id=str(payload.get("coara_id", "")),
            coara_name=str(payload.get("coara_name", "")),
            started_at=str(payload.get("started_at", "")),
            matrix_enabled=bool(payload.get("matrix_enabled", False)),
            matrix_room_id=str(payload.get("matrix_room_id", "")),
        )


def runtime_file(coara_home: Path) -> Path:
    return coara_home / "runtime" / "active.json"


def is_pid_alive(pid: int) -> bool:
    """True when *pid* still refers to a running process.

    On Windows, ``OpenProcess`` can succeed for a process that has already
    exited (zombie / not fully reaped). Always confirm with
    ``GetExitCodeProcess`` == ``STILL_ACTIVE`` (259).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # PROCESS_QUERY_LIMITED_INFORMATION is enough for GetExitCodeProcess on
        # Vista+; fall back to PROCESS_QUERY_INFORMATION for older hosts.
        process_query_limited = 0x1000
        process_query = 0x0400
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited, False, int(pid))
        if not handle:
            handle = kernel32.OpenProcess(process_query, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_active_runtime(coara_home: Path) -> ActiveWorkspaceRuntime | None:
    path = runtime_file(coara_home)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    runtime = ActiveWorkspaceRuntime.from_dict(payload)
    if not is_pid_alive(runtime.pid):
        # 清掉僵尸登记，避免下次再误判；pid 对账防误删别的活进程刚写的文件
        clear_active_runtime(coara_home, pid=runtime.pid)
        return None
    return runtime


def publish_active_runtime(
    *,
    coara_home: Path,
    workspace_path: Path,
    workspace_name: str,
    session_id: str,
    coara_id: str,
    coara_name: str,
    matrix_enabled: bool = False,
    matrix_room_id: str = "",
) -> ActiveWorkspaceRuntime:
    # 启动竞态治理：active.json 里若登记着 另一个仍存活 的主进程 pid，说明并发
    # 启动/旧进程未退净——后写者会盖掉先活者的登记（实测：11084 活着、active.json
    # 却是已死的 33868）。此时拒绝覆盖，让先活者的登记保持，避免运行时事实错乱。
    target = runtime_file(coara_home)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict):
            existing_pid = int(existing.get("pid", 0) or 0)
            if existing_pid and existing_pid != os.getpid() and is_pid_alive(existing_pid):
                logger.warning(
                    f"Refuse to publish active runtime: another live main process (pid={existing_pid}) "
                    f"holds {target}; my pid={os.getpid()}"
                )
                return load_active_runtime(coara_home)  # type: ignore[return-value]
    paths = ensure_workspace_layout(workspace_path, configured_home=coara_home)
    runtime = ActiveWorkspaceRuntime(
        workspace_id=paths.workspace_id,
        workspace_path=str(workspace_path.resolve()),
        workspace_name=workspace_name,
        pid=os.getpid(),
        session_id=session_id,
        coara_id=coara_id,
        coara_name=coara_name,
        started_at=utc_now_iso(timespec="seconds"),
        matrix_enabled=matrix_enabled,
        matrix_room_id=matrix_room_id,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(target, asdict(runtime))
    return runtime


def update_active_runtime_matrix(
    coara_home: Path,
    *,
    matrix_enabled: bool,
    matrix_room_id: str = "",
) -> None:
    runtime = load_active_runtime(coara_home)
    if runtime is None or runtime.pid != os.getpid():
        return
    runtime.matrix_enabled = matrix_enabled
    if matrix_room_id:
        runtime.matrix_room_id = matrix_room_id
    target = runtime_file(coara_home)
    write_json_atomic(target, asdict(runtime))


def clear_active_runtime(coara_home: Path, *, pid: int | None = None) -> None:
    path = runtime_file(coara_home)
    if not path.exists():
        return
    if pid is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict) and int(payload.get("pid", -1)) != pid:
            return
    with contextlib.suppress(OSError):
        path.unlink()


def resolve_matrix_workspace_path(
    *,
    coara_home: Path | None,
    fallback_workspace: Path,
) -> tuple[Path, ActiveWorkspaceRuntime | None]:
    """Prefer the workspace bound to a live local CLI; else fall back."""
    if coara_home is None:
        return fallback_workspace.resolve(), None
    home = Path(coara_home).expanduser().resolve()
    runtime = load_active_runtime(home)
    if runtime is not None:
        workspace = Path(runtime.workspace_path).expanduser().resolve()
        if workspace.is_dir():
            return workspace, runtime
    return fallback_workspace.expanduser().resolve(), None
