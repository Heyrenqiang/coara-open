"""cron 事件源与消息保质期的回归测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.event_sources.sources.cron import CronSource
from src.event_sources.types import EventSourceDefinition, EventSourceKind


def _defn(**kwargs) -> EventSourceDefinition:
    base = {"id": "cron-test", "kind": "cron", "workspace": "shop", "cron": "* * * * *"}
    base.update(kwargs)
    return EventSourceDefinition.model_validate(base)


def test_cron_kind_accepted_and_requires_expression() -> None:
    defn = _defn()
    assert defn.kind == EventSourceKind.CRON
    with pytest.raises(ValueError, match="cron"):
        _defn(cron="  ")


def test_cron_source_rejects_invalid_expression() -> None:
    with pytest.raises(ValueError):
        CronSource(_defn(cron="not a cron"), emit=_noop_emit)


async def _noop_emit(event) -> None:
    return None


@pytest.mark.asyncio
async def test_cron_source_fires_event_with_minute_dedupe_key() -> None:
    """把下一次触发压到 0.05s 后（猴子补丁 compute_next_cron），验证事件形状。"""
    import src.event_sources.sources.cron as cron_mod

    fired: list = []

    async def _emit(event) -> None:
        fired.append(event)

    real_compute = cron_mod.compute_next_cron
    cron_mod.compute_next_cron = lambda cron, tz, after=None: datetime.now(tz) + timedelta(seconds=0.05)
    try:
        source = CronSource(_defn(), emit=_emit)
        source.start()
        await asyncio.sleep(0.3)
        await source.stop()
    finally:
        cron_mod.compute_next_cron = real_compute

    assert fired, "cron source should have fired"
    event = fired[0]
    assert event.source_id == "cron-test"
    assert event.workspace == "shop"
    assert event.event_type == "cron.tick"
    assert event.payload["cron"] == "* * * * *"
    assert "fired_at" in event.payload
    # 去重键按分钟：reload 双开窗口内重复触发会被吸收
    expected_minute = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M")
    assert event.dedupe_key.startswith("cron:cron-test:")
    assert len(event.dedupe_key.rsplit(":", 1)[1]) == len(expected_minute)


@pytest.mark.asyncio
async def test_cron_source_stop_is_clean() -> None:
    source = CronSource(_defn(), emit=_noop_emit)
    source.start()
    await source.stop()
    await source.stop()  # 幂等
