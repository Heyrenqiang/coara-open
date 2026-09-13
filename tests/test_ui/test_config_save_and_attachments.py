"""Config save strip + web chat attachment ref resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.coara_home import system_dir_for_home
from src.core.config import ConfigManager, reset_config_manager_for_tests
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.web_server import WebServer


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config_manager_for_tests()
    yield
    reset_config_manager_for_tests()


@pytest.fixture
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    monkeypatch.setattr("src.core.coara_home._iter_coara_home_env_values", lambda: [str(home)])
    return home


@pytest.mark.asyncio
async def test_save_config_strips_providers_domain_and_underscore_keys(
    coara_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_dir = system_dir_for_home(coara_home)
    system_dir.mkdir(parents=True)
    config_path = system_dir / "config.yaml"
    config_path.write_text(
        "log_level: INFO\nproviders:\n  stale:\n    base_url: http://old\nmatrix:\n  user: old\n",
        encoding="utf-8",
    )
    (system_dir / "providers.yaml").write_text(
        "providers:\n  kept:\n    base_url: http://p\n    api_key_env: K\n",
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    manager = ConfigManager()
    await manager.load()

    handlers = DashboardRestHandlers.__new__(DashboardRestHandlers)
    await handlers._save_config_payload(
        {
            "log_level": "DEBUG",
            "providers": {"stale": {"base_url": "http://old"}},
            "llm_profiles": {"x": {"provider": "kept", "model": "m"}},
            "matrix": {"user": "u", "_display_note": "x"},
        }
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["log_level"] == "DEBUG"
    assert "providers" not in saved
    assert "llm_profiles" not in saved
    assert saved["matrix"]["user"] == "u"
    assert "_display_note" not in saved["matrix"]

    providers = yaml.safe_load((system_dir / "providers.yaml").read_text(encoding="utf-8"))
    assert "kept" in providers["providers"]
    assert "stale" not in providers.get("providers", {})


@pytest.mark.asyncio
async def test_resolve_image_refs_skips_non_images(tmp_path: Path) -> None:
    uploads = tmp_path / ".coara" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "note.txt").write_text("hello", encoding="utf-8")
    (uploads / "pic.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path

    blocks = await server._resolve_image_refs(["note.txt", "pic.png", "missing.png"])
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_append_text_file_refs_inlines_text(tmp_path: Path) -> None:
    uploads = tmp_path / ".coara" / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "note.txt").write_text("line1\nline2", encoding="utf-8")
    (uploads / "blob.bin").write_bytes(b"\x00\x01\x02")

    server = WebServer.__new__(WebServer)
    server.workspace_dir = tmp_path

    out = await server._append_text_file_refs("请看附件", ["note.txt", "blob.bin"])
    assert "请看附件" in out
    assert "--- 附件: note.txt ---" in out
    assert "line1" in out and "line2" in out
    # 二进制（blob.bin）不内联内容，但给绝对路径提示供模型经工具处理
    assert "--- 附件: blob.bin ---" in out
    assert "二进制文件" in out
    assert "read/shell" in out
