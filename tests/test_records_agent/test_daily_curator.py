"""Tests for daily memory curator dispatch / state / delivery."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.records import daily_curator as dc
from src.records.agent_store import MemoryStore

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _reset_flight():
    dc.reset_curator_flight_for_tests()
    yield
    dc.reset_curator_flight_for_tests()


@pytest.fixture(autouse=True)
def _fixed_tz(monkeypatch):
    monkeypatch.setattr(dc, "curator_timezone", lambda: TZ)


def test_run_due_after_slot_arrives(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    # First tick on a fresh install: a random slot is picked and persisted —
    # but only for TOMORROW. Day one never curates (nothing to curate yet).
    early = datetime(2026, 8, 5, 5, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=early, rng=random.Random(0)) is False
    slot = dc._parse_dt(dc.load_curator_state(mem)["next_run_at"], tz=TZ)
    assert slot is not None
    assert slot.date() == date(2026, 8, 6)
    assert datetime(2026, 8, 6, 6, 0, tzinfo=TZ) <= slot <= datetime(2026, 8, 6, 9, 0, tzinfo=TZ)
    # Before the slot: not due; at the slot: due.
    assert dc.curator_run_due(agent_dir=mem, now=slot - timedelta(minutes=1)) is False
    assert dc.curator_run_due(agent_dir=mem, now=slot) is True


def test_run_due_fresh_install_skips_day_one(tmp_path):
    """Fresh install started after the morning window: no immediate catch-up run.

    A brand-new user has nothing to curate on day one; the slot is scheduled
    for tomorrow instead of collapsing to "now" (which used to burn tokens on
    an empty digest right after setup).
    """
    mem = tmp_path / "memory"
    mem.mkdir()
    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is False
    slot = dc._parse_dt(dc.load_curator_state(mem)["next_run_at"], tz=TZ)
    assert slot is not None
    assert slot.date() == date(2026, 8, 6)
    assert datetime(2026, 8, 6, 6, 0, tzinfo=TZ) <= slot <= datetime(2026, 8, 6, 9, 0, tzinfo=TZ)


def test_run_due_catchup_after_window_for_established_user(tmp_path):
    """Established user (state exists) with a stale slot: missed window collapses
    to "now" so the next tick runs ASAP."""
    mem = tmp_path / "memory"
    mem.mkdir()
    yesterday = datetime(2026, 8, 4, 7, 0, tzinfo=TZ)
    dc.save_curator_state(mem, {"next_run_at": yesterday.isoformat()})
    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is True
    # Missed window: slot collapses to "now" so the next tick runs ASAP.
    slot = dc._parse_dt(dc.load_curator_state(mem)["next_run_at"], tz=TZ)
    assert slot == late


def test_run_not_due_when_today_digest_exists(tmp_path):
    mem = tmp_path / "memory"
    store = MemoryStore(mem)
    store.write_digest("2026-08-05", "# ok\n")
    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is False
    # Next slot rolled into tomorrow's window.
    slot = dc._parse_dt(dc.load_curator_state(mem)["next_run_at"], tz=TZ)
    assert slot is not None
    assert slot.date() == date(2026, 8, 6)
    assert datetime(2026, 8, 6, 6, 0, tzinfo=TZ) <= slot <= datetime(2026, 8, 6, 9, 0, tzinfo=TZ)


def test_promote_inflight_when_digest_ready(tmp_path):
    mem = tmp_path / "memory"
    store = MemoryStore(mem)
    day = date(2026, 8, 5)
    range_end = datetime(2026, 8, 5, 7, 23, tzinfo=TZ)
    dc.mark_curator_inflight(mem, day, range_end=range_end)
    store.write_digest(day.isoformat(), "## 要点\n\n做了概况维护\n")
    assert dc.promote_inflight_if_digest_ready(mem) is True
    state = dc.load_curator_state(mem)
    assert state.get("pending_digest") == "2026-08-05"
    # Coverage advances to the run's upper bound.
    assert state.get("last_run_at") == range_end.isoformat()
    assert "inflight_date" not in state
    assert "inflight_range_end" not in state


@pytest.mark.asyncio
async def test_deliver_pending_digest_immediately(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    store = MemoryStore(mem)
    day = date(2026, 8, 5)
    store.write_digest(day.isoformat(), "要点 A\n\n启发一句。")
    dc.mark_pending_digest(mem, day)

    root = SimpleNamespace(
        records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None),
        matrix_notify=None,
    )
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)

    # 整理完即刻投递：不等待任何时间窗口。
    assert await dc.maybe_deliver_pending_digest(root) is True
    # 只显示不进上下文：root 上没有 foreground_coara 也必须能正常投递；且幂等
    assert await dc.maybe_deliver_pending_digest(root) is False
    state = dc.load_curator_state(mem)
    assert state.get("delivered_digest") == "2026-08-05"
    assert "pending_digest" not in state
    assert "delivery_at" not in state


def test_compute_next_run_at_in_morning_window():
    now = datetime(2026, 8, 5, 0, 10, tzinfo=TZ)
    rng = random.Random(42)
    slots = [dc.compute_next_run_at(now=now, tz=TZ, rng=rng) for _ in range(20)]
    for slot in slots:
        assert slot.tzinfo is not None
        assert slot.date() == date(2026, 8, 5)
        assert datetime(2026, 8, 5, 6, 0, tzinfo=TZ) <= slot <= datetime(2026, 8, 5, 9, 0, tzinfo=TZ)


def test_compute_next_run_at_catchup_after_window():
    now = datetime(2026, 8, 5, 11, 30, tzinfo=TZ)
    slot = dc.compute_next_run_at(now=now, tz=TZ, rng=random.Random(0))
    assert slot == now


def test_compute_next_run_at_tomorrow_window():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    slot = dc.compute_next_run_at(now=now, tz=TZ, rng=random.Random(0), day=date(2026, 8, 6))
    assert datetime(2026, 8, 6, 6, 0, tzinfo=TZ) <= slot <= datetime(2026, 8, 6, 9, 0, tzinfo=TZ)


def test_coverage_start_legacy_migration(tmp_path):
    """老 state 只有 last_run_date：覆盖截止点按旧语义回退到次日零点。"""
    state = {"last_run_date": "2026-08-04"}
    current = datetime(2026, 8, 5, 7, 30, tzinfo=TZ)
    start = dc._coverage_start(state, current=current, zone=TZ)
    assert start == datetime(2026, 8, 5, 0, 0, tzinfo=TZ)


def test_coverage_start_fresh_install(tmp_path):
    current = datetime(2026, 8, 5, 7, 30, tzinfo=TZ)
    start = dc._coverage_start({}, current=current, zone=TZ)
    assert start == datetime(2026, 8, 5, 0, 0, tzinfo=TZ)


def test_curator_prompt_carries_sampled_reference_phrases():
    prompt = dc.build_curator_prompt(
        for_date=date(2026, 8, 5),
        range_start=datetime(2026, 8, 4, 7, 0, tzinfo=TZ),
        range_end=datetime(2026, 8, 5, 7, 0, tzinfo=TZ),
        agent_dir=Path("/tmp/agent"),
        digest_file=Path("/tmp/agent/digests/2026-08-05.md"),
        rng=random.Random(7),
    )
    assert "轮播语参考词" in prompt
    ref_lines = [ln for ln in prompt.splitlines() if ln.startswith("- ")]
    assert len(ref_lines) == 6


class _FakeSessionCoara:
    """daily 空间会话主体的测试替身：吃 process_message 流并留痕。

    记录放在生成器体内（而非调用处）：dispatch 把回合丢进 asyncio.Task，
    断言执行时 process_message 可能已被调用但流尚未开始消费——真正的
    语义发生点是消费开始。
    """

    def __init__(self, captured: dict):
        self.captured = captured
        self.task: asyncio.Task | None = None

    def process_message(self, content, **kwargs):
        async def _gen():
            self.captured["content"] = content
            self.captured["kwargs"] = kwargs
            yield "chunk"

        return _gen()

    def spawn(self, coro, **kwargs):
        self.task = asyncio.get_running_loop().create_task(coro, **kwargs)
        return self.task


@pytest.mark.asyncio
async def test_dispatch_single_flight(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory")
    root = SimpleNamespace(records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None))

    captured: dict = {}
    coara = _FakeSessionCoara(captured)
    session = SimpleNamespace(coara=coara)

    async def _ensure(r):
        assert r is root
        return session

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: ("deepseek", "flash"))
    monkeypatch.setattr(dc.asyncio, "create_task", coara.spawn)

    now = datetime(2026, 8, 5, 7, 23, tzinfo=TZ)
    tid1 = await dc.dispatch_daily(root, now=now)
    assert tid1 and tid1.startswith("daily-2026-08-05-")
    tid2 = await dc.dispatch_daily(root, now=now)
    assert tid2 == tid1  # coalesce
    # 让回合开始消费（process_message 的语义发生点在流消费开始）
    await asyncio.sleep(0)

    # 空间内回合：process_message 吃 curator prompt，trust/显示/来源语义固定。
    assert "2026-08-05 00:00 至 2026-08-05 07:23" in captured["content"]
    assert captured["kwargs"]["trust_level"] == "owner"
    assert captured["kwargs"]["show_tool_summary"] is False
    assert captured["kwargs"]["source"] == "background"

    # 连续区间：digest 键为派发日，inflight 记录区间端点。
    state = dc.load_curator_state(store.root)
    assert state["inflight_date"] == "2026-08-05"
    assert state["inflight_range_end"] == now.isoformat()

    # 回合 task 自然完成 → done callback 清 flight 并结算（无 digest = failed）。
    await coara.task
    await asyncio.sleep(0)
    assert dc._curator_flight["task_id"] is None
    state = dc.load_curator_state(store.root)
    assert "inflight_date" not in state
    assert state["last_failure"]["date"] == "2026-08-05"


@pytest.mark.asyncio
async def test_dispatch_completion_promotes_digest(tmp_path, monkeypatch):
    """回合把 digest 写出来后结束：done callback 促转 pending（产出物链路不变）。"""
    store = MemoryStore(tmp_path / "memory")
    root = SimpleNamespace(records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None))

    class _DigestCoara(_FakeSessionCoara):
        def process_message(self, content, **kwargs):
            async def _gen():
                store.write_digest("2026-08-05", "## 要点\n\n整理完成。\n")
                yield "chunk"

            return _gen()

    captured: dict = {}
    coara = _DigestCoara(captured)
    session = SimpleNamespace(coara=coara)

    async def _ensure(r):
        return session

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: ("deepseek", "flash"))
    monkeypatch.setattr(dc.asyncio, "create_task", coara.spawn)

    now = datetime(2026, 8, 5, 7, 23, tzinfo=TZ)
    tid = await dc.dispatch_daily(root, now=now)
    assert tid
    await coara.task
    await asyncio.sleep(0)
    state = dc.load_curator_state(store.root)
    assert state.get("pending_digest") == "2026-08-05"
    assert dc._curator_flight["task_id"] is None


def test_daily_llm_params_prefers_records_config(monkeypatch):
    """records.daily_provider/model 显式配置时优先，缺 model 取 provider 默认。"""
    from src.llm.registry import provider_registry

    mem = SimpleNamespace(daily_provider="kimi", daily_model=None)
    monkeypatch.setattr(dc, "_records_cfg", lambda: mem)
    monkeypatch.setattr(provider_registry, "has", lambda name: name == "kimi")
    monkeypatch.setattr(provider_registry, "get", lambda name: SimpleNamespace(default_model="k3"))

    assert dc._daily_llm_params() == ("kimi", "k3")


def test_daily_llm_params_falls_back_to_global_default(monkeypatch):
    """未配置时跟随全局默认（llm_preferences），不跟前台工作空间绑定。"""
    from src.core.config import config_manager
    from src.llm.registry import provider_registry

    monkeypatch.setattr(dc, "_records_cfg", lambda: SimpleNamespace(daily_provider=None, daily_model=None))
    monkeypatch.setattr(
        config_manager,
        "_config",
        SimpleNamespace(default_provider="deepseek", default_model="flash"),
    )
    monkeypatch.setattr(provider_registry, "has", lambda name: True)

    assert dc._daily_llm_params() == ("deepseek", "flash")


def test_daily_llm_params_invalid_provider_falls_back(monkeypatch):
    """配置的 provider 未注册时告警并回退全局默认。"""
    from src.core.config import config_manager
    from src.llm.registry import provider_registry

    monkeypatch.setattr(dc, "_records_cfg", lambda: SimpleNamespace(daily_provider="nope", daily_model="x"))
    monkeypatch.setattr(
        config_manager,
        "_config",
        SimpleNamespace(default_provider="deepseek", default_model="flash"),
    )
    monkeypatch.setattr(provider_registry, "has", lambda name: name == "deepseek")

    assert dc._daily_llm_params() == ("deepseek", "flash")


@pytest.mark.asyncio
async def test_memory_digest_write_action(tmp_path):
    from src.records.store import RecordsStore
    from src.tools.builtin.records.record import RecordTool

    rs = RecordsStore(tmp_path / "records", agent_enabled=True)
    tool = RecordTool(store=rs, parent_coara=SimpleNamespace(session_id="s"))  # type: ignore[arg-type]
    result = await tool.create_invocation(
        {
            "action": "digest_write",
            "date": "2026-08-04",
            "content": "# 日报\n\n完成了双任务接线。",
        }
    ).execute()
    assert not result.is_error
    assert rs.agent is not None
    path = rs.agent.digest_path("2026-08-04")
    assert path.is_file()
    assert "双任务" in path.read_text(encoding="utf-8")


def test_system_only_includes_daily():
    from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES, get_enabled_subagents, get_subagent

    assert "daily" in SYSTEM_ONLY_SUBAGENT_TYPES
    assert "janitor" in SYSTEM_ONLY_SUBAGENT_TYPES
    names = {sa.name for sa in get_enabled_subagents()}
    assert "daily" not in names
    assert "janitor" not in names
    assert "truth" not in names
    assert get_subagent("daily") is not None
    assert get_subagent("truth") is None
    assert get_subagent("memory_curator") is None


def test_janitor_prompt_carries_only_dynamic_params(tmp_path):
    """信封一条 <系统提醒>：动态参数 + janitor.md；不复述旧版任务类型/触发字段。"""
    from src.coara.workspace_protocol import build_janitor_prompt

    text = build_janitor_prompt(workspace_dir=str(tmp_path))
    assert "概况文件" in text
    assert "ws.md" in text
    assert text.count("<系统提醒>") == 1
    assert "任务类型" not in text
    assert "触发" not in text
    assert "主会话快照" not in text
    assert "双任务" not in text
    assert "必读" not in text
    # 职责正文并进信封，不再另注
    assert "维护当前工作空间概况" in text or "工作空间概况" in text


def test_memory_config_curator_defaults():
    from src.core.types import RecordsConfig

    cfg = RecordsConfig()
    assert cfg.daily_curator_enabled is True
    assert cfg.curator_timezone == "Asia/Shanghai"


def test_stale_flight_self_heals(tmp_path):
    """A flight whose completion event was lost must not block future runs."""
    mem = tmp_path / "memory"
    mem.mkdir()
    # 磁盘 inflight 标记也已超过死亡 TTL（进程中断遗留），配合内存 flight 卡死。
    day = date(2026, 8, 5)
    dc.mark_curator_inflight(mem, day, range_end=datetime(2026, 8, 5, 7, 23, tzinfo=TZ))
    state = dc.load_curator_state(mem)
    state["inflight_since"] = time.time() - (dc._INFLIGHT_STALE_SECONDS + 60)
    dc.save_curator_state(mem, state)
    dc._curator_flight["task_id"] = "sa-curator-stuck"
    dc._curator_flight["started_at"] = time.monotonic() - (dc._FLIGHT_STALE_SECONDS + 60)

    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is True
    assert dc._curator_flight["task_id"] is None


def test_active_flight_blocks_dispatch(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    dc._curator_flight["task_id"] = "sa-curator-live"
    dc._curator_flight["started_at"] = time.monotonic()

    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is False


def test_failure_clears_inflight_and_backs_off(tmp_path):
    """Failed run (no digest): inflight cleared, retry throttled by backoff."""
    mem = tmp_path / "memory"
    MemoryStore(mem)
    day = date(2026, 8, 5)
    range_end = datetime(2026, 8, 5, 7, 23, tzinfo=TZ)
    dc.mark_curator_inflight(mem, day, range_end=range_end)

    assert dc.handle_daily_completion(mem, day) == "failed"
    state = dc.load_curator_state(mem)
    assert "inflight_date" not in state
    assert "inflight_range_end" not in state
    assert state["last_failure"]["date"] == "2026-08-05"
    # 失败不推进覆盖截止点：区间内容不丢。
    assert "last_run_at" not in state

    late = datetime(2026, 8, 5, 10, 0, tzinfo=TZ)
    # Within backoff: no immediate re-dispatch loop.
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is False

    # After backoff: retry allowed.
    state["last_failure"]["ts"] = time.time() - (dc._FAILURE_BACKOFF_SECONDS + 60)
    dc.save_curator_state(mem, state)
    assert dc.curator_run_due(agent_dir=mem, now=late, rng=random.Random(0)) is True


def test_completion_promotes_when_digest_ready(tmp_path):
    mem = tmp_path / "memory"
    store = MemoryStore(mem)
    day = date(2026, 8, 5)
    dc.mark_curator_inflight(mem, day, range_end=datetime(2026, 8, 5, 7, 23, tzinfo=TZ))
    store.write_digest(day.isoformat(), "## 要点\n\n写完概况。\n")

    assert dc.handle_daily_completion(mem, day) == "promoted"
    state = dc.load_curator_state(mem)
    assert state.get("pending_digest") == "2026-08-05"
    assert "last_failure" not in state
