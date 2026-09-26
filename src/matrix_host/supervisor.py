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
  最终负责实例存活）。adopt 接管按端口终止占位 gomatrix（只动可确认为
  gomatrix 的进程，绝不误杀无关监听）。
- 隧道公网可达性看门狗：端口健康不代表隧道活着（quick tunnel 会被 CF 侧
  回收而 gomatrix 进程无恙）。看门狗定期从公网视角 GET 隧道 URL 的
  /_matrix/client/versions——仅硬死信号（NXDOMAIN / CF 530）在本机能上网
  时累计，满 2 次重启整个 gomatrix（连带重拉隧道）；超时/抖动一律不定，
  断网中硬死也不重启（不白耗退避）。隧道重建后 URL 变更，手机端需重新
  扫码（quick tunnel 固有属性）。
- coara 关停时只终止自己拉起的进程。

断电/断网自愈：开机自启见 ``src/cli/autostart.py``（coara autostart on|off|status）。
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

# 隧道公网可达性探测：端口健康不代表隧道活着（quick tunnel 会被 CF 侧
# 回收而 gomatrix 进程无恙），看门狗须独立验证隧道真能从公网访问。
_TUNNEL_PROBE_TIMEOUT = 8.0
_TUNNEL_PROBE_EVERY_TICKS = 6  # 每 6 个 watch tick（60s）探一次隧道
_TUNNEL_SPAWN_GRACE_SECONDS = 120.0  # spawn 后宽限期，隧道拉起要时间
# NXDOMAIN，收紧只慢不错。
_TUNNEL_DEAD_THRESHOLD = 2
_INTERNET_PROBE_URL = "https://www.cloudflare.com"
_INTERNET_PROBE_TIMEOUT = 5.0

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
    proc = _listener_process(port)
    if proc is None:
        return None
    try:
        return Path(proc.exe()).resolve()
    except (psutil.Error, OSError, ValueError):
        return None


def _listener_process(port: int) -> psutil.Process | None:
    """Return the confirmed-gomatrix process listening on ``port``, if any.

    只返回可确认为 gomatrix 的进程（_looks_like_gomatrix 判据）——调用方
    拿它做 terminate，无关进程绝不能落到终止路径上。
    """
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port != port or conn.status != psutil.CONN_LISTEN:
                continue
            pid = conn.pid
            if pid is None:
                continue
            proc = psutil.Process(pid)
            try:
                exe = Path(proc.exe()).resolve()
            except (psutil.Error, OSError, ValueError):
                continue
            if _looks_like_gomatrix(exe, None):
                return proc
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
            # 有意静默：路径 resolve 失败只是无法走「同路径」确认，
            # 回落到下面的按文件名判断
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


