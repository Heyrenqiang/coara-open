"""daily inflight 标记的超时清除：进程死于运行中不再永久漏派发"""

from __future__ import annotations

import random
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.records import daily_curator as dc

TZ = ZoneInfo("Asia/Shanghai")
# 窗口已过（10:00），只要没有 inflight/失败退避就应到期可跑。
LATE = datetime(2026, 8, 9, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _reset_flight():
    dc.reset_curator_flight_for_tests()
    yield
    dc.reset_curator_flight_for_tests()


def test_mark_curator_inflight_writes_timestamp(tmp_path: Path) -> None:
    dc.mark_curator_inflight(tmp_path, date(2026, 8, 9))
    state = dc.load_curator_state(tmp_path)
    assert state["inflight_date"] == "2026-08-09"
    assert abs(float(state["inflight_since"]) - time.time()) < 5


def test_fresh_inflight_blocks_dispatch(tmp_path: Path) -> None:
    dc.mark_curator_inflight(tmp_path, date(2026, 8, 9))
    assert dc.curator_run_due(agent_dir=tmp_path, now=LATE, rng=random.Random(0)) is False


def test_stale_inflight_cleared_and_run_redispatched(tmp_path: Path) -> None:
    dc.save_curator_state(
        tmp_path,
        {
            "inflight_date": "2026-08-09",
            "inflight_since": time.time() - (dc._INFLIGHT_STALE_SECONDS + 10),
        },
    )
    assert dc.curator_run_due(agent_dir=tmp_path, now=LATE, rng=random.Random(0)) is True
    state = dc.load_curator_state(tmp_path)
    assert "inflight_date" not in state
    assert "inflight_since" not in state


def test_legacy_inflight_without_timestamp_treated_as_dead(tmp_path: Path) -> None:
    """旧版本写入的 inflight 无时间戳：视为死亡，允许重派"""
    dc.save_curator_state(tmp_path, {"inflight_date": "2026-08-09"})
    assert dc.curator_run_due(agent_dir=tmp_path, now=LATE, rng=random.Random(0)) is True


def test_clear_stale_inflight_keeps_fresh_marker(tmp_path: Path) -> None:
    dc.mark_curator_inflight(tmp_path, date(2026, 8, 9))
    assert dc.clear_stale_inflight(tmp_path) is False
    state = dc.load_curator_state(tmp_path)
    assert state["inflight_date"] == "2026-08-09"
