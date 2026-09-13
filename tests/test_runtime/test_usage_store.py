from __future__ import annotations

import json
from pathlib import Path

from src.core.events import TraceEvent
from src.runtime.usage_collector import UsageCollector
from src.runtime.usage_query import summarize_session, summarize_usage
from src.runtime.usage_store import UsageStore, UsageStoreConfig


def test_usage_store_appends_in_background(tmp_path: Path) -> None:
    events_path = tmp_path / "usage" / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    store.record({"kind": "tool", "tool": "web_search", "session_id": "s1"})
    store.flush(timeout=2.0)
    store.close(timeout=2.0)
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["kind"] == "tool"
    assert payload["tool"] == "web_search"
    assert payload["ts"]


def test_usage_collector_records_llm_and_tool(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)

    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="tool_complete",
            message="done",
            payload={
                "tool_name": "web_fetch",
                "tool_call_id": "tc1",
                "session_id": "sess-a",
                "agent_kind": "root",
                "duration_ms": 12.5,
                "is_error": False,
                "usage_args": {"url": "https://example.com"},
                "fetch_meta": {"quality": 0.8, "chars": 900},
            },
        )
    )
    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="llm_turn_complete",
            message="llm",
            payload={
                "session_id": "sess-a",
                "agent_kind": "root",
                "model": "mimo-v2.5-pro",
                "provider": "xiaomi",
                "has_tool_calls": True,
                "llm_output": {"usage": {"input_tokens": 100, "output_tokens": 20}},
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    summary = summarize_usage(events_path, days=7)
    assert summary.llm_turns == 1
    assert summary.tool_calls == 1
    assert summary.input_tokens == 100
    assert summary.output_tokens == 20
    assert summary.tools["web_fetch"] == 1

    session = summarize_session(events_path, "sess-a")
    assert session["llm_turns"] == 1
    assert session["tools"]["web_fetch"] == 1


def test_usage_collector_records_partial_llm_turn(tmp_path: Path) -> None:
    """流式中途失败的 attempt：已计量 token 以 partial 形态入账，与正常回合同口径聚合。"""
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)

    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="llm_turn_partial",
            message="partial",
            payload={
                "session_id": "sess-p",
                "agent_kind": "root",
                "model": "mimo-v2.5-pro",
                "provider": "xiaomi",
                "usage": {"input_tokens": 500, "output_tokens": 12},
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "llm_turn"
    assert rows[0]["status"] == "partial"

    summary = summarize_usage(events_path, days=7)
    assert summary.llm_turns == 1
    assert summary.input_tokens == 500
    assert summary.output_tokens == 12


def test_usage_collector_skips_partial_without_usage(tmp_path: Path) -> None:
    """provider 未返回任何 usage 的失败 attempt 不入账（无账可记）。"""
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)

    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="llm_turn_partial",
            message="partial",
            payload={"session_id": "sess-p", "agent_kind": "root", "usage": {}},
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    assert not events_path.exists() or not events_path.read_text(encoding="utf-8").strip()


def test_usage_collector_marks_silent_truncation_turn_partial(tmp_path: Path) -> None:
    """无 finish chunk 的静默截断（finish_reason=partial）：llm_turn 按 partial 口径入账。"""
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)

    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="llm_turn_complete",
            message="llm",
            payload={
                "session_id": "sess-t",
                "agent_kind": "root",
                "model": "MiniMax-M3",
                "provider": "minimax",
                "has_tool_calls": False,
                "llm_output": {
                    "finish_reason": "partial",
                    "usage": {"input_tokens": 500, "output_tokens": 7},
                },
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "llm_turn"
    assert rows[0]["status"] == "partial"

    # 聚合口径与正常回合一致
    summary = summarize_usage(events_path, days=7)
    assert summary.llm_turns == 1
    assert summary.input_tokens == 500
    assert summary.output_tokens == 7


def test_usage_collector_normal_finish_turn_has_no_partial_status(tmp_path: Path) -> None:
    """正常 finish_reason 的回合不带 partial 标记。"""
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=64))
    collector = UsageCollector(store)

    collector.handle_event(
        TraceEvent(
            coara_id="root",
            coara_name="Root",
            event_type="llm_turn_complete",
            message="llm",
            payload={
                "session_id": "sess-n",
                "agent_kind": "root",
                "model": "MiniMax-M3",
                "provider": "minimax",
                "has_tool_calls": False,
                "llm_output": {
                    "finish_reason": "end_turn",
                    "usage": {"input_tokens": 500, "output_tokens": 9},
                },
            },
        )
    )
    store.flush(timeout=2.0)
    store.close(timeout=2.0)

    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert "status" not in rows[0]


def test_usage_store_drops_when_queue_full(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    store = UsageStore(events_path, config=UsageStoreConfig(enabled=True, max_queue=2))
    store.record({"kind": "a"})
    store.record({"kind": "b"})
    store.record({"kind": "c"})
    assert store.dropped_events >= 1


def test_parse_usage_chat_args() -> None:
    from src.runtime.usage_query import parse_usage_chat_args

    assert parse_usage_chat_args("/usage") == ("session", 7)
    assert parse_usage_chat_args("/usage 7") == ("window", 7)
    assert parse_usage_chat_args("/usage session") == ("session", 7)
    assert parse_usage_chat_args("/usage week") == ("window", 7)


def test_summarize_usage_skips_blocked_fetch_quality(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "tool", "fetch": {"quality": 0.9, "chars": 1000}}),
                json.dumps({"kind": "tool", "fetch": {"quality": 0.1, "blocked": "duplicate_url"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = summarize_usage(events_path, days=7)
    assert summary.fetch_quality_count == 1
    assert summary.fetch_blocked["duplicate_url"] == 1
    assert summary.fetch_quality_sum == 0.9


def test_summarize_usage_search_providers(tmp_path: Path) -> None:
    from src.runtime.usage_query import format_session_usage_lines, summarize_session_stats

    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "tool",
                        "tool": "web_search",
                        "session_id": "sess-search",
                        "search": {
                            "result_count": 8,
                            "provider_outcomes": [
                                {"name": "serper", "status": "ok", "count": 5},
                                {"name": "linkup", "status": "error", "error": "402"},
                                {"name": "baidu", "status": "ok", "count": 3},
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "kind": "tool",
                        "tool": "web_search",
                        "session_id": "sess-search",
                        "search": {
                            "result_count": 4,
                            "provider_outcomes": [
                                {"name": "serper", "status": "ok", "count": 4},
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_usage(events_path, days=7)
    assert summary.search_calls == 2
    assert summary.search_result_count_sum == 12
    assert summary.search_provider_ok["serper"] == 2
    assert summary.search_provider_ok["baidu"] == 1
    assert summary.search_provider_error["linkup"] == 1

    session = summarize_session_stats(events_path, "sess-search")
    lines = format_session_usage_lines(session)
    joined = "\n".join(lines)
    assert "联网搜索：2 次，平均 6.0 条结果" in joined
    assert "搜索渠道：baidu 成功×1、linkup 失败×1、serper 成功×2" in joined
