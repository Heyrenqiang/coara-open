"""records 双域落盘统一走 write_text_atomic 原子写"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.records import agent_index, agent_store, user_store
from src.records.agent_store import MemoryStore
from src.records.agent_store import build_entry as build_memory
from src.records.user_store import CollectionStore
from src.records.user_store import build_entry as build_collection


def _spy_atomic(monkeypatch: pytest.MonkeyPatch, module: object) -> list[Path]:
    calls: list[Path] = []
    real = module.write_text_atomic  # type: ignore[attr-defined]

    def spy(path: Path, text: str) -> None:
        calls.append(Path(path))
        real(path, text)

    monkeypatch.setattr(module, "write_text_atomic", spy)  # type: ignore[attr-defined]
    return calls


async def test_memory_store_writes_via_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store_calls = _spy_atomic(monkeypatch, agent_store)
    index_calls = _spy_atomic(monkeypatch, agent_index)

    store = MemoryStore(tmp_path / "memory")
    entry = build_memory(content="断电安全笔记", title="原子写")
    await store.write(entry)
    assert store_calls and index_calls
    assert any(p.suffix == ".md" for p in store_calls)
    assert any(p.name == "index.yaml" for p in index_calls)

    # touch / archive / unarchive 也全部走原子写
    store_calls.clear()
    await store.touch(entry.id)
    assert store_calls

    store_calls.clear()
    assert await store.archive(entry.id)
    assert store_calls

    store_calls.clear()
    assert await store.unarchive(entry.id)
    assert store_calls


async def test_memory_store_replace_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟断电：os.replace 抛异常时原条目内容不变 无临时文件残留"""
    store = MemoryStore(tmp_path / "memory")
    entry = build_memory(content="原始内容", title="保持")
    await store.write(entry)
    entry_path = store.root / entry.path
    before = entry_path.read_text(encoding="utf-8")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated power cut")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated power cut"):
        await store.update(entry.id, {"content": "新内容"})

    assert entry_path.read_text(encoding="utf-8") == before
    assert not list(store.root.rglob("*.tmp"))


async def test_collection_store_writes_via_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_atomic(monkeypatch, user_store)

    store = CollectionStore(tmp_path / "knowledge")
    entry = build_collection(title="收藏", summary="摘要", content="正文")
    await store.write(entry)
    assert any(p.suffix == ".md" for p in calls)
    assert any(p.name == "index.yaml" for p in calls)

    calls.clear()
    await store.touch(entry.id)
    assert calls


def test_write_digest_via_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """digest 直写收编到 write_text_atomic：唯一临时名 + fsync + replace"""
    calls = _spy_atomic(monkeypatch, agent_store)

    store = MemoryStore(tmp_path / "memory")
    path = store.write_digest("2026-08-09", "# 概况")

    assert calls == [path]
    assert path.read_text(encoding="utf-8") == "# 概况\n"
    assert not list(store.root.rglob("*.tmp"))


def test_write_digest_replace_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟断电：os.replace 抛异常时原 digest 内容不变 无临时文件残留"""
    store = MemoryStore(tmp_path / "memory")
    path = store.write_digest("2026-08-09", "旧内容")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated power cut")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated power cut"):
        store.write_digest("2026-08-09", "新内容")

    assert path.read_text(encoding="utf-8") == "旧内容\n"
    assert not list(store.root.rglob("*.tmp"))


def test_write_digest_temp_files_use_unique_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """临时文件走 mkstemp 唯一名：写入中途崩溃留下的同名垃圾不再被后续写入互踩"""
    store = MemoryStore(tmp_path / "memory")
    seen_temps: list[str] = []
    real_replace = os.replace

    def spy_replace(src: object, dst: object) -> None:
        seen_temps.append(Path(str(src)).name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    store.write_digest("2026-08-09", "第一次")
    store.write_digest("2026-08-09", "第二次")

    assert len(seen_temps) == 2
    assert len(set(seen_temps)) == 2
    assert all(name.startswith("2026-08-09.md.") and name.endswith(".tmp") for name in seen_temps)
