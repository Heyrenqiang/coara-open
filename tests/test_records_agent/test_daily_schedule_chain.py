"""定时触发链路连通性：watcher tick → curator_tick → 空间会话回合 → 结算 → 投递。

daily 升级为 internal 空间后，调度/覆盖推进/产出物/投递链路一个字不能变。
本文件用替身会话验证整条链端到端仍然连通（不经真实 LLM）。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.records import daily_curator as dc
from src.records.agent_store import MemoryStore

TZ = ZoneInfo("Asia/Shanghai")
# 已过 09:00 窗口：curator_run_due 恒为 True（无 inflight/失败退避/今日 digest 时）。
LATE = datetime(2026, 8, 9, 10, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _reset_flight():
    dc.reset_curator_flight_for_tests()
    yield
    dc.reset_curator_flight_for_tests()


@pytest.fixture(autouse=True)
def _fixed_tz(monkeypatch):
    monkeypatch.setattr(dc, "curator_timezone", lambda: TZ)


class _ChainCoara:
    """daily 空间会话替身：回合内写出 digest 与轮播语（模拟产出物），留痕回合参数。"""

    def __init__(self, store: MemoryStore, captured: dict):
        self._store = store
        self.captured = captured
        self.task: asyncio.Task | None = None

    def process_message(self, content, **kwargs):
        async def _gen():
            self.captured["content"] = content
            self.captured["kwargs"] = kwargs
            # 产出物 1：日报 digest（digest_path(agent_dir, target)）
            self._store.write_digest("2026-08-09", "## 要点\n\n整理完成。\n")
            # 产出物 2：CLI 轮播语（agent_dir/loading_phrases_custom.json）
            (self._store.root / "loading_phrases_custom.json").write_text(
                '{"date": "2026-08-09", "phrases": ["链路连通中"]}',
                encoding="utf-8",
            )
            yield "chunk"

        return _gen()

    def spawn(self, coro, **kwargs):
        self.task = asyncio.get_running_loop().create_task(coro, **kwargs)
        return self.task


@pytest.mark.asyncio
async def test_tick_to_delivery_full_chain(tmp_path, monkeypatch):
    """端到端：curator_tick → maybe_dispatch_curator → dispatch_daily → 空间回合
    → done callback(handle_daily_completion) → 下轮 tick 投递 digest。"""
    store = MemoryStore(tmp_path / "memory")
    root = SimpleNamespace(
        records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None),
        matrix_notify=None,
    )

    captured: dict = {}
    coara = _ChainCoara(store, captured)
    session = SimpleNamespace(coara=coara)

    async def _ensure(r):
        assert r is root
        return session

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: ("deepseek", "flash"))
    monkeypatch.setattr(dc.asyncio, "create_task", coara.spawn)
    # 已用过一天的用户：昨天的 slot 未消费（stale）→ 今天窗口已过则立即补跑。
    # 全新安装（无 state）首日不再触发（见 test_daily_curator.py）。
    dc.save_curator_state(
        store.root,
        {"next_run_at": (LATE - timedelta(days=1)).isoformat()},
    )
    # 调度时机：下一 tick 落在已过窗口的时刻（与 root watcher 的调用形态一致——
    # curator_tick(root) 内部经 local_now 取当前时间，这里钉住它）。
    monkeypatch.setattr(dc, "local_now", lambda *, tz=None: LATE)

    # --- 第 1 轮 tick（root idle watcher 的 daily 钩子调用形态）---
    await dc.curator_tick(root)
    # 调度机制原样：随机槽位持久化 → 到点 → 派发空间回合
    assert coara.task is not None, "curator_tick 未触发 dispatch_daily"
    # 让回合开始消费（process_message 的语义发生点在流消费开始）
    await asyncio.sleep(0)
    assert "整理时段" in captured["content"]
    assert "日报日期：2026-08-09" in captured["content"]
    assert captured["kwargs"]["source"] == "background"
    # inflight 标记与覆盖区间端点照常落盘
    state = dc.load_curator_state(store.root)
    assert state["inflight_date"] == "2026-08-09"
    assert state["inflight_range_end"] == LATE.isoformat()

    # --- 回合完成 → done callback 结算：coverage 推进 + pending ---
    await coara.task
    await asyncio.sleep(0)
    assert dc._curator_flight["task_id"] is None
    state = dc.load_curator_state(store.root)
    assert state.get("pending_digest") == "2026-08-09"
    assert state["last_run_at"] == LATE.isoformat()  # coverage 推进到回合上界
    assert "inflight_date" not in state

    # --- 第 2 轮 tick：投递（CLI/Matrix 展示逻辑不变；替身无 matrix 走 CLI 通道）---
    assert await dc.curator_tick(root) is None  # tick 无返回值，投递是副作用
    state = dc.load_curator_state(store.root)
    assert state.get("delivered_digest") == "2026-08-09"
    assert "pending_digest" not in state

    # --- 调度幂等：今日已有 digest → 不再到期，next_run_at 滚到明天窗口 ---
    assert dc.curator_run_due(agent_dir=store.root, now=LATE, rng=random.Random(0)) is False
    slot = dc._parse_dt(dc.load_curator_state(store.root)["next_run_at"], tz=TZ)
    assert slot is not None and slot.date().isoformat() == "2026-08-10"


@pytest.mark.asyncio
async def test_dispatch_signature_unchanged_and_turn_params(tmp_path, monkeypatch):
    """dispatch_daily 签名（root, *, now, rng）与返回值（task_id | None）不变。"""
    store = MemoryStore(tmp_path / "memory")
    root = SimpleNamespace(records_store=SimpleNamespace(agent=store, agent_root=store.root, user=None))

    captured: dict = {}
    coara = _ChainCoara(store, captured)
    session = SimpleNamespace(coara=coara)

    async def _ensure(r):
        return session

    monkeypatch.setattr(dc, "ensure_daily_workspace_session", _ensure)
    monkeypatch.setattr(dc, "curator_enabled", lambda: True)
    monkeypatch.setattr(dc, "_daily_llm_params", lambda: ("deepseek", "flash"))
    monkeypatch.setattr(dc.asyncio, "create_task", coara.spawn)

    now = datetime(2026, 8, 9, 7, 30, tzinfo=TZ)
    tid = await dc.dispatch_daily(root, now=now, rng=random.Random(0))
    assert isinstance(tid, str) and tid
    await asyncio.sleep(0)
    # 回合仍是 owner 信任 / 无工具摘要 / background 来源（与旧 delegate 派发语义对齐）
    assert captured["kwargs"]["trust_level"] == "owner"
    assert captured["kwargs"]["show_tool_summary"] is False
    assert captured["kwargs"]["source"] == "background"
    # 轮播语产出物照常落盘
    phrases = store.root / "loading_phrases_custom.json"
    await coara.task
    await asyncio.sleep(0)
    assert phrases.is_file()
    assert "链路连通中" in phrases.read_text(encoding="utf-8")


def test_root_watcher_calls_curator_tick():
    """root idle watcher 的 daily 钩子仍指向 curator_tick（源码级连通断言）。"""
    import inspect

    from src.coara.root import RootCoara

    source = inspect.getsource(RootCoara.start_idle_timeout_watcher)
    assert "curator_tick" in source
    assert "src.records.daily_curator" in source
