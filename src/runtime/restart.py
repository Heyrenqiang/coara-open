"""内核自重启：安全交棒协议。

顺序不可调换——

1. 落意图：``<coara_home>/restart_intent.json`` 记下时间、原因、发起端、启动命令。
2. 让位：新进程与旧进程共用同一把实例锁（``<coara_home>/coara.pid``）和同一个监听
   端口。旧进程不先松开，新进程启动第一步就撞锁秒退，端口也无从绑定——所以旧进程
   先让出监听端口（只停 site，不拆 app/runner，可恢复），再释放实例锁。
3. 复用原启动方式：取 ``sys.orig_argv`` 原样复现，避免「看起来差不多」的命令把常驻
   模式换成另一种（模式错了等于换了个程序）。
4. 分离式拉起：新进程独立于旧进程（Windows: DETACHED_PROCESS + 新进程组；
   POSIX: start_new_session），不随旧进程退出而死。
5. 健康确认：轮询「监听端口上可连」——新进程抢到锁、bind 成功才可能连上，这是唯一
   允许交棒的凭据。无端口的纯内核退回读 ``restart_ready.json``（新实例启动早期写，
   含 pid 与晚于本次意图的时刻）。
6. 交棒：确认成功即 ``os._exit(0)``。**这是唯一允许退出的位置**——不做端上提示，
   不落带消息：重启是内核自己的能力，端下次连接自然连到新实例。

任一步失败：杀掉已拉起的子进程（若还活着）、把监听端口与实例锁都拿回来、清掉意图
文件，旧进程继续运行，并把原因返回给调用方（这是唯一会出现在端上的情形）。

静默契约：成功路径不产出任何端上可见输出，唯一可观测面是本模块的 ``[restart]`` 日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from src.core.instance_lock import acquire_instance_lock, release_instance_lock
from src.core.logger import logger

INTENT_NAME = "restart_intent.json"
READY_NAME = "restart_ready.json"
LAST_NAME = "restart_last.json"

_HEALTH_TIMEOUT_S = 30.0
_HEALTH_INTERVAL_S = 0.5
_KILL_GRACE_S = 3.0


def restart_home() -> Path:
    """重启标记文件所在目录：优先引导期解析出的 coara home，退回工作目录解析。"""
    with contextlib.suppress(Exception):
        from src.cli.main import resolve_bootstrap_coara_home

        home = resolve_bootstrap_coara_home()
        if home is not None:
            return Path(home)
    with contextlib.suppress(Exception):
        from src.core.coara_home import resolve_coara_home

        return Path(resolve_coara_home(Path.cwd()))
    return Path.cwd()


def launch_command() -> list[str]:
    """复现本进程的启动方式。

    ``sys.orig_argv``（3.10+）保留的是解释器视角的原始参数，托盘/常驻/直接运行都能
    如实还原；拿不到时退回 ``[sys.executable, *sys.argv]``。``-c`` 这类一次性参数不
    可复用（复现出来的是另一件事），直接判为不可重启。
    """
    argv = list(getattr(sys, "orig_argv", []) or [])
    if not argv:
        argv = [sys.executable, *sys.argv]
    if any(a == "-c" for a in argv[1:]):
        raise RuntimeError(f"无法复现启动方式（一次性参数）: {argv!r}")
    return argv


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _probe_host(host: str) -> str:
    """探测用主机名：监听在通配地址时回落到本机回环。"""
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "::", "[::]", "*"):  # noqa: S104 — 通配地址只做比较
        return "127.0.0.1"
    return h


def _port_reachable(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """端口上能建立连接即视为新实例已在服务（旧进程此时已让位）。"""
    if port <= 0:
        return False
    import socket

    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def consume_restart_intent(home: Path | None = None) -> None:
    """新实例启动早期调用：写就绪标记，并把意图归档成「上次重启」记录。

    幂等、失败只记日志——它绝不能影响内核启动。
    """
    base = home or restart_home()
    intent_path = base / INTENT_NAME
    try:
        intent = _read_json(intent_path)
        if not intent:
            return
        now = time.time()
        _write_json(
            base / READY_NAME,
            {"pid": os.getpid(), "ts": now, "intent_requested_at": intent.get("requested_at")},
        )
        _write_json(
            base / LAST_NAME,
            {
                "started_at": now,
                "pid": os.getpid(),
                "reason": intent.get("reason"),
                "requested_by": intent.get("requested_by"),
                "requested_at": intent.get("requested_at"),
                "launch_cmd": intent.get("launch_cmd"),
                "status": "started",
            },
        )
        intent_path.unlink(missing_ok=True)
        logger.info(f"[restart] new instance started (pid={os.getpid()}), intent archived")
    except Exception:  # noqa: BLE001 — 归档失败不影响启动
        logger.exception("[restart] consume intent failed")


def has_pending_restart(home: Path | None = None, *, max_age_s: float = 120.0) -> bool:
    """是否有一次刚发起、仍在窗口内的重启交棒未完成。

    新实例启动时用它放行：交棒期间旧内核仍在退场（``load_active_runtime`` 还能
    看到它），若照旧判「内核已在运行」直接退出，交棒就永远走不完——这正是
    ``/restart`` 实测「新实例启动即退出（code=0）」的原因。
    """
    base = Path(home) if home else restart_home()
    intent = _read_json(base / INTENT_NAME)
    if not intent:
        return False
    requested_at = float(intent.get("requested_at") or 0)
    return requested_at > 0 and (time.time() - requested_at) <= max_age_s


def _spawn(argv: list[str]) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {
        "close_fds": True,
        "cwd": os.getcwd(),
        "env": os.environ.copy(),
    }
    if os.name == "nt":  # pragma: no cover - Windows 专属
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:  # pragma: no cover - POSIX 专属
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)  # noqa: S603 — argv 来自本进程原始启动参数


def _kill(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except Exception:  # noqa: BLE001 — 宽限内没退就强杀
        with contextlib.suppress(Exception):
            proc.kill()


async def restart_kernel(
    *, reason: str, requested_by: str, root: Any = None, home: Path | None = None
) -> tuple[bool, str]:
    """执行协议 1-6。成功路径不会返回（内核已退出）；失败返回 (False, 原因)。"""
    base = Path(home) if home else restart_home()
    intent_path = base / INTENT_NAME
    ready_path = base / READY_NAME

    if _read_json(intent_path) is not None:
        return False, "已有一次重启未决，等它完成再试"

    try:
        argv = launch_command()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    server = getattr(root, "_web_server", None) if root is not None else None
    host = str(getattr(server, "host", "") or "127.0.0.1")
    port = int(getattr(server, "port", 0) or 0)

    def _flush_before_exit(rt: Any) -> None:
        """交棒前把还没落盘的用量/trace 尾部 flush——成功路径 os._exit 不经过 shutdown，
        daemon 写线程队列里未落盘的事件会静默丢。成本亚秒级，换确定性。"""
        store = getattr(rt, "_usage_store", None) if rt is not None else None
        if store is not None:
            with contextlib.suppress(Exception):
                store.flush(timeout=1.0)

    async def _rollback(detail: str) -> tuple[bool, str]:
        """回退：把实例锁与监听端口都拿回来，旧进程继续服务。"""
        with contextlib.suppress(Exception):
            acquire_instance_lock(base)
        if server is not None:
            with contextlib.suppress(Exception):
                await server.resume_listening()
        intent_path.unlink(missing_ok=True)
        logger.warning(f"[restart] rolled back: {detail}")
        return False, detail

    # 让位（顺序不可换：先松端口，再松锁）。旧进程不松开，新进程必然启动即死。
    if server is not None and not await server.yield_listening():
        return False, "旧进程无法释放监听端口，已放弃重启"
    with contextlib.suppress(Exception):
        release_instance_lock(base)

    ready_path.unlink(missing_ok=True)
    requested_at = time.time()
    _write_json(
        intent_path,
        {
            "requested_at": requested_at,
            "reason": reason,
            "requested_by": requested_by,
            "launch_cmd": argv,
            "pid": os.getpid(),
        },
    )
    logger.info(f"[restart] intent written; relaunching: {argv!r}")

    try:
        proc = _spawn(argv)
    except Exception as exc:  # noqa: BLE001 — 拉不起来就地回退
        return await _rollback(f"拉起新实例失败：{exc}")
    logger.info(f"[restart] child spawned (pid={proc.pid}); waiting for readiness")

    deadline = time.time() + _HEALTH_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            return await _rollback(f"新实例启动即退出（code={proc.returncode}）")
        if port > 0:
            # 有监听端口：只认「端口上可连」——它证明新进程真的在服务。
            if _port_reachable(host, port):
                logger.info(f"[restart] new instance serving on {_probe_host(host)}:{port}; exiting old process")
                _flush_before_exit(root)
                os._exit(0)
        else:
            # 纯内核（无 Web）：退回实例自报的就绪标记。
            ready = _read_json(ready_path)
            if ready and float(ready.get("ts") or 0) > requested_at:
                logger.info(f"[restart] readiness confirmed (new pid={ready.get('pid')}); exiting old process")
                _flush_before_exit(root)
                os._exit(0)
        await asyncio.sleep(_HEALTH_INTERVAL_S)

    _kill(proc)
    return await _rollback(f"新实例 {_HEALTH_TIMEOUT_S:.0f}s 内未就绪，已回退")
