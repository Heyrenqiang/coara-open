"""后台自动更新：内核就绪后查新版 → 下载 + SHA256 校验 → 落 pending 标记。

设计边界：启动路径零网络依赖、运行中绝不替换文件。
- 检查与下载在后台线程做，任何失败静默（下次启动再试）。
- 下载校验通过后写 ``%TEMP%/coara-update-pending.json``（zip 路径/hash/version/
  manifest_url），实际装包由 coara.cmd / coara-tray.cmd 下次启动时本地完成
  （Check-Update.ps1 -ApplyPending，无网络、有进度窗）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.core.logger import logger

PENDING_FILENAME = "coara-update-pending.json"
_DOWNLOAD_TIMEOUT_S = 30.0
_MANIFEST_TIMEOUT_S = 8.0


def _install_dir() -> Path | None:
    """安装根目录：<InstallDir>/runtime/python.exe → 上两级。源码环境返回 None。"""
    exe = Path(sys.executable).resolve()
    if exe.parent.name.lower() == "runtime" and (exe.parent.parent / "bin" / "coara.cmd").is_file():
        return exe.parent.parent
    return None


def _read_update_json(install_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((install_dir / "update.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data.get("manifest_url") or not data.get("sha256"):
        return None
    return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pending_path() -> Path:
    return Path(tempfile.gettempdir()) / PENDING_FILENAME


def check_and_stage_update() -> dict[str, Any] | None:
    """查新版并下载校验。返回 pending 信息（有新版且就绪）或 None。

    全程静默：网络失败/无新版/源码环境一律返回 None，不打扰启动与运行。
    """
    install_dir = _install_dir()
    if install_dir is None:
        return None
    current = _read_update_json(install_dir)
    if current is None:
        return None

    try:
        import httpx

        with httpx.Client(timeout=_MANIFEST_TIMEOUT_S, follow_redirects=True) as http:
            manifest = http.get(str(current["manifest_url"])).json()
    except Exception:
        return None

    plat = (manifest.get("platforms") or {}).get("win-x64") or manifest.get("bundle") or {}
    remote_hash = str(plat.get("sha256") or "").lower()
    name = str(plat.get("name") or "")
    version = str(manifest.get("version") or "")
    if not remote_hash or not name or remote_hash == str(current["sha256"]).lower():
        return None

    # manifest.json 在仓库 raw，附件在 Release：url = releases/download/<tag>/<name>。
    tag = version if version.startswith("v") else f"v{version}"
    # https://gitee.com/<owner>/<repo>/raw/master/manifest.json → 仓库基址
    repo_base = str(current["manifest_url"]).split("/raw/", 1)[0]

    def _download(name_: str, expect_hash: str) -> Path | None:
        url_ = f"{repo_base}/releases/download/{tag}/{name_}"
        tmp = Path(tempfile.gettempdir()) / name_
        try:
            if not (tmp.is_file() and _sha256(tmp) == expect_hash):
                import httpx

                with (
                    httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S * 20, follow_redirects=True) as http,
                    http.stream("GET", url_) as resp,
                    tmp.open("wb") as f,
                ):
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
                if _sha256(tmp) != expect_hash:
                    tmp.unlink(missing_ok=True)
                    return None
        except Exception:
            tmp.unlink(missing_ok=True)
            return None
        return tmp

    tmp_zip = _download(name, remote_hash)
    if tmp_zip is None:
        return None

    # ext 包（gomatrix/cloudflared）一并下载校验，启动时与主包一起装
    ext = (manifest.get("platforms") or {}).get("win-x64-ext") or {}
    ext_name = str(ext.get("name") or "")
    ext_hash = str(ext.get("sha256") or "").lower()
    ext_zip = ""
    if ext_name and ext_hash:
        ext_path = _download(ext_name, ext_hash)
        if ext_path is None:
            tmp_zip.unlink(missing_ok=True)
            return None
        ext_zip = str(ext_path)

    pending = {
        "zip": str(tmp_zip),
        "sha256": remote_hash,
        "ext_zip": ext_zip,
        "ext_sha256": ext_hash if ext_zip else "",
        "version": version,
        "manifest_url": str(current["manifest_url"]),
    }
    try:
        _pending_path().write_text(json.dumps(pending), encoding="utf-8")
    except OSError:
        return None
    logger.info(f"auto-update staged: v{version} ready at {tmp_zip}")
    return pending


def schedule_background_update(*, on_staged: Any | None = None) -> None:
    """内核就绪后调用：后台线程查新版，就绪后经 on_staged 提示（不阻塞、不应用）。"""
    if os.environ.get("COARA_NO_UPDATE") == "1":
        return
    import threading

    def _worker() -> None:
        try:
            pending = check_and_stage_update()
        except Exception as exc:
            logger.debug(f"auto-update check failed: {exc}")
            return
        if pending and on_staged is not None:
            with contextlib.suppress(Exception):
                on_staged(pending)

    threading.Thread(target=_worker, name="coara-auto-update", daemon=True).start()
