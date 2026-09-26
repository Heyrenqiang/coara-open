"""后台自动更新：内核就绪后查新版 → 下载 + SHA256 校验 → 落 pending 标记 设计边界：启动路径零网络依赖、
运行中绝不替换文件 -"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from src.core.logger import logger

PENDING_FILENAME = "coara-update-pending.json"
LOCAL_FILELIST_NAME = "coara-filelist.json"
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


def _read_local_filelist(install_dir: Path) -> dict[str, Any] | None:
    """读 InstallDir/coara-filelist.json（当前已装版本的文件清单），缺失或不可解析返回 None"""
    try:
        data = json.loads((install_dir / LOCAL_FILELIST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data.get("files"), dict):
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


def _staging_dir(version: str) -> Path:
    return Path(tempfile.gettempdir()) / f"coara-update-staging-v{version}"


def compute_diff(old_files: dict[str, Any], new_files: dict[str, Any]) -> tuple[list[str], list[str]]:
    """比对新旧 filelist 的 files 表，返回 (需下载的相对路径列表, 需删除的相对路径列表)

    新有旧无或两边 sha256 不同 → 下载；旧有新无 → 删除；其余不动
    """
    download: list[str] = []
    for path, meta in new_files.items():
        old_meta = old_files.get(path)
        new_hash = str((meta or {}).get("sha256") or "").lower()
        old_hash = str((old_meta or {}).get("sha256") or "").lower() if isinstance(old_meta, dict) else ""
        if not old_hash or old_hash != new_hash:
            download.append(path)
    delete = [path for path in old_files if path not in new_files]
    return sorted(download), sorted(delete)


def _safe_rel_path(rel: str) -> Path | None:
    """校验 filelist 相对路径，防路径穿越；非法返回 None

    只接受正斜杠相对路径：拒绝绝对路径、盘符、反斜杠与 ``..`` 段
    """
    if not rel or "\\" in rel or ":" in rel:
        return None
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return Path(*pure.parts)


def _download_to(url: str, dest: Path, expect_hash: str, *, timeout: float) -> bool:
    """下载 url 到 dest 并校验 sha256；dest 已存在且哈希匹配则跳过（断点续传）

    先写入旁路文件，哈希通过后再替换，避免半截内容盖掉已校验过的文件
    """
    if dest.is_file() and _sha256(dest) == expect_hash:
        return True
    partial = dest.with_name(dest.name + ".partial")
    try:
        import httpx

        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=timeout, follow_redirects=True) as http, http.stream("GET", url) as resp:
            resp.raise_for_status()
            with partial.open("wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
        if _sha256(partial) != expect_hash:
            partial.unlink(missing_ok=True)
            return False
        partial.replace(dest)
    except Exception:
        partial.unlink(missing_ok=True)
        return False
    return True


def _write_pending(pending: dict[str, Any]) -> None:
    path = _pending_path()
    tmp = path.with_suffix(".json.partial")
    tmp.write_text(json.dumps(pending), encoding="utf-8")
    tmp.replace(path)


def _stage_files(
    *, base_url: str, filelist_name: str, version: str, local_files: dict[str, Any]
) -> tuple[Path, list[str], list[str]]:
    """增量暂存：拉远端 filelist → 比对 → 下载变化文件 → filelist 本体入暂存目录

    返回 (staging_dir, downloaded, deletes)；任一步失败抛异常，由调用方清理并回退整包
    """
    if _safe_rel_path(filelist_name) is None:
        raise ValueError(f"unsafe filelist name: {filelist_name}")
    import httpx

    with httpx.Client(timeout=_MANIFEST_TIMEOUT_S, follow_redirects=True) as http:
        resp = http.get(base_url + filelist_name)
        resp.raise_for_status()
        remote_filelist = resp.json()
    remote_files = remote_filelist.get("files")
    if not isinstance(remote_files, dict):
        raise ValueError("remote filelist missing files table")
    remote_ver = str(remote_filelist.get("version") or "")
    if remote_ver != version:
        raise ValueError(f"filelist version {remote_ver} != {version}")

    download, deletes = compute_diff(local_files, remote_files)
    staging = _staging_dir(version)
    fetched: list[str] = []
    for rel in download:
        dest_rel = _safe_rel_path(rel)
        meta = remote_files.get(rel)
        expect_hash = str((meta or {}).get("sha256") or "").lower()
        if dest_rel is None or not expect_hash:
            raise ValueError(f"unsafe or incomplete filelist entry: {rel}")
        url = f"{base_url}files/{expect_hash}"
        if not _download_to(url, staging / dest_rel, expect_hash, timeout=_DOWNLOAD_TIMEOUT_S * 20):
            raise OSError(f"download failed: {rel}")
        fetched.append(rel)
    for rel in deletes:
        if _safe_rel_path(rel) is None:
            raise ValueError(f"unsafe delete entry: {rel}")

    # filelist 本体入暂存目录，应用侧换装后据它写 InstallDir/coara-filelist.json
    staging.mkdir(parents=True, exist_ok=True)
    (staging / filelist_name).write_text(json.dumps(remote_filelist, ensure_ascii=False), encoding="utf-8")
    return staging, fetched, deletes


def _seed_local_filelist(install_dir: Path, base_url: str, filelist_name: str, version: str) -> None:
    """已是当前版本、但安装目录还没有清单时，把远端清单存下来。

    换装脚本要等下一版才跑到新代码，所以清单不能只靠换装成功后写入。
    """
    if _safe_rel_path(filelist_name) is None:
        raise ValueError(f"unsafe filelist name: {filelist_name}")
    import httpx

    with httpx.Client(timeout=_MANIFEST_TIMEOUT_S, follow_redirects=True) as http:
        resp = http.get(base_url + filelist_name)
        resp.raise_for_status()
        remote_filelist = resp.json()
    if str(remote_filelist.get("version") or "") != version:
        raise ValueError("filelist version does not match the installed release")
    if not isinstance(remote_filelist.get("files"), dict) or not remote_filelist["files"]:
        raise ValueError("remote filelist missing files table")
    dest = install_dir / LOCAL_FILELIST_NAME
    tmp = dest.with_suffix(".json.partial")
    tmp.write_text(json.dumps(remote_filelist, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)


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
            resp = http.get(str(current["manifest_url"]))
            resp.raise_for_status()
            manifest = resp.json()
    except Exception:
        return None

    plat = (manifest.get("platforms") or {}).get("win-x64") or manifest.get("bundle") or {}
    remote_hash = str(plat.get("sha256") or "").lower()
    name = str(plat.get("name") or "")
    version = str(manifest.get("version") or "")
    if not remote_hash or not name:
        return None

    # 附件与 manifest 同目录（coara.top 站点根）：url = <manifest 目录>/<name>。
    base_url = str(current["manifest_url"]).rsplit("/", 1)[0] + "/"
    filelist_name = str(plat.get("filelist") or "")
    if remote_hash == str(current["sha256"]).lower():
        if filelist_name and _read_local_filelist(install_dir) is None:
            try:
                _seed_local_filelist(install_dir, base_url, filelist_name, version)
                logger.info(f"auto-update: saved filelist baseline for v{version}")
            except Exception as exc:
                logger.info(f"auto-update: filelist baseline not saved: {exc}")
        return None
    local_filelist = _read_local_filelist(install_dir) if filelist_name else None
    if local_filelist is not None:
        try:
            staging, downloaded, deletes = _stage_files(
                base_url=base_url,
                filelist_name=filelist_name,
                version=version,
                local_files=local_filelist["files"],
            )
        except Exception as exc:
            logger.info(f"auto-update: incremental staging failed, fallback to full zip: {exc}")
            shutil.rmtree(_staging_dir(version), ignore_errors=True)
        else:
            # zip/sha256 仍填（整包回退用）：zip 给远端整包 URL，应用侧需回退时可按它重下
            pending = {
                "mode": "files",
                "version": version,
                "manifest_url": str(current["manifest_url"]),
                "staging_dir": str(staging),
                "filelist_name": filelist_name,
                "deletes": deletes,
                "zip": base_url + name,
                "sha256": remote_hash,
                "ext_zip": "",
                "ext_sha256": "",
            }
            try:
                _write_pending(pending)
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)
                return None
            logger.info(f"auto-update staged: v{version} incremental ({len(downloaded)} files) at {staging}")
            return pending

    def _download(name_: str, expect_hash: str) -> Path | None:
        tmp = Path(tempfile.gettempdir()) / name_
        return tmp if _download_to(base_url + name_, tmp, expect_hash, timeout=_DOWNLOAD_TIMEOUT_S * 20) else None

    tmp_zip = _download(name, remote_hash)
    if tmp_zip is None:
        return None

    # 单包发布：gomatrix/cloudflared 已在主包 bin/ 里，只下载主包
    pending = {
        "mode": "zip",
        "zip": str(tmp_zip),
        "sha256": remote_hash,
        "ext_zip": "",
        "ext_sha256": "",
        "version": version,
        "manifest_url": str(current["manifest_url"]),
    }
    try:
        _write_pending(pending)
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
