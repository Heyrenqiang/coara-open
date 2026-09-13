"""P0-19: view_seq allocation must not collide across processes."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from src.ui.web_views import WebViewStore, resolve_web_view_path


@pytest.fixture()
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    return home


def _worker_append(coara_home: str, ws: str, sess: str, count: int, out_path: str) -> None:
    store = WebViewStore()
    path = resolve_web_view_path(Path(ws), coara_home=Path(coara_home), subject="root", session_id=sess)
    seqs: list[int] = []
    for i in range(count):
        seq = store.append_event(
            path,
            kind="chunk",
            turn_id=f"t-{i}",
            source="web",
            subject="root",
            session_id=sess,
            payload={"text": f"p-{mp.current_process().pid}-{i}"},
        )
        seqs.append(seq)
    store.flush(timeout=5.0)
    store.close()
    Path(out_path).write_text(",".join(str(s) for s in seqs), encoding="utf-8")


@pytest.mark.timeout(30)
def test_view_seq_unique_across_two_processes(coara_home: Path, tmp_path: Path) -> None:
    """交棒窗口模拟：两个 WebViewStore 进程交错写同一线，序号不得重复。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-xproc"
    out_a = tmp_path / "a.txt"
    out_b = tmp_path / "b.txt"
    n = 25
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(
        target=_worker_append,
        args=(str(coara_home), str(ws), sess, n, str(out_a)),
    )
    p2 = ctx.Process(
        target=_worker_append,
        args=(str(coara_home), str(ws), sess, n, str(out_b)),
    )
    p1.start()
    p2.start()
    p1.join(timeout=20)
    p2.join(timeout=20)
    assert p1.exitcode == 0 and p2.exitcode == 0
    seqs_a = [int(x) for x in out_a.read_text(encoding="utf-8").split(",") if x]
    seqs_b = [int(x) for x in out_b.read_text(encoding="utf-8").split(",") if x]
    assert len(seqs_a) == n and len(seqs_b) == n
    combined = seqs_a + seqs_b
    assert len(combined) == len(set(combined)), f"duplicate view_seq: {sorted(combined)}"
    assert max(combined) == 2 * n
