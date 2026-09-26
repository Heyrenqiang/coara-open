"""归档冷压缩：轮转后归档段 gzip，读取透明解压，顺序与 seq 契约不变。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.session_log.store as store_mod
from src.session_log.store import append_with_seq, iter_events, last_seq, read_events
from src.session_log.types import EVENT_USER_MESSAGE, build_event

_ARCHIVE_GZ_RE = re.compile(r"^session_events\.20\d{6}-\d{6}\.jsonl\.gz$")


def _event(seq: int, content: str = "x"):
    return build_event(kind=EVENT_USER_MESSAGE, seq=seq, session_id="s1", payload={"content": content})


def test_rotation_compresses_archives_and_reads_transparently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """轮转产生归档 → 同步冷压缩成 .gz → iter/read/last_seq 与未压缩完全一致。"""
    monkeypatch.setattr(store_mod, "_ROTATE_THRESHOLD_BYTES", 1)
    monkeypatch.setattr(store_mod, "_compress_archives_async", store_mod._compress_archives)
    path = tmp_path / "session_events.jsonl"
    for i in range(1, 4):
        append_with_seq(path, lambda s, i=i: [_event(s, f"c{i}")])

    gz = [p for p in tmp_path.iterdir() if _ARCHIVE_GZ_RE.match(p.name)]
    assert gz, "归档段应被压缩为 .jsonl.gz"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".jsonl") and p.name != path.name]

    assert [e["seq"] for e in read_events(path)] == [1, 2, 3]
    assert [e["seq"] for e in iter_events(path)] == [1, 2, 3]
    assert last_seq(path) == 3


def test_last_seq_falls_back_to_gz_archive_when_active_empty(tmp_path: Path) -> None:
    """active 为空时 last_seq 回退到最新 .gz 归档（整段扫路径）。"""
    archive = tmp_path / "session_events.20260822-010000.jsonl"
    archive.write_text(
        "\n".join(__import__("json").dumps(_event(i, f"c{i}"), ensure_ascii=False) for i in (1, 2)) + "\n",
        encoding="utf-8",
    )
    store_mod._compress_archives(tmp_path / "session_events.jsonl")
    assert not archive.exists()
    gz = tmp_path / "session_events.20260822-010000.jsonl.gz"
    assert gz.is_file()
    (tmp_path / "session_events.jsonl").touch()

    assert last_seq(tmp_path / "session_events.jsonl") == 2
    # 冷启动分配 seq 从 .gz 末序继续
    seen = []

    def factory(start: int):
        seen.append(start)
        return [_event(start, "new")]

    append_with_seq(tmp_path / "session_events.jsonl", factory)
    assert seen == [3]


def test_mixed_plain_and_gz_archives_read_in_time_order(tmp_path: Path) -> None:
    """同一带混有未压缩与 .gz 归档时按时间戳序读取（与压缩状态无关）。"""
    import json

    older = tmp_path / "session_events.20260801-000000.jsonl.gz"
    with __import__("gzip").open(older, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_event(1, "old"), ensure_ascii=False) + "\n")
    newer = tmp_path / "session_events.20260802-000000.jsonl"
    newer.write_text(json.dumps(_event(2, "new"), ensure_ascii=False) + "\n", encoding="utf-8")
    active = tmp_path / "session_events.jsonl"
    active.write_text(json.dumps(_event(3, "active"), ensure_ascii=False) + "\n", encoding="utf-8")

    assert [e["seq"] for e in iter_events(active)] == [1, 2, 3]


def test_compress_failure_keeps_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """压缩中途失败：原段保留不丢，临时文件清理。"""
    archive = tmp_path / "session_events.20260801-000000.jsonl"
    archive.write_text("{}\n", encoding="utf-8")

    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("disk full")

    monkeypatch.setattr(store_mod.gzip, "open", _Boom)
    store_mod._compress_archives(tmp_path / "session_events.jsonl")

    assert archive.is_file()
    assert not list(tmp_path.glob("*.tmp"))
