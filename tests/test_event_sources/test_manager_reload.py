"""Tests for EventSourceManager.reload() hot-swap semantics (#136).

reload 对 file_watch / poll 先 start 新源再 stop 旧源（双开窗口不丢事件，
重复事件由带锁 dedupe 吸收）；webhook 端口独占无法双开，退化为 stop→start。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from src.event_sources.manager import EventSourceManager
from src.event_sources.types import InboundEvent
from src.workspace.registry import WorkspaceRegistry
from src.workspace.types import WorkspaceEntry


def _wm(tmp_path: Path, *, name: str = "shop") -> SimpleNamespace:
    home = tmp_path / "home"
    home.mkdir()
    reg = WorkspaceRegistry(home)
    entry = WorkspaceEntry(id="ws-1", name=name, path=str(tmp_path / "shop"))
    (tmp_path / "shop").mkdir(exist_ok=True)
    reg.document.workspaces[entry.id] = entry
    reg.save()
    return SimpleNamespace(coara_home=home, registry=reg, vfs=SimpleNamespace())


def _write_defn(wm: SimpleNamespace, defn: dict[str, Any]) -> None:
    """写现行布局：``<ws>/.coara/matters/definitions/<id>.yaml``。"""
    entry = wm.registry.resolve_name_or_id(defn["workspace"])
    defs = Path(entry.path) / ".coara" / "matters" / "definitions"
    defs.mkdir(parents=True, exist_ok=True)
    (defs / f"{defn['id']}.yaml").write_text(
        yaml.safe_dump(defn, allow_unicode=True),
        encoding="utf-8",
    )


class _FakeWebhookServer:
    """Record webhook lifecycle calls; never binds a port."""

    def __init__(self, log: list[Any]) -> None:
        self.log = log
        self.is_running = False

    def register(self, defn) -> None:
        self.log.append(("wh_register", defn.id))

    def has_source(self, source_id: str) -> bool:
        return False

    def unregister_all(self) -> None:
        self.log.append("wh_unregister_all")

    async def start(self) -> None:
        self.log.append("wh_start")

    async def stop(self) -> None:
        self.log.append("wh_stop")


@pytest.mark.asyncio
async def test_reload_starts_new_source_before_stopping_old(tmp_path: Path, monkeypatch) -> None:
    """热替换：新 file_watch 先 start，旧源后 stop；webhook 走 stop→start 退化"""
    wm = _wm(tmp_path)
    log: list[Any] = []

    class FakeFileWatch:
        def __init__(self, defn, *, vfs, emit, loop) -> None:
            self.definition = defn
            self.emit = emit

        def start(self) -> None:
            log.append(("fw_start", id(self)))

        def stop(self) -> None:
            log.append(("fw_stop", id(self)))

    monkeypatch.setattr("src.event_sources.manager.FileWatchSource", FakeFileWatch)
    _write_defn(
        wm,
        {"id": "fw", "kind": "file_watch", "workspace": "shop", "watch_path": "."},
    )
    manager = EventSourceManager(wm, coara_id="test")
    manager._webhook_server = _FakeWebhookServer(log)

    await manager.start()
    old = manager._file_sources[0]
    await manager.reload()
    new = manager._file_sources[0]

    assert new is not old
    # 新源 start 先于旧源 stop（无监听空窗）；webhook 先摘注册停服务再随新源重建
    assert log == [
        ("fw_start", id(old)),
        "wh_start",  # 初次 start 的收尾
        "wh_unregister_all",
        "wh_stop",
        ("fw_start", id(new)),
        "wh_start",
        ("fw_stop", id(old)),
    ]


@pytest.mark.asyncio
async def test_reload_overlap_duplicate_event_absorbed_by_dedupe(tmp_path: Path, monkeypatch) -> None:
    """双开窗口新旧源各报一次同一文件事件：带锁 dedupe 只放行一次"""
    wm = _wm(tmp_path)
    captured: list[Any] = []

    class FakeFileWatch:
        def __init__(self, defn, *, vfs, emit, loop) -> None:
            self.definition = defn
            self.emit = emit
            captured.append(self)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr("src.event_sources.manager.FileWatchSource", FakeFileWatch)
    _write_defn(
        wm,
        {"id": "fw", "kind": "file_watch", "workspace": "shop", "watch_path": "."},
    )
    stored: list[Any] = []

    async def _on_stored(update) -> None:
        stored.append(update)

    manager = EventSourceManager(
        wm,
        coara_id="test",
        on_update_stored=_on_stored,
    )

    await manager.start()
    await manager.reload()
    assert len(captured) == 2  # 旧源与新源并存过（reload 后旧源才被停）
    old_emit, new_emit = captured[0].emit, captured[1].emit

    event = InboundEvent(
        source_id="fw",
        workspace="shop",
        event_type="file.created",
        payload={"path": str(tmp_path / "shop" / "a.txt")},
        dedupe_key="unused",
    )
    await old_emit(event)  # 旧源在双开窗口迟到的投递
    await new_emit(event)  # 新源对同一变动的重复投递
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_reload_failure_keeps_existing_sources_running(tmp_path: Path, monkeypatch) -> None:
    """失败的热重载只清理新源，旧源与 webhook 注册都能继续服务。"""
    wm = _wm(tmp_path)
    log: list[Any] = []

    class FakeFileWatch:
        def __init__(self, defn, *, vfs, emit, loop) -> None:
            self.definition = defn

        def start(self) -> None:
            pass

        def stop(self) -> None:
            log.append(("fw_stop", id(self)))

    monkeypatch.setattr("src.event_sources.manager.FileWatchSource", FakeFileWatch)
    _write_defn(wm, {"id": "fw", "kind": "file_watch", "workspace": "shop", "watch_path": "."})
    _write_defn(wm, {"id": "wh", "kind": "webhook", "workspace": "shop"})
    manager = EventSourceManager(wm, coara_id="test")

    class FailingReplacementWebhook(_FakeWebhookServer):
        starts = 0

        async def start(self) -> None:
            self.log.append("wh_start")
            FailingReplacementWebhook.starts += 1
            if FailingReplacementWebhook.starts == 2:
                raise RuntimeError("replacement failed")
            self.is_running = True

    manager._webhook_server = FailingReplacementWebhook(log)

    await manager.start()
    old = manager._file_sources[0]
    with pytest.raises(RuntimeError, match="replacement failed"):
        await manager.reload()

    assert manager._file_sources == [old]
    assert ("fw_stop", id(old)) not in log
    assert log[-2:] == [("wh_register", "wh"), "wh_start"]


@pytest.mark.asyncio
async def test_reload_failure_restores_the_previous_definition_snapshot(tmp_path: Path, monkeypatch) -> None:
    """A failed replacement cannot mismatch live sources with new definitions."""
    wm = _wm(tmp_path)

    class FakeFileWatch:
        starts = 0

        def __init__(self, defn, *, vfs, emit, loop) -> None:
            self.definition = defn

        def start(self) -> None:
            FakeFileWatch.starts += 1

        def stop(self) -> None:
            pass

    monkeypatch.setattr("src.event_sources.manager.FileWatchSource", FakeFileWatch)
    _write_defn(wm, {"id": "fw", "kind": "file_watch", "workspace": "shop", "watch_path": "old"})
    manager = EventSourceManager(wm, coara_id="test")
    await manager.start()

    _write_defn(wm, {"id": "fw", "kind": "file_watch", "workspace": "shop", "watch_path": "new"})

    async def _fail_start() -> None:
        raise RuntimeError("replacement failed")

    monkeypatch.setattr(manager, "_start_sources", _fail_start)
    with pytest.raises(RuntimeError, match="replacement failed"):
        await manager.reload()

    assert manager.definitions["fw"].watch_path == "old"
