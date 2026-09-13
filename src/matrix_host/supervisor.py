"""GoMatrix 托管监督器：coara 拥有 gomatrix 的生命周期与数据目录。

定位见 docs/接入层-gomatrix.md——gomatrix 是 coara 的内嵌接入层，核心职责
是连接手机与本地 coara；外部智能体可接入，但只是访客。

托管语义：

- 数据目录归 coara Home：``<coara_home>/matrix/``（gomatrix.toml / gomatrix.db /
  media）。首次启动从二进制旁的旧布局一次性迁移（toml 必拷；db/media 有才拷）。
- 端口以 coara 配置 ``matrix.port`` 为准：拷贝来的 toml 中 port 行被改写。
- adopt-or-spawn：端口上已有健康实例（如用户手动起的）则接入它、
  不动其生命周期；否则以 ``--config <toml>`` 拉起无头子进程。
  quick tunnel 的 URL 随 cloudflared 重启变化，adopt 避免 coara 重启时
  把 gomatrix 杀掉重启导致手机断连需重扫。
- 看门狗：自己拉起的进程退出即重启（退避 5s → 60s 封顶）；连续 3 次健康
  探测失败则杀掉重拉。adopt 的外部实例失联同样接管拉起（结婚语义：coara
  最终负责实例存活）。
- coara 关停时只终止自己拉起的进程。
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

from src.core.json_store import write_text_atomic
from src.core.logger import logger

_HEALTH_PATH = "/_matrix/client/versions"
_HEALTH_TIMEOUT = 2.0
_SPAWN_WAIT_SECONDS = 20.0
_WATCH_INTERVAL_SECONDS = 10.0
_UNHEALTHY_KILL_THRESHOLD = 3
_RESTART_BACKOFFS = (5.0, 15.0, 60.0)

# 数据目录下从旧布局一次性迁移的文件/目录名
_MIGRATE_FILES = ("gomatrix.toml", "gomatrix.db", "gomatrix.db-wal", "gomatrix.db-shm")
_MIGRATE_DIRS = ("media",)

_DEFAULT_TOML = """server_name = "coara.local"
database_path = "./gomatrix.db"
address = "0.0.0.0"
port = {port}
allow_registration = true
allow_encryption = false
default_room_version = "10"
media_path = "./media"
log_level = "info"
"""


def _dev_binary_candidates() -> list[Path]:
    """Repo checkout with a locally built gomatrix binary (not shipped in git)."""
    from src.core.config import _repo_root

    root = _repo_root()
    if root is None:
        return []
    exe_name = "gomatrix.exe" if sys.platform == "win32" else "gomatrix"
    return [root / "gomatrix" / exe_name, root / "deploy" / "bundle" / exe_name]


def _is_packaged_coara_install() -> bool:
    """True when running from a release install tree (not editable dev checkout)."""
    if (os.environ.get("COARA_ROOT") or "").strip():
        return True
    try:
        exe = Path(sys.executable).resolve()
        lowered = str(exe).lower()
        return any(marker in lowered for marker in ("/.local/coara/", "\\.local\\coara\\", "\\localappdata\\coara\\"))
    except OSError:
        return False


def find_binary(matrix_cfg: Any) -> Path | None:
    """按优先级探测 gomatrix 二进制：配置覆盖 → 环境变量 → 安装包 bin/。"""
    candidates: list[Path] = []
    configured = str(getattr(matrix_cfg, "host_binary", "") or "").strip()
    if configured:
        candidates.append(Path(configured))
    env = (os.environ.get("COARA_GOMAX_BINARY") or "").strip()
    if env:
        candidates.append(Path(env))
    exe_name = "gomatrix.exe" if sys.platform == "win32" else "gomatrix"
    # 发行布局：<install>/bin/gomatrix（coara 的 python 嵌入在 bin 下）
    for base in (Path(sys.executable).resolve().parent, Path(sys.prefix).resolve()):
        candidates.extend([base / exe_name, base.parent / "bin" / exe_name])
    candidates.extend(_dev_binary_candidates())
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _rewrite_port(toml_text: str, port: int) -> str:
    """把 toml 顶层 port 改写为指定值；没有 port 行时插到首个 section 之前
    （避免误写进尾部 section 里）。容忍前导空格与行尾注释。"""
    if re.search(r"(?m)^\s*port\s*=", toml_text):
        return re.sub(r"(?m)^\s*port\s*=.*$", f"port = {port}", toml_text, count=1)
    match = re.search(r"(?m)^\[", toml_text)
    line = f"port = {port}\n"
    if match:
        return toml_text[: match.start()] + line + toml_text[match.start() :]
    return toml_text.rstrip("\n") + "\n" + line


def rewrite_toml_port(toml: Path, port: int) -> None:
    """每次 spawn 前把数据目录 toml 的 port 归一到 coara 配置（幂等）。

    首次拷贝之外的场景（用户后续改了 matrix.port）也必须生效——否则
    健康探测打新端口、实例听旧端口，看门狗会进入重启死循环。
    """
    if not toml.is_file():
        return
    write_text_atomic(toml, _rewrite_port(toml.read_text(encoding="utf-8"), port))


def ensure_data_dir(coara_home: Path, binary: Path, port: int) -> Path:
    """准备 ``<coara_home>/matrix/`` 数据目录并做一次性迁移，返回 toml 路径。"""
    data_dir = coara_home / "matrix"
    data_dir.mkdir(parents=True, exist_ok=True)
    legacy = binary.parent

    toml = data_dir / "gomatrix.toml"
    if not toml.is_file():
        src = legacy / "gomatrix.toml"
        text = src.read_text(encoding="utf-8") if src.is_file() else _DEFAULT_TOML.format(port=port)
        write_text_atomic(toml, _rewrite_port(text, port))
        logger.info(f"matrix host: gomatrix.toml 已落到数据目录 {toml}")

    for name in _MIGRATE_FILES:
        if name == "gomatrix.toml":
            continue
        old, new = legacy / name, data_dir / name
        if old.is_file() and not new.exists():
            try:
                shutil.copy2(old, new)
                logger.info(f"matrix host: 迁移 {name} → {data_dir}")
            except OSError as exc:
                logger.warning(f"matrix host: 迁移 {name} 失败：{exc}")
    for name in _MIGRATE_DIRS:
        old, new = legacy / name, data_dir / name
        if old.is_dir() and not new.exists():
            try:
                shutil.copytree(old, new)
                logger.info(f"matrix host: 迁移 {name}/ → {data_dir}")
            except OSError as exc:
                logger.warning(f"matrix host: 迁移 {name}/ 失败：{exc}")
    purge_legacy_layout(legacy)
    return toml


def purge_legacy_layout(legacy: Path) -> None:
    """删除二进制旁旧 sidecar 数据（已归 ``<coara_home>/matrix/``，留着只会被误用）。"""
    for name in _MIGRATE_FILES:
        path = legacy / name
        if path.is_file():
            with contextlib.suppress(OSError):
                path.unlink()
                logger.info(f"matrix host: 已删除旧布局 {path.name}")
    media = legacy / "media"
    if media.is_dir():
        with contextlib.suppress(OSError):
            shutil.rmtree(media)
            logger.info("matrix host: 已删除旧布局 media/")


def _listener_exe(port: int) -> Path | None:
    """Return the executable path of the process listening on ``port``, if any."""
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port != port or conn.status != psutil.CONN_LISTEN:
                continue
            pid = conn.pid
            if pid is None:
                continue
            return Path(psutil.Process(pid).exe()).resolve()
    except (psutil.Error, OSError, ValueError):
        return None
    return None


def _looks_like_gomatrix(exe: Path | None, binary: Path | None = None) -> bool:
    """判断监听进程是否可确认为 gomatrix（只有自己人才允许动它）。

    确认途径二选一：与捆绑二进制路径相同；或可执行文件名含 gomatrix
    （覆盖另一安装路径的 coara 拉起的实例，避免两个 coara 互杀死循环）。
    无法确认的一律视为无关进程——宁可不管，绝不错杀。
    """
    if exe is None:
        return False
    if binary is not None:
        try:
            if exe.resolve() == binary.resolve():
                return True
        except OSError:
            pass
    return "gomatrix" in exe.name.lower()


async def is_healthy(port: int) -> bool:
    """探测端口上的 gomatrix 是否健康（Matrix client API 可达）。

    纯 loopback 探测必须 verify=False, trust_env=False：默认构造要加载
    Windows CA 证书库与代理环境（同步，实测 500ms+），每 10s 一次会把
    事件循环冻出输入卡顿；trust_env=True 还会把 127.0.0.1 送进代理。

    仅 status==200 不算健康——任何 HTTP 服务都能返回 200；必须响应体是
    含 "versions" 列表的 JSON（Matrix client /versions 特征）才判定为
    Matrix 服务，避免把无关服务误认为 gomatrix。
    """
    try:
        async with httpx.AsyncClient(verify=False, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}{_HEALTH_PATH}", timeout=_HEALTH_TIMEOUT)
            if resp.status_code != 200:
                return False
            data = resp.json()
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("versions"), list)


def _escape_toml_string(value: str) -> str:
    """TOML 基本字符串转义（反斜杠与双引号）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def apply_tunnel_config(toml: Path, matrix_cfg: Any) -> None:
    """把 coara 配置里的 tunnel 键写入数据目录 toml（隧道收编）。

    只动显式设置的键（``tunnel_enabled`` 非 None、其余非空）；未设置的键
    保持 toml 现状。每次 spawn 前调用，保证 coara 配置是事实源。
    """
    updates: dict[str, str] = {}
    enabled = getattr(matrix_cfg, "tunnel_enabled", None)
    if enabled is not None:
        updates["enabled"] = "true" if enabled else "false"
    for attr, key in (
        ("tunnel_mode", "mode"),
        ("tunnel_token", "token"),
        ("tunnel_public_url", "public_url"),
    ):
        value = str(getattr(matrix_cfg, attr, "") or "").strip()
        if value:
            updates[key] = f'"{_escape_toml_string(value)}"'
    if not updates or not toml.is_file():
        return

    lines = toml.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_tunnel = False
    seen: set[str] = set()
    found_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_tunnel:
                for key, val in updates.items():
                    if key not in seen:
                        out.append(f"{key} = {val}")
            in_tunnel = bool(re.fullmatch(r"\[\s*tunnel\s*\]", stripped))
            found_section = found_section or in_tunnel
            out.append(line)
            continue
        if in_tunnel and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key} = {updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    if in_tunnel:
        for key, val in updates.items():
            if key not in seen:
                out.append(f"{key} = {val}")
    elif not found_section:
        out.extend(["", "[tunnel]"] + [f"{key} = {val}" for key, val in updates.items()])
    write_text_atomic(toml, "\n".join(out) + "\n")


class GoMatrixHost:
    """gomatrix 生命周期托管：adopt-or-spawn + 看门狗 + 关停。"""

    def __init__(self, coara_home: Path | str, matrix_cfg: Any) -> None:
        self.coara_home = Path(coara_home)
        self.port = int(getattr(matrix_cfg, "port", 8008) or 8008)
        self._matrix_cfg = matrix_cfg
        self._binary: Path | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._managed = False
        self._watch_task: asyncio.Task[None] | None = None
        self._restart_count = 0
        self._stopping = False
        self._events: collections.deque[str] = collections.deque(maxlen=50)

    def _log_event(self, text: str) -> None:
        """记录一条面向用户的连接日志（手机接入页展示）。"""
        self._events.append(f"{time.strftime('%H:%M:%S')} {text}")

    @property
    def managed(self) -> bool:
        """True 表示实例由本进程拉起（关停时要带走）。"""
        return self._managed

    async def start(self) -> bool:
        """接入或拉起 gomatrix；成功（含 adopt）返回 True。"""
        binary = find_binary(self._matrix_cfg)
        if await is_healthy(self.port):
            listener = _listener_exe(self.port)
            if listener is not None and _looks_like_gomatrix(listener, binary):
                logger.info(f"matrix host: 端口 {self.port} 已有健康 gomatrix 实例（{listener}），接入（adopt）")
                self._log_event("接入已在运行的实例")
                self._binary = binary
                self._managed = False
                self._start_watchdog()
                return True
            if listener is not None:
                logger.error(
                    f"matrix host: 端口 {self.port} 被非 gomatrix 进程（{listener}）占用，"
                    "不会终止它。请用户自行释放端口或修改 coara 配置 matrix.port。"
                )
                self._log_event(f"端口 {self.port} 被 {listener.name} 占用，无法启动")
            else:
                logger.error(
                    f"matrix host: 端口 {self.port} 被占用且无法识别占用进程，"
                    "不会尝试终止。请用户自行排查或修改 matrix.port。"
                )
                self._log_event(f"端口 {self.port} 被占用且无法识别占用进程，无法启动")
            return False
        if binary is None:
            msg = "matrix host: 未找到 gomatrix 二进制，跳过托管启动"
            if _is_packaged_coara_install():
                logger.warning(msg)
            else:
                logger.debug(
                    f"{msg}（开发机：cd gomatrix && go build -o gomatrix.exe ./cmd/gomatrix；"
                    "不需要 Matrix 时在 config 设 matrix.host_enabled: false）"
                )
            self._log_event("未找到 gomatrix 二进制")
            return False
        self._binary = binary
        toml = ensure_data_dir(self.coara_home, binary, self.port)
        from src.matrix_host.credentials import ensure_matrix_bot_credentials

        ensure_matrix_bot_credentials(self.coara_home, port=self.port)
        rewrite_toml_port(toml, self.port)
        apply_tunnel_config(toml, self._matrix_cfg)
        if not await self._spawn(toml):
            return False
        self._managed = True
        self._start_watchdog()
        return True

    async def _spawn(self, toml: Path) -> bool:
        kwargs: dict[str, Any] = {
            "cwd": str(toml.parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            # gomatrix 托管化后已删 GUI/托盘，天然无头，不再传 -headless（会按未知参数退出 code=2）
            self._proc = await asyncio.create_subprocess_exec(str(self._binary), "--config", str(toml), **kwargs)
        except OSError as exc:
            logger.error(f"matrix host: 拉起失败：{exc}")
            return False
        deadline = time.monotonic() + _SPAWN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                logger.error(f"matrix host: 进程退出过早（code={self._proc.returncode}）")
                self._log_event(f"进程退出过早（code={self._proc.returncode}）")
                return False
            if await is_healthy(self.port):
                logger.info(f"matrix host: gomatrix 已启动（pid={self._proc.pid}, port={self.port}）")
                self._log_event(f"实例已启动（端口 {self.port}）")
                return True
            await asyncio.sleep(0.5)
        logger.error("matrix host: 启动后健康探测超时")
        self._log_event("启动后健康探测超时")
        await self._terminate_proc()
        return False

    def _start_watchdog(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop(), name="gomatrix-watchdog")

    async def _watch_loop(self) -> None:
        unhealthy = 0
        while not self._stopping:
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
            if self._stopping:
                return
            if self._managed and self._proc is not None and self._proc.returncode is not None:
                await self._restart("进程退出")
                continue
            if await is_healthy(self.port):
                unhealthy = 0
                continue
            unhealthy += 1
            if unhealthy < _UNHEALTHY_KILL_THRESHOLD:
                continue
            unhealthy = 0
            if self._managed:
                await self._restart(f"连续 {_UNHEALTHY_KILL_THRESHOLD} 次健康探测失败")
            else:
                # adopt 的外部实例死了：托管语义下由 coara 接管拉起（数据目录归一）
                logger.warning("matrix host: adopt 的实例失联，coara 接管拉起")
                self._log_event("外部实例失联，已接管拉起")
                self._managed = True
                await self._restart("外部实例失联")

    async def _restart(self, reason: str) -> None:
        if self._binary is None:
            # adopt 时就没找到二进制：接管无从谈起，看门狗放弃而非空转
            logger.error("matrix host: 无可用 gomatrix 二进制，无法接管重启，看门狗停止")
            self._managed = False
            return
        backoff = _RESTART_BACKOFFS[min(self._restart_count, len(_RESTART_BACKOFFS) - 1)]
        self._restart_count += 1
        logger.warning(f"matrix host: {reason}，{backoff:.0f}s 后重启（第 {self._restart_count} 次）")
        self._log_event(f"{reason}，{backoff:.0f}s 后重启（第 {self._restart_count} 次）")
        await self._terminate_proc()
        await asyncio.sleep(backoff)
        if self._stopping:
            return
        toml = ensure_data_dir(self.coara_home, self._binary, self.port)
        rewrite_toml_port(toml, self.port)
        apply_tunnel_config(toml, self._matrix_cfg)
        if await self._spawn(toml):
            self._restart_count = 0

    async def _terminate_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def stop(self) -> None:
        """关停：保留 gomatrix 作为持久服务，下次启动 adopt 复用同一进程与隧道。

        gomatrix 的 quick tunnel URL 随 cloudflared 重启而变化——coara 干净退出
        若把它杀掉，重啟时只能重新拉起，手机端被迫重新扫码。这里不终止托管实例，
        由下次 coara 启动时 adopt（同进程、同隧道）实现自动重连。
        """
        self._stopping = True
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        if self._managed and self._proc is not None and self._proc.returncode is None:
            logger.info("matrix host: coara 退出，保留 gomatrix 运行（下次启动 adopt 自动重连）")
            self._log_event("保留实例运行，重启后手机端自动重连")
