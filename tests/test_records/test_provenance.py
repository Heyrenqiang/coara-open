"""录像带区间回放（session_log.replay）与 record 溯源行为测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.records.facade import RecordsFacade
from src.records.store import RecordsStore
from src.session_log.replay import replay_dialogue
from src.session_log.store import append_with_seq, resolve_session_log_path
from src.session_log.types import build_event
from src.tools.builtin.records.record import RecordTool

WS = "D:/ws/demo"
SID = "sess-1"


def _seed_tape(coara_home: Path, rows: list[tuple[float, str, str]]) -> Path:
    """rows: (ts, kind_short, content)；kind_short ∈ user/assistant/sub。"""
    tape = resolve_session_log_path(WS, coara_home=coara_home)

    def factory(start: int):
        events = []
        for i, (ts, kind, content) in enumerate(rows):
            if kind == "user":
                k, agent = "user/message", "main"
            elif kind == "assistant":
                k, agent = "assistant/message", "main"
            else:
                k, agent = "assistant/message", "subagent"
            events.append(
                build_event(
                    kind=k,
                    seq=start + i,
                    session_id=SID,
                    payload={"content": content},
                    coara_name="考拉",
                    agent_kind=agent,
                    ts=ts,
                )
            )
        return events

    append_with_seq(tape, factory)
    return tape


@pytest.fixture
def taped_ws(tmp_path):
    home = tmp_path / "home"
    base = time.time() - 7200
    rows = [
        (base, "user", "两小时前的提问"),
        (base + 10, "assistant", "两小时前的答复"),
        (base + 3600, "user", "一小时前的提问"),
        (base + 3610, "assistant", "一小时前的答复"),
        (base + 3620, "sub", "子智能体过程话"),
    ]
    _seed_tape(home, rows)
    return home, base


def test_replay_window_and_buffer(taped_ws):
    home, base = taped_ws
    # 只给 1 小时前那个点，缓冲 300s 应覆盖该轮，不含 2 小时前的内容
    text = replay_dialogue(WS, ts_end=base + 3610, buffer_seconds=300, coara_home=home)
    assert "一小时前的提问" in text
    assert "一小时前的答复" in text
    assert "两小时前的提问" not in text
    # 子智能体默认排除
    assert "子智能体过程话" not in text


def test_replay_range_includes_subagents_when_asked(taped_ws):
    home, base = taped_ws
    text = replay_dialogue(
        WS,
        ts_start=base,
        ts_end=base + 3620,
        buffer_seconds=0,
        coara_home=home,
        include_subagents=True,
    )
    assert "子智能体过程话" in text
    assert "两小时前的提问" in text


def test_replay_empty_window_and_missing_tape(taped_ws, tmp_path):
    home, base = taped_ws
    assert "无对话" in replay_dialogue(WS, ts_end=base - 99999, buffer_seconds=0, coara_home=home)
    assert replay_dialogue(WS, coara_home=home) == ""
    assert "暂无录像带" in replay_dialogue("D:/ws/nonexistent", ts_end=time.time(), coara_home=tmp_path / "x")


def test_replay_truncates_to_max_chars(taped_ws):
    home, base = taped_ws
    text = replay_dialogue(WS, ts_start=base, ts_end=base + 3620, buffer_seconds=0, max_chars=40, coara_home=home)
    assert "（前段略）" in text
    assert len(text) < 100


@pytest.fixture
def store(tmp_path):
    return RecordsStore(tmp_path / "records", agent_enabled=True)


@pytest.mark.asyncio
async def test_add_agent_persists_provenance(store):
    facade = RecordsFacade(store)
    now = time.time()
    result = await facade.add_agent(
        content="v8 空间修了缓存统计口径",
        workspace=WS,
        tape_start=now - 3600,
        tape_end=now,
    )
    assert result.ok
    entry = await store.agent.read(result.metadata["id"])
    assert entry is not None
    assert entry.source.workspace == WS
    assert entry.source.tape_start == now - 3600
    assert entry.source.tape_end == now


@pytest.mark.asyncio
async def test_read_source_replays_tape(store, taped_ws):
    home, base = taped_ws
    facade = RecordsFacade(store)
    result = await facade.add_agent(
        content="一小时前讨论了缓存命中率",
        workspace=WS,
        tape_start=base + 3500,
        tape_end=base + 3610,
    )
    assert result.ok
    # facade.read_source 内部自行解析录像带（默认 coara home）；单测改走 replay 验证坐标可用
    entry = await store.agent.read(result.metadata["id"])
    text = replay_dialogue(
        entry.source.workspace,
        ts_start=entry.source.tape_start,
        ts_end=entry.source.tape_end,
        coara_home=home,
    )
    assert "一小时前的提问" in text

    # 无坐标的老记录 → 明确报错
    plain = await facade.add_agent(content="没有坐标的老记录")
    no_src = await facade.read_source(plain.metadata["id"])
    assert no_src.is_error
    assert "无录像带溯源" in no_src.message


@pytest.mark.asyncio
async def test_record_tool_source_action(store, tmp_path):
    tool = RecordTool(store=store)
    missing = await tool.create_invocation({"action": "source", "id": "nope"}).execute()
    assert missing.is_error
    no_id = await tool.create_invocation({"action": "source"}).execute()
    assert no_id.is_error


def test_provenance_without_parent(store):
    tool = RecordTool(store=store)
    inv = tool.create_invocation({"action": "add", "content": "x"})
    assert inv._provenance() == {}


def test_provenance_stamps_workspace_and_time(store, tmp_path):
    class _Identity:
        class Persona:
            name = "coaras"

    class _Parent:
        workspace_dir = WS
        session_id = "s-9"
        identity = _Identity()

    tool = RecordTool(store=store, parent_coara=_Parent())
    inv = tool.create_invocation({"action": "add", "content": "x"})
    prov = inv._provenance()
    assert prov["workspace"] == WS
    assert prov["tape_start"] is None
    assert prov["tape_end"] > time.time() - 60
