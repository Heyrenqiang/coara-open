"""自动更新暂存侧测试：compute_diff 四态、增量暂存、断点续传、整包回退、路径安全。"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from src.cli import auto_update
from src.cli.auto_update import compute_diff


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeStream:
    def __init__(self, data: bytes, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_bytes(self, size: int):
        for i in range(0, len(self._data), size):
            yield self._data[i : i + size]


class _FakeClient:
    server: _FakeServer  # noqa: F821 — 类属性由 FakeServer 装配

    def __init__(self, **kwargs):
        self.timeout = kwargs.get("timeout")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url: str):
        type(self).server.get_urls.append(url)
        payload = type(self).server.json_map[url]
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload, type(self).server.status_map.get(url, 200))

    def stream(self, method: str, url: str):
        type(self).server.stream_urls.append(url)
        data = type(self).server.blob_map[url]
        if isinstance(data, Exception):
            raise data
        return _FakeStream(data, type(self).server.status_map.get(url, 200))


class _FakeServer:
    """内存 HTTP 假服务：json_map 喂 .get().json()，blob_map 喂 .stream()。"""

    def __init__(self):
        self.json_map: dict[str, object] = {}
        self.blob_map: dict[str, object] = {}
        self.status_map: dict[str, int] = {}
        self.get_urls: list[str] = []
        self.stream_urls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("httpx")
        client_cls = type("Client", (_FakeClient,), {"server": self})
        fake.Client = client_cls  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "httpx", fake)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """造安装目录与 TEMP，钉 sys.executable，装内存 httpx 假服务。"""
    install_dir = tmp_path / "install"
    (install_dir / "runtime").mkdir(parents=True)
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "bin" / "coara.cmd").write_text("@echo off", encoding="ascii")
    python = install_dir / "runtime" / "python.exe"
    python.write_bytes(b"")
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()

    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(auto_update.tempfile, "gettempdir", lambda: str(temp_dir))

    server = _FakeServer()
    server.install(monkeypatch)

    manifest_url = "https://coara.top/manifest.json"
    (install_dir / "update.json").write_text(
        json.dumps({"manifest_url": manifest_url, "sha256": "oldhash"}), encoding="utf-8"
    )
    return types.SimpleNamespace(
        install_dir=install_dir,
        temp_dir=temp_dir,
        server=server,
        manifest_url=manifest_url,
        base_url="https://coara.top/",
    )


def _new_version_manifest(zip_bytes: bytes, *, filelist: str | None = None) -> dict:
    plat: dict[str, str] = {"name": "coara-v1.0.32-win-x64.zip", "sha256": _sha(zip_bytes)}
    if filelist:
        plat["filelist"] = filelist
    return {"version": "1.0.32", "platforms": {"win-x64": plat}}


def _read_pending(env) -> dict:
    return json.loads((env.temp_dir / auto_update.PENDING_FILENAME).read_text(encoding="utf-8"))


# ---------- compute_diff 四态 ----------


def test_compute_diff_added_deleted_modified_unchanged():
    old = {
        "a.txt": {"sha256": "aaa", "size": 1},
        "b.txt": {"sha256": "bbb", "size": 1},
        "c.txt": {"sha256": "ccc", "size": 1},
    }
    new = {
        "a.txt": {"sha256": "aaa", "size": 1},  # 不变
        "b.txt": {"sha256": "zzz", "size": 2},  # 修改
        "d.txt": {"sha256": "ddd", "size": 1},  # 新增
    }
    download, delete = compute_diff(old, new)
    assert download == ["b.txt", "d.txt"]
    assert delete == ["c.txt"]


def test_compute_diff_empty_new_downloads_all_and_nothing_deleted():
    download, delete = compute_diff({}, {"x/y.py": {"sha256": "h"}})
    assert download == ["x/y.py"]
    assert delete == []


# ---------- 增量暂存 ----------


def _setup_incremental(env, *, changed: bytes = b"new-content", kept: bytes = b"same"):
    """远端 filelist：a.txt 不变、b.txt 修改、c.txt 新增、d.txt 删除。返回 (远端 filelist, zip bytes)。"""
    zip_bytes = b"fake-zip"
    remote_filelist = {
        "version": "1.0.32",
        "files": {
            "a.txt": {"sha256": _sha(kept), "size": len(kept)},
            "b.txt": {"sha256": _sha(changed), "size": len(changed)},
            "sub/c.txt": {"sha256": _sha(b"added"), "size": 5},
        },
    }
    (env.install_dir / auto_update.LOCAL_FILELIST_NAME).write_text(
        json.dumps(
            {
                "version": "1.0.31",
                "files": {
                    "a.txt": {"sha256": _sha(kept), "size": len(kept)},
                    "b.txt": {"sha256": "old-b", "size": 1},
                    "d.txt": {"sha256": "old-d", "size": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    env.server.json_map[env.manifest_url] = _new_version_manifest(zip_bytes, filelist="filelist-v1.0.32.json")
    env.server.json_map[env.base_url + "filelist-v1.0.32.json"] = remote_filelist
    env.server.blob_map[env.base_url + f"files/{_sha(changed)}"] = changed
    env.server.blob_map[env.base_url + f"files/{_sha(b'added')}"] = b"added"
    env.server.blob_map[env.base_url + "coara-v1.0.32-win-x64.zip"] = zip_bytes
    return remote_filelist, zip_bytes


def test_incremental_staging_files_mode(env):
    remote_filelist, _ = _setup_incremental(env)
    pending = auto_update.check_and_stage_update()

    assert pending is not None
    assert pending["mode"] == "files"
    assert pending["version"] == "1.0.32"
    assert pending["filelist_name"] == "filelist-v1.0.32.json"
    assert pending["deletes"] == ["d.txt"]
    assert pending["zip"].endswith("coara-v1.0.32-win-x64.zip")

    staging = Path(pending["staging_dir"])
    assert (staging / "b.txt").read_bytes() == b"new-content"
    assert (staging / "sub" / "c.txt").read_bytes() == b"added"
    assert json.loads((staging / "filelist-v1.0.32.json").read_text(encoding="utf-8")) == remote_filelist

    on_disk = _read_pending(env)
    assert on_disk == pending


def test_incremental_resume_skips_existing_files(env):
    """断点续传：目标已存在且哈希匹配则不再请求网络。"""
    _setup_incremental(env)
    staging = env.temp_dir / "coara-update-staging-v1.0.32"
    (staging / "sub").mkdir(parents=True)
    (staging / "sub" / "c.txt").write_bytes(b"added")

    pending = auto_update.check_and_stage_update()
    assert pending is not None and pending["mode"] == "files"
    added_url = env.base_url + f"files/{_sha(b'added')}"
    assert added_url not in env.server.stream_urls  # 已存在且匹配，未再下载
    assert env.base_url + f"files/{_sha(b'new-content')}" in env.server.stream_urls


def test_incremental_bad_hash_falls_back_to_zip(env):
    """下载哈希不匹配 → 清理暂存并回退整包（mode=zip）。"""
    _, zip_bytes = _setup_incremental(env)
    env.server.blob_map[env.base_url + f"files/{_sha(b'new-content')}"] = b"tampered"

    pending = auto_update.check_and_stage_update()
    assert pending is not None
    assert pending["mode"] == "zip"
    assert pending["sha256"] == _sha(zip_bytes)
    assert Path(pending["zip"]).read_bytes() == zip_bytes
    assert not (env.temp_dir / "coara-update-staging-v1.0.32").exists()  # 暂存已清理


def test_incremental_http_error_falls_back_to_zip(env):
    """远端 filelist 非 200 → 不采用增量，回退整包。"""
    zip_bytes = b"fake-zip"
    (env.install_dir / auto_update.LOCAL_FILELIST_NAME).write_text(json.dumps({"files": {}}), encoding="utf-8")
    env.server.json_map[env.manifest_url] = _new_version_manifest(zip_bytes, filelist="filelist-v1.0.32.json")
    env.server.json_map[env.base_url + "filelist-v1.0.32.json"] = {"version": "1.0.32", "files": {}}
    env.server.status_map[env.base_url + "filelist-v1.0.32.json"] = 404
    env.server.blob_map[env.base_url + "coara-v1.0.32-win-x64.zip"] = zip_bytes

    pending = auto_update.check_and_stage_update()
    assert pending is not None and pending["mode"] == "zip"


def test_no_local_filelist_goes_full_zip(env):
    """本地无 coara-filelist.json → 直接整包，不拉远端 filelist。"""
    zip_bytes = b"fake-zip"
    env.server.json_map[env.manifest_url] = _new_version_manifest(zip_bytes, filelist="filelist-v1.0.32.json")
    env.server.blob_map[env.base_url + "coara-v1.0.32-win-x64.zip"] = zip_bytes

    pending = auto_update.check_and_stage_update()
    assert pending is not None and pending["mode"] == "zip"
    assert env.base_url + "filelist-v1.0.32.json" not in env.server.get_urls


def test_manifest_without_filelist_goes_full_zip(env):
    """manifest 无 filelist 字段 → 整包路径，与旧行为一致（pending 带 mode=zip）。"""
    zip_bytes = b"fake-zip"
    env.server.json_map[env.manifest_url] = _new_version_manifest(zip_bytes)
    env.server.blob_map[env.base_url + "coara-v1.0.32-win-x64.zip"] = zip_bytes

    pending = auto_update.check_and_stage_update()
    assert pending is not None and pending["mode"] == "zip"


# ---------- 路径安全 ----------


def test_safe_rel_path_rejects_traversal_and_absolute():
    assert auto_update._safe_rel_path("../evil.txt") is None
    assert auto_update._safe_rel_path("sub/../../evil.txt") is None
    assert auto_update._safe_rel_path("/abs.txt") is None
    assert auto_update._safe_rel_path("sub\\evil.txt") is None
    assert auto_update._safe_rel_path("C:/evil.txt") is None
    ok = auto_update._safe_rel_path("bin/coara.cmd")
    assert ok is not None and str(ok).replace("\\", "/") == "bin/coara.cmd"


def test_malicious_entry_falls_back_to_zip(env):
    """filelist 含 ../ 条目 → 拒绝并回退整包，绝不写出越界文件。"""
    zip_bytes = b"fake-zip"
    (env.install_dir / auto_update.LOCAL_FILELIST_NAME).write_text(json.dumps({"files": {}}), encoding="utf-8")
    env.server.json_map[env.manifest_url] = _new_version_manifest(zip_bytes, filelist="filelist-v1.0.32.json")
    env.server.json_map[env.base_url + "filelist-v1.0.32.json"] = {
        "version": "1.0.32",
        "files": {"../evil.txt": {"sha256": _sha(b"evil"), "size": 4}},
    }
    env.server.blob_map[env.base_url + f"files/{_sha(b'evil')}"] = b"evil"
    env.server.blob_map[env.base_url + "coara-v1.0.32-win-x64.zip"] = zip_bytes

    pending = auto_update.check_and_stage_update()
    assert pending is not None and pending["mode"] == "zip"
    assert not (env.temp_dir / "evil.txt").exists()
    assert not (env.temp_dir / "coara-update-staging-v1.0.32").exists()


# ---------- 边界 ----------


def test_same_version_seeds_filelist_without_pending(env):
    """已是当前版本但没有本地清单时，只补存清单，不写 pending。"""
    manifest = _new_version_manifest(b"x", filelist="filelist-v1.0.32.json")
    manifest["version"] = "1.0.31"
    manifest["platforms"]["win-x64"]["sha256"] = "oldhash"
    env.server.json_map[env.manifest_url] = manifest
    remote = {"version": "1.0.31", "files": {"a.txt": {"sha256": "abc", "size": 1}}}
    env.server.json_map[env.base_url + "filelist-v1.0.32.json"] = remote

    assert auto_update.check_and_stage_update() is None
    saved = json.loads((env.install_dir / auto_update.LOCAL_FILELIST_NAME).read_text(encoding="utf-8"))
    assert saved == remote
    assert not (env.temp_dir / auto_update.PENDING_FILENAME).exists()


def test_same_hash_no_update(env):
    env.server.json_map[env.manifest_url] = _new_version_manifest(b"x")
    env.server.json_map[env.manifest_url]["platforms"]["win-x64"]["sha256"] = "oldhash"
    assert auto_update.check_and_stage_update() is None
    assert not (env.temp_dir / auto_update.PENDING_FILENAME).exists()


def test_manifest_network_failure_returns_none(env):
    env.server.json_map[env.manifest_url] = ConnectionError("offline")
    assert auto_update.check_and_stage_update() is None
