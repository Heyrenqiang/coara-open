"""分段轮转：阈值触发、归档+active 按序读取、失败降级、seq 跨段连续。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.session_log.store as store_mod
from src.session_log.store import (
    append_events,
    append_with_seq,
    iter_events,
    last_seq,
    read_events,
)
from src.session_log.types import EVENT_USER_MESSAGE, build_event

_ARCHIVE_NAME_RE = re.compile(r"^session_events\.20\d{6}-\d{6}\.jsonl$")


@pytest.fixture(autouse=True)
def _no_cold_compress(monkeypatch: pytest.MonkeyPatch):
    """轮转触发的后台冷压缩在本文件一律改为同步空操作（另由 test_log_cold_compress 专测）。"""
    monkeypatch.setattr(store_mod, "_compress_archives_async", lambda path: None)


def _event(seq: int, content: str = "x"):
    return build_event(kind=EVENT_USER_MESSAGE, seq=seq, session_id="s1", payload={"content": content})


def _archives(tmp_path: Path) -> list[Path]:
    return sorted(p for p in tmp_path.iterdir() if _ARCHIVE_NAME_RE.match(p.name))


def test_rotate_triggered_by_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """active 写前达到阈值：先轮转为时间戳归档段（保留），再写新 active。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    path = tmp_path / "session_events.jsonl"
    append_events(path, [_event(1, "a")])
    append_events(path, [_event(2, "b")])  # 写前 size >= 阈值 → 先轮转

    archives = _archives(tmp_path)
    assert len(archives) == 1
    # 归档段保留旧事件，active 只有新事件；对外读取顺序不变
    assert [e["seq"] for e in store_mod._iter_segment_events(archives[0])] == [1]
    assert [e["seq"] for e in store_mod._iter_segment_events(path)] == [2]
    assert [e["seq"] for e in read_events(path)] == [1, 2]


def test_rotate_via_append_with_seq_keeps_seq_continuous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """append_with_seq 同样轮转；轮转不清 seq 分配点，seq 跨段连续。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    path = tmp_path / "session_events.jsonl"
    append_with_seq(path, lambda s: [_event(s, "a")])
    append_with_seq(path, lambda s: [_event(s, "b")])
    append_with_seq(path, lambda s: [_event(s, "c")])

    assert _archives(tmp_path)
    assert [e["seq"] for e in read_events(path)] == [1, 2, 3]
    assert store_mod._seq_cache[path] == 3


def test_iter_events_reads_archives_in_order_then_active(tmp_path: Path) -> None:
    """归档段按文件名（时间序）先读，active 最后；非时间戳文件不混入。"""
    path = tmp_path / "session_events.jsonl"
    # 乱序创建两段归档，读取必须按名排序
    append_events(tmp_path / "session_events.20260101-000002.jsonl", [_event(3), _event(4)])
    append_events(tmp_path / "session_events.20260101-000001.jsonl", [_event(1), _event(2)])
    append_events(path, [_event(5)])
    (tmp_path / "session_events.jsonl.bak").write_text('{"seq": 99}\n', encoding="utf-8")
    (tmp_path / "session_events.old.jsonl").write_text('{"seq": 98}\n', encoding="utf-8")

    assert [e["seq"] for e in iter_events(path)] == [1, 2, 3, 4, 5]


def test_rotate_failure_degrades_without_losing_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """轮转失败（如 Windows 读方句柄未关）：跳过本次照常追加，恢复后下次写再试。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    path = tmp_path / "session_events.jsonl"
    append_events(path, [_event(1, "a")])

    original_rename = Path.rename

    def _deny(self: Path, target: Path) -> None:
        if self == path:
            raise OSError("access denied")
        original_rename(self, target)

    monkeypatch.setattr(Path, "rename", _deny)
    append_events(path, [_event(2, "b")])  # 轮转失败：不丢事件、不抛给调用方
    assert not _archives(tmp_path)
    assert [e["seq"] for e in read_events(path)] == [1, 2]
    assert path in store_mod._rotate_failure_logged

    monkeypatch.setattr(Path, "rename", original_rename)
    append_events(path, [_event(3, "c")])  # 恢复后下次写重试成功
    assert len(_archives(tmp_path)) == 1
    assert [e["seq"] for e in read_events(path)] == [1, 2, 3]
    assert path not in store_mod._rotate_failure_logged


def test_last_seq_tail_fast_path_across_segments(tmp_path: Path) -> None:
    """last_seq 只读尾部：active 缺失/清空时回退归档末段，不回退重发 seq。"""
    path = tmp_path / "session_events.jsonl"
    append_events(tmp_path / "session_events.20260101-000001.jsonl", [_event(1), _event(5)])
    assert last_seq(path) == 5  # active 缺失：回退归档末段
    append_events(path, [_event(6)])
    assert last_seq(path) == 6
    path.write_text("", encoding="utf-8")  # active 被外部清空：仍取归档末段
    assert last_seq(path) == 5


def test_seq_allocation_continues_across_rotation_cold_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程重启（分配缓存空）后首次写：经尾部快路径校准，seq 跨段连续不重发。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    path = tmp_path / "session_events.jsonl"
    append_with_seq(path, lambda s: [_event(s, "a")])
    append_with_seq(path, lambda s: [_event(s, "b")])  # 触发轮转
    store_mod._seq_cache.clear()  # 模拟进程重启
    append_with_seq(path, lambda s: [_event(s, "c")])
    assert [e["seq"] for e in read_events(path)] == [1, 2, 3]


def test_engine_tape_rotates_with_own_stem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """engine 系统带走同一 store：归档段沿用自身 stem，互不串段。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    path = tmp_path / "engine_session_events.jsonl"
    append_events(path, [_event(1)])
    append_events(path, [_event(2)])
    names = [p.name for p in tmp_path.iterdir()]
    assert any(re.match(r"^engine_session_events\.20\d{6}-\d{6}\.jsonl$", n) for n in names)
    assert [e["seq"] for e in read_events(path)] == [1, 2]