async def _dashboard_status(port: int) -> dict | None:
    """读本地 gomatrix dashboard 状态（tunnel_enabled/tunnel_state/tunnel_url）。

    与 is_healthy 同纪律：loopback 必须 verify=False, trust_env=False。
    拿不到（gomatrix 未就绪/接口异常）返回 None，调用方按「本轮不探」处理。
    """
    try:
        async with httpx.AsyncClient(verify=False, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/api/dashboard/status", timeout=_HEALTH_TIMEOUT)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def _tunnel_probe(tunnel_url: str) -> bool | None:
    """从公网视角探隧道可达性，三态返回。

    - True：可达（200 且响应是 Matrix /versions 特征，与 is_healthy 同判据，
      防 CF 错误页返回 200 误判活）
    - False：硬死信号——DNS 解析失败（NXDOMAIN，记录已被回收）或 CF 隧道
      错误状态码（530/1033 等 origin 不可达）；是否据此重启由 ``_check_tunnel``
      结合 ``_internet_up`` 裁定（断网中不重启）
    - None：不确定（超时/连接重置/非 Matrix 200 等）——一律不下结论，不触发重启
    """
    try:
        async with httpx.AsyncClient(verify=False, trust_env=False, timeout=_TUNNEL_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{tunnel_url.rstrip('/')}{_HEALTH_PATH}")
    except httpx.ConnectError as exc:
        # getaddrinfo failed = DNS 查无此域，quick tunnel 记录已被回收，确定死
        if "getaddrinfo" in str(exc).lower() or "name or service not known" in str(exc).lower():
            return False
        return None
    except Exception:
        return None
    if resp.status_code in (502, 503, 530, 1033):
        return False
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    # 200 但非 Matrix 特征（劫持落地页/异常代理）→ 不确定，不据此判死
    if isinstance(data, dict) and isinstance(data.get("versions"), list):
        return True
    return None


async def _internet_up() -> bool:
    """参考探测本机能否上公网：硬死信号仅在能上网时才触发重启。

    断网期间重启 gomatrix 无意义（新隧道也拉不起来），且白耗退避档位。
    """
    try:
        async with httpx.AsyncClient(verify=False, trust_env=False, timeout=_INTERNET_PROBE_TIMEOUT) as client:
            resp = await client.head(_INTERNET_PROBE_URL)
            return resp.status_code < 500
    except Exception:
        return False


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
                # adopt 的实例可能由另一端拉起：反代上游与本端不一致时接管重拉，
                # 否则手机端 /coara-api 反代静默 502（双版本并存实测坑）
                if await self._adopted_upstream_mismatch():
                    logger.warning("matrix host: adopt 实例反代上游与本端不一致，接管重拉")
                    self._log_event("实例反代上游不一致，已接管重拉")
                    self._managed = True
                    await self._restart("adopt 实例反代上游不匹配")
                else:
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

    def _coara_api_env(self) -> dict[str, str]:
        """拉起 gomatrix 时注入本端 coara web 反代上游（端口 + dashboard token 文件）。

        不注入则 gomatrix 吃默认 8080 + $COARA_HOME/system/dashboard_token——
        双版本并存或旧进程残留时反代指错端，手机 /coara-api 静默 502。
        """
        import os

        port = int(os.environ.get("COARA_WEB_PORT", "8080") or "8080")
        return {
            "GOMAX_COARA_API_ENABLED": "true",
            "GOMAX_COARA_API_PORT": str(port),
            "GOMAX_COARA_API_TOKEN_FILE": str(self.coara_home / "system" / "dashboard_token"),
            "COARA_HOME": str(self.coara_home),
        }

    async def _adopted_upstream_mismatch(self) -> bool:
        """adopt 实例的反代上游是否与本端不一致。

        当前 gomatrix 的 /api/dashboard/status 必带 coara_api 字段（Go struct
        固定序列化）；缺该字段=旧版/异版二进制，其反代不存在或指向默认
        8080——双安装并存时手机端 /coara-api 静默 502。旧版一律按不一致
        处理（接管重拉），不再「信息缺失按一致」放过。
        接口不可达（status=None）仍按一致处理——那是探测失败不是版本证据。
        """
        status = await _dashboard_status(self.port)
        if status is None:
            return False
        upstream = status.get("coara_api")
        if not isinstance(upstream, dict):
            return True
        import os

        port = int(os.environ.get("COARA_WEB_PORT", "8080") or "8080")
        if int(upstream.get("upstream_port") or 0) != port:
            return True
        # token 文件不一致同样会 401：反代拿着别的 coara_home 的 token 打本端
        token_file = str(upstream.get("token_file") or "").strip()
        if token_file:
            expected = str(self.coara_home / "system" / "dashboard_token")
            if token_file != expected:
                return True
        return False

    async def _spawn(self, toml: Path) -> bool:
        kwargs: dict[str, Any] = {
            "cwd": str(toml.parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        env = {**os.environ, **self._coara_api_env()}
        try:
            # gomatrix 托管化后已删 GUI/托盘，天然无头，不再传 -headless（会按未知参数退出 code=2）
            self._proc = await asyncio.create_subprocess_exec(
                str(self._binary), "--config", str(toml), env=env, **kwargs
            )
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
        tick = 0
        tunnel_dead = 0
        spawned_at = time.monotonic()
        while not self._stopping:
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
            tick += 1
            if self._stopping:
                return
            if self._managed and self._proc is not None and self._proc.returncode is not None:
                await self._restart("进程退出")
                spawned_at = time.monotonic()
                tunnel_dead = 0
                continue
            if not await is_healthy(self.port):
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
                spawned_at = time.monotonic()
                tunnel_dead = 0
                continue
            unhealthy = 0
            # 隧道公网可达性：端口健康不代表隧道活着，定期从公网视角验证
            if tick % _TUNNEL_PROBE_EVERY_TICKS != 0:
                continue
            if time.monotonic() - spawned_at < _TUNNEL_SPAWN_GRACE_SECONDS:
                continue
            verdict = await self._check_tunnel()
            if verdict is True:
                tunnel_dead = 0
            elif verdict is False:
                tunnel_dead += 1
                if tunnel_dead >= _TUNNEL_DEAD_THRESHOLD:
                    tunnel_dead = 0
                    await self._restart("隧道公网不可达")
                    self._log_event("隧道已重建，手机端需重新扫码")
                    spawned_at = time.monotonic()
            # verdict None = 断网中或探测不定，不计数不重启，等下轮

    async def _check_tunnel(self) -> bool | None:
        """三态判隧道公网可达性：True 活 / False 确定死 / None 本轮不下结论。

        - 超时/抖动（probe None）一律不定——瞬时超时 ≠ 隧道真死，否则会误重启并逼手机重扫
        - 硬死信号（NXDOMAIN/530）仅在本机能上网时判死；断网中返回 None，不白耗退避
        """
        status = await _dashboard_status(self.port)
        if status is None:
            return None
        if not status.get("tunnel_enabled"):
            return True  # 未启用隧道，无需探（视为健康，不参与重启判据）
        if status.get("tunnel_state") not in ("ready", None) and not status.get("tunnel_url"):
            return None  # 隧道还在拉起中
        url = str(status.get("tunnel_url") or "").strip()
        if not url:
            return None
        probe = await _tunnel_probe(url)
        if probe is True:
            return True
        if probe is False:
            # 硬死 + 能上网 → 真该死；硬死但整机断网 → 不定（新隧道也拉不起来）
            if await _internet_up():
                return False
            return None
        return None

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
        # adopt 的外部实例：_proc 为 None，进程还占着端口，须按端口杀掉才能重拉
        await self._kill_port_listener()
        await asyncio.sleep(backoff)
        if self._stopping:
            return
        toml = ensure_data_dir(self.coara_home, self._binary, self.port)
        rewrite_toml_port(toml, self.port)
        apply_tunnel_config(toml, self._matrix_cfg)
        if await self._spawn(toml):
            self._restart_count = 0

    async def _kill_port_listener(self) -> None:
        """按端口终止占位的 gomatrix 监听进程（adopt 实例接管场景）。

        只动可确认为 gomatrix 的进程（_looks_like_gomatrix 判据）——无关
        进程占端口时宁可重拉失败也不误杀。
        """
        proc = _listener_process(self.port)
        if proc is None:
            return
        try:
            logger.info(f"matrix host: 终止占用端口 {self.port} 的 gomatrix 进程（pid={proc.pid}）")
            proc.terminate()
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

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
