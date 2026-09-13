"""devtools llm_mirror_reader：直接读盘归并每实例最后一轮，脱离 agent 进程。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.devtools.llm_mirror_reader import (
    DAILY_WORKSPACE_KEY,
    FLOW_WORKSPACE_KEY,
    collect_latest,
    summarize,
)

# 镜像窗口 _MIRROR_MAX_AGE_DAYS=7：用"现在"附近的动态时间，避免硬编码日期随时间推移
# 越过窗口被 _prune_agents 误裁（否则测试会随日期变而红）。
_RECENT = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
_STALE = "2020-01-01T00:00:00+00:00"


def _entry(agent_id: str, content: str, ts: str, **extra) -> dict:
    e = {
        "ts": ts,
        "agent_id": agent_id,
        "kind": "coaras",
        "overview": "后台 · coaras · 可编辑",
        "user_input": content,
        "llm_call": 1,
        "tool_call_count": 0,
        "model": "m",
        "tool_count": 0,
        "system_prompt": "sys",
        "conversation": [{"round": 1, "user": content}],
        "response": {"content": content, "finish_reason": "stop"},
    }
    e.update(extra)
    return e


def _write_mirror(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def test_collect_latest_merges_per_agent(tmp_path) -> None:
    ws = tmp_path / "ws1"
    mirror = ws / ".coara" / "llm" / "llm-calls.jsonl"
    _write_mirror(
        mirror,
        [
            _entry("root", "第一轮", _RECENT),
            _entry("root", "第二轮", _RECENT),
            _entry("sa-x1", "子智能体", _RECENT),
        ],
    )
    collected = collect_latest(tmp_path / "home", [("工作区一", ws)])
    key = str(ws.resolve())
    assert key in collected
    assert collected[key]["root"]["user_input"] == "第二轮"  # 同实例后出现覆盖先前
    assert collected[key]["sa-x1"]["user_input"] == "子智能体"


def test_collect_latest_includes_pseudo_keys(tmp_path) -> None:
    home = tmp_path / "home"
    pseudo_dir = home / ".coara" / "llm"
    _write_mirror(pseudo_dir / "llm-calls-flow.jsonl", [_entry("root", "flow 回复", _RECENT)])
    _write_mirror(pseudo_dir / "llm-calls-daily.jsonl", [_entry("sa-daily-x1", "daily 回复", _RECENT)])
    collected = collect_latest(home, [])
    assert FLOW_WORKSPACE_KEY in collected
    assert DAILY_WORKSPACE_KEY in collected
    assert collected[FLOW_WORKSPACE_KEY]["root"]["user_input"] == "flow 回复"


def test_collect_latest_prunes_stale(tmp_path) -> None:
    ws = tmp_path / "ws1"
    mirror = ws / ".coara" / "llm" / "llm-calls.jsonl"
    _write_mirror(
        mirror,
        [
            _entry("root", "新", _RECENT),
            _entry("sa-old", "旧", _STALE),
        ],
    )
    collected = collect_latest(tmp_path / "home", [("w", ws)])
    agents = collected[str(ws.resolve())]
    assert "root" in agents
    assert "sa-old" not in agents  # 超龄淘汰


def test_collect_latest_skips_bad_and_legacy_lines(tmp_path) -> None:
    ws = tmp_path / "ws1"
    mirror = ws / ".coara" / "llm" / "llm-calls.jsonl"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"ts": _RECENT, "agent_id": "root", "user_input": "旧摘要"}
    mirror.write_text(
        "{bad json\n" + json.dumps(legacy, ensure_ascii=False) + "\n"
        + json.dumps(_entry("root", "正常", _RECENT), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    collected = collect_latest(tmp_path / "home", [("w", ws)])
    agents = collected[str(ws.resolve())]
    assert agents["root"]["user_input"] == "正常"  # 坏行/旧摘要行跳过，正常行在


def test_summarize_strips_big_fields(tmp_path) -> None:
    entry = _entry("root", "x" * 300, _RECENT, error="boom")
    s = summarize(entry)
    assert s["agent_id"] == "root"
    assert s["has_error"] is True
    assert len(s["user_input"]) == 201  # 200 + 省略号
    assert "system_prompt" not in s
    assert "conversation" not in s
    assert "response" not in s
