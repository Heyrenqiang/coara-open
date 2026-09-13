"""recent_files 最近文件索引回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.records.recent_files import list_recent_files, record_recent_file


@pytest.fixture()
def coara_home(tmp_path: Path) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    return home


def _mk_file(tmp_path: Path, name: str) -> Path:
    f = tmp_path / name
    f.write_bytes(b"x")
    return f


def test_record_and_list_roundtrip(coara_home: Path, tmp_path: Path) -> None:
    f1 = _mk_file(tmp_path, "a.png")
    f2 = _mk_file(tmp_path, "b.pdf")
    record_recent_file(coara_home, origin="inbound", end="web", name="a.png",
                       path=str(f1), mime="image/png", size=1, workspace_id="ws1")
    record_recent_file(coara_home, origin="outbound", end="web", name="b.pdf",
                       path=str(f2), mime="application/pdf", size=1, workspace_id="ws1")

    entries, has_more = list_recent_files(coara_home, workspace_id="ws1")
    assert not has_more
    # 倒序：最新在前
    assert [e["name"] for e in entries] == ["b.pdf", "a.png"]
    assert entries[0]["kind"] == "doc"
    assert entries[1]["kind"] == "image"
    assert entries[1]["origin"] == "inbound"


def test_workspace_filter(coara_home: Path, tmp_path: Path) -> None:
    f1 = _mk_file(tmp_path, "a.png")
    record_recent_file(coara_home, origin="inbound", end="web", name="a.png",
                       path=str(f1), workspace_id="ws1")
    record_recent_file(coara_home, origin="inbound", end="matrix", name="a2.png",
                       path=str(f1), workspace_id="ws2")

    ws1, _ = list_recent_files(coara_home, workspace_id="ws1")
    assert [e["name"] for e in ws1] == ["a.png"]
    allf, _ = list_recent_files(coara_home, workspace_id=None)
    assert len(allf) == 2


def test_dead_link_filtered(coara_home: Path, tmp_path: Path) -> None:
    live = _mk_file(tmp_path, "live.png")
    dead = tmp_path / "dead.png"
    record_recent_file(coara_home, origin="inbound", end="web", name="live.png", path=str(live))
    record_recent_file(coara_home, origin="inbound", end="web", name="dead.png", path=str(dead))

    entries, _ = list_recent_files(coara_home)
    assert [e["name"] for e in entries] == ["live.png"]


def test_pagination_before_ts(coara_home: Path, tmp_path: Path) -> None:
    f = _mk_file(tmp_path, "x.png")
    for i in range(5):
        record_recent_file(coara_home, origin="inbound", end="web", name=f"x{i}.png", path=str(f))
    page1, has_more = list_recent_files(coara_home, limit=2)
    assert len(page1) == 2 and has_more
    cursor = page1[-1]["ts"]
    page2, _ = list_recent_files(coara_home, before_ts=cursor, limit=10)
    # 不重复、不回绕
    names1 = {e["name"] for e in page1}
    assert all(e["name"] not in names1 for e in page2)
