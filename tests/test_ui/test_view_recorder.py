"""录像带录制器：落带是内核职责，与端无关。

契约（2026-09-13 定）：任何端（web / 手机 Matrix / CLI attach）产生的对话一律进
录像带，落在「帧自身 workspace + session」那条线上；端不再注入落带回调，也不再
决定落不落。本模块钉死三条：

1. 归属缺失宁可不落，不猜；
2. TurnStream 缺省走内核录制器，帧照样带 view_seq；
3. 手机端帧落在自己的空间线上，source 记真实端。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ui import view_recorder
from src.ui.turn_stream import TurnStream


class _FakeWriter:
    def __init__(self) -> None:
        self.rows: list[tuple[Path, dict]] = []

    def append_jsonl(self, path: Path, frame: dict) -> None:
        self.rows.append((path, frame))


@pytest.fixture()
def writer(monkeypatch: pytest.MonkeyPatch) -> _FakeWriter:
    store = view_recorder.shared_view_store()
    fake = _FakeWriter()
    monkeypatch.setattr(store, "_writer", fake, raising=False)
    monkeypatch.setattr(store, "_seq_cache", {}, raising=False)
    monkeypatch.setattr(store, "_line_meta", {}, raising=False)
    return fake


def test_record_frame_without_workspace_is_skipped() -> None:
    """归属缺失宁可不落：猜归属等于把内容搬到别人家。"""
    assert view_recorder.record_view_frame({"session_id": "s", "kind": "chunk"}) is None


def test_matrix_frame_lands_on_its_own_line(writer: _FakeWriter, tmp_path: Path) -> None:
    """手机端的一帧同样进带，source 记真实端，序号照常分配。"""
    seq = view_recorder.record_view_frame(
        {
            "kind": "user_message",
            "workspace_dir": str(tmp_path / "ws"),
            "session_id": "sess-1",
            "subject": "root",
            "source": "matrix",
            "turn_id": "t1",
            "payload": {"content": "手机端说的一句话"},
        },
        coara_home=tmp_path,
    )
    assert seq and seq > 0
    path, frame = writer.rows[-1]
    assert frame["source"] == "matrix"
    assert frame["kind"] == "user_message"
    assert frame["view_seq"] == seq
    assert frame["payload"]["content"] == "手机端说的一句话"
    assert "web_views" in str(path)


@pytest.mark.asyncio
async def test_turn_stream_lands_without_injected_persist(writer: _FakeWriter, tmp_path: Path) -> None:
    """端不再注入落带回调：TurnStream 缺省走内核录制器，帧照样带 view_seq。"""
    registry = SimpleNamespace(has_active=lambda: False, send_to_active_nowait=lambda frame: None)
    server = SimpleNamespace(registry=registry, coara_home=tmp_path)
    stream = TurnStream(
        "t1",
        "cli-attached",
        "root",
        server,
        session_id="sess-1",
        workspace_dir=str(tmp_path / "ws"),
    )
    stream.emit("chunk", text="正文")
    await asyncio.sleep(0.05)  # 微批窗口
    frame = stream.replay()[-1]
    assert frame["type"] == "chunk"
    assert frame.get("view_seq")
    assert writer.rows and writer.rows[-1][1]["source"] == "cli-attached"
