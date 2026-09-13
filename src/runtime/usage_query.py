"""Offline aggregation over usage/events.jsonl (read-only, no runtime impact).

用量三层：事件落盘（不动）→ 按原文 provider/model 聚合 → 响应里贴展示字段
（``usage_display``）。本模块只做读与聚合，不改写 jsonl。
"""

from __future__ import annotations

import heapq
import json
import re
from collections import Counter, OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from src.core.coara_home import resolve_coara_home
from src.llm.usage import cache_read_tokens, total_prompt_tokens
from src.runtime.usage_display import (
    cost_state,
    decorate_token_totals,
    format_day_display,
    format_token_count,
    format_ts_display,
    format_usage_money,
)
from src.runtime.usage_store import resolve_usage_events_path


@dataclass(slots=True)
class UsageSummary:
    events_path: Path
    days: int
    llm_turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    fetch_blocked: Counter[str] = field(default_factory=Counter)
    fetch_quality_sum: float = 0.0
    fetch_quality_count: int = 0
    search_calls: int = 0
    search_result_count_sum: int = 0
    search_provider_ok: Counter[str] = field(default_factory=Counter)
    search_provider_empty: Counter[str] = field(default_factory=Counter)
    search_provider_error: Counter[str] = field(default_factory=Counter)
    sessions: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SessionUsageSummary:
    session_id: str
    events_path: Path
    llm_turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    tools: Counter[str] = field(default_factory=Counter)
    fetch_blocked: Counter[str] = field(default_factory=Counter)
    fetch_quality_sum: float = 0.0
    fetch_quality_count: int = 0
    search_calls: int = 0
    search_result_count_sum: int = 0
    search_provider_ok: Counter[str] = field(default_factory=Counter)
    search_provider_empty: Counter[str] = field(default_factory=Counter)
    search_provider_error: Counter[str] = field(default_factory=Counter)


class _UsageBucket(Protocol):
    llm_turns: int
    tool_calls: int
    tool_errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    tools: Counter[str]
    fetch_blocked: Counter[str]
    fetch_quality_sum: float
    fetch_quality_count: int
    search_calls: int
    search_result_count_sum: int
    search_provider_ok: Counter[str]
    search_provider_empty: Counter[str]
    search_provider_error: Counter[str]


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Legacy UsageStore stamped naive local time; treat as local wall clock.
        local_tz = datetime.now().astimezone().tzinfo or UTC
        dt = dt.replace(tzinfo=local_tz).astimezone(UTC)
    return dt


def iter_usage_events(events_path: Path) -> Iterator[dict[str, Any]]:
    yield from _load_events_cached(events_path)


# 解析结果按 (路径, 大小, mtime) 缓存：轮转归档可达数十 MB（v8 实测 70MB / 5.9 万条），
# 每次请求从头解析要秒级。文件只在末尾追加，stat 一变就重读；LRU 限制文件数，避免
# 长驻进程把多份历史同时留在内存里（每个文件约几十 MB 的 dict）。
_PARSE_CACHE_MAX_FILES = 6
_parse_cache: OrderedDict[tuple[str, int, int], list[dict[str, Any]]] = OrderedDict()


def _load_events_cached(events_path: Path) -> list[dict[str, Any]]:
    """读一个事件文件的全部记录（带 stat 键缓存）。

    返回的是缓存里的同一个列表——调用方只读，不得原地修改记录。
    """
    try:
        stat = events_path.stat()
    except OSError:
        return []
    key = (str(events_path), stat.st_size, stat.st_mtime_ns)
    cached = _parse_cache.get(key)
    if cached is not None:
        _parse_cache.move_to_end(key)
        return cached
    records: list[dict[str, Any]] = []
    try:
        with events_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    _parse_cache[key] = records
    while len(_parse_cache) > _PARSE_CACHE_MAX_FILES:
        _parse_cache.popitem(last=False)
    return records


def _cache_read_from_usage(usage: dict[str, Any]) -> int:
    """Prefer Anthropic ``cache_read_input_tokens``, else OpenAI ``cached_tokens`` (not both)."""
    from src.llm.usage import cache_read_tokens

    return cache_read_tokens(usage)


def _prompt_tokens_from_usage(usage: dict[str, Any]) -> int:
    """Effective total prompt size (Anthropic: input+cache_read+creation; OpenAI: input).

    llmlog 镜像用 ``prompt_tokens``，events 用 ``input_tokens``；此处统一。
    """
    from src.llm.usage import total_prompt_tokens

    if not isinstance(usage, dict) or not usage:
        return 0
    if usage.get("input_tokens") is None and usage.get("prompt_tokens") is not None:
        usage = {
            **usage,
            "input_tokens": int(usage.get("prompt_tokens") or 0),
        }
    return total_prompt_tokens(usage)


def _accumulate_search(bucket: _UsageBucket, record: dict[str, Any]) -> None:
    if str(record.get("tool") or "") != "web_search":
        return
    search = record.get("search") or {}
    if not isinstance(search, dict):
        return
    bucket.search_calls += 1
    result_count = search.get("result_count")
    if isinstance(result_count, int):
        bucket.search_result_count_sum += result_count
    for item in search.get("provider_outcomes") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status == "ok":
            bucket.search_provider_ok[name] += 1
        elif status == "empty":
            bucket.search_provider_empty[name] += 1
        elif status == "error":
            bucket.search_provider_error[name] += 1


def _accumulate_record(bucket: _UsageBucket, record: dict[str, Any]) -> None:
    kind = record.get("kind")
    if kind == "llm_turn":
        bucket.llm_turns += 1
        usage = record.get("usage") or {}
        bucket.input_tokens += _prompt_tokens_from_usage(usage)
        bucket.output_tokens += int(usage.get("output_tokens") or 0)
        bucket.cache_read_tokens += _cache_read_from_usage(usage)
        return
    if kind != "tool":
        return
    bucket.tool_calls += 1
    if record.get("is_error"):
        bucket.tool_errors += 1
    tool_name = str(record.get("tool") or "")
    if tool_name:
        bucket.tools[tool_name] += 1
    _accumulate_search(bucket, record)
    fetch = record.get("fetch") or {}
    blocked = fetch.get("blocked")
    if blocked:
        bucket.fetch_blocked[str(blocked)] += 1
        return
    quality = fetch.get("quality")
    if isinstance(quality, (int, float)):
        bucket.fetch_quality_sum += float(quality)
        bucket.fetch_quality_count += 1


def summarize_usage(events_path: Path, *, days: int = 7) -> UsageSummary:
    summary = UsageSummary(events_path=events_path, days=max(1, days))
    if not events_path.is_file():
        return summary
    cutoff = datetime.now(tz=UTC) - timedelta(days=summary.days)
    for record in iter_usage_events(events_path):
        ts = _parse_ts(str(record.get("ts") or ""))
        if ts is not None and ts < cutoff:
            continue
        session_id = str(record.get("session_id") or "")
        if session_id:
            summary.sessions.add(session_id)
        _accumulate_record(summary, record)
    return summary


def _iter_session_records(events_path: Path, session_id: str) -> Iterator[dict[str, Any]]:
    if not session_id or not events_path.is_file():
        return
    for record in iter_usage_events(events_path):
        if str(record.get("session_id") or "") == session_id:
            yield record


def summarize_session_stats(events_path: Path, session_id: str) -> SessionUsageSummary:
    summary = SessionUsageSummary(session_id=session_id, events_path=events_path)
    for record in _iter_session_records(events_path, session_id):
        _accumulate_record(summary, record)
    return summary


def summarize_session(events_path: Path, session_id: str) -> dict[str, Any]:
    stats = SessionUsageSummary(session_id=session_id, events_path=events_path)
    rows: list[dict[str, Any]] = []
    for record in _iter_session_records(events_path, session_id):
        rows.append(record)
        _accumulate_record(stats, record)
    return {
        "session_id": session_id,
        "events": len(rows),
        "llm_turns": stats.llm_turns,
        "usage_totals": {
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
            "cache_read_input_tokens": stats.cache_read_tokens,
        },
        "tools": dict(stats.tools),
        "rows": rows[-40:],
    }


def resolve_default_usage_path(workspace_dir: Path | None = None, *, coara_home: Path | None = None) -> Path:
    anchor = workspace_dir or Path.cwd()
    home = resolve_coara_home(anchor, coara_home)
    return resolve_usage_events_path(anchor, coara_home=home)


def session_cache_hit_ratio(
    events_path: Path,
    session_id: str,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> float | None:
    """当前会话 Σ cache_read / Σ prompt（与 WebUI /usage session 同源同口径）。

    只回读本会话文件尾部（默认 8MB），供 CLI toolbar 等展示层复用；
    events.jsonl 是唯一事实源，避免各端自建累计造成口径漂移。
    """
    if not session_id or not events_path.is_file():
        return None
    cache_sum = 0
    prompt_sum = 0
    try:
        with events_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            tail = handle.read()
    except OSError:
        return None
    # 先用 session_id 字节子串预过滤：8MB 尾部全量 json.loads 约 80ms（GIL 占用，
    # 工具栏 5s TTL 刷新会周期性卡顿输入），预过滤后只解析本会话的行。
    needle = session_id.encode("utf-8", errors="ignore")
    for raw in tail.splitlines():
        line = raw.strip()
        if not line or (needle and needle not in line):
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("kind") != "llm_turn":
            continue
        if str(record.get("session_id") or "") != session_id:
            continue
        usage = record.get("usage") or {}
        prompt = total_prompt_tokens(usage)
        if prompt <= 0:
            continue
        prompt_sum += prompt
        cache_sum += cache_read_tokens(usage)
    if prompt_sum <= 0 or cache_sum <= 0:
        return None
    return cache_sum / prompt_sum


def summarize_session_token_cost(
    session_id: str,
    *,
    coara_home: Path | None = None,
    workspace_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """单会话累计 token + 费用（与 dashboard 同口径，含 *_display）。

    优先扫 ``workspace_dir`` 对应 events.jsonl；未指定或伪空间则扫全部用量文件。
    无 llm_turn 记录时返回 None。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    from src.runtime.usage_pricing import load_pricing_map

    pricing_map = load_pricing_map(coara_home)
    bucket = _TokenBucket()
    paths: list[Path] = []
    if workspace_dir:
        raw = str(workspace_dir).strip()
        # 伪键（工作流/日报）没有物理 workspace_dir，走全量扫描
        if raw and not raw.startswith("工作流") and not raw.startswith("日报"):
            try:
                paths = [resolve_usage_events_path(Path(raw), coara_home=coara_home)]
            except Exception:
                paths = []
    if not paths:
        paths = iter_all_usage_event_paths(coara_home)

    for events_path in paths:
        if not events_path.is_file():
            continue
        for record in _iter_session_records(events_path, sid):
            if record.get("kind") != "llm_turn":
                continue
            _add_llm_turn(bucket, record, pricing_map)

    if bucket.llm_turns <= 0:
        return None
    return _token_dict(bucket)


def enrich_usage_with_cost(
    usage: dict[str, Any] | None,
    *,
    provider: str | None,
    model: str | None,
    coara_home: Path | None = None,
) -> dict[str, Any] | None:
    """给单次调用 usage 挂上 cost_* / *_display（就地改并返回；无 usage 则 None）。"""
    if not isinstance(usage, dict) or not usage:
        return None
    from src.runtime.usage_pricing import compute_turn_cost, load_pricing_map, pricing_is_configured

    pricing_map = load_pricing_map(coara_home)
    provider_s = str(provider or "").strip()
    model_s = str(model or "").strip()
    model_key = f"{provider_s}/{model_s}" if provider_s and model_s else (model_s or provider_s)
    pricing = pricing_map.get(model_key) if pricing_map else None
    # compute_turn_cost 认 input_tokens/output_tokens；llmlog 镜像用 prompt/completion
    normalized = {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or 0),
    }
    cost = compute_turn_cost(normalized, pricing)
    usage["cost_miss"] = round(cost["cost_miss"], 6)
    usage["cost_hit"] = round(cost["cost_hit"], 6)
    usage["cost_out"] = round(cost["cost_out"], 6)
    usage["cost_total"] = round(cost["cost_total"], 6)
    usage["pricing_configured"] = bool(pricing is not None and pricing_is_configured(pricing))
    usage["cost_total_display"] = format_usage_money(usage["cost_total"])
    usage["cost_miss_display"] = format_usage_money(usage["cost_miss"])
    usage["cost_hit_display"] = format_usage_money(usage["cost_hit"])
    usage["cost_out_display"] = format_usage_money(usage["cost_out"])
    # 有效输入（含 cache_read/creation）与计费口径一致，供 unpriced 判定
    effective_in = _prompt_tokens_from_usage(normalized)
    usage["cost_state"] = cost_state(usage["cost_total"], effective_in)
    usage["input_display"] = format_token_count(effective_in)
    usage["output_display"] = format_token_count(normalized["output_tokens"])
    return usage


def _usage_dict_from_event(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """events.jsonl 的 usage → llmlog 风格 prompt/completion 字典。"""
    if not isinstance(usage, dict) or not usage:
        return None
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (prompt + completion)
    if not any((prompt, completion, total)):
        return None
    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens", "cached_tokens"):
        val = int(usage.get(key) or 0)
        if val:
            out[key] = val
    return out


def _collect_session_llm_usages(
    session_id: str,
    *,
    coara_home: Path | None,
    workspace_dir: Path | str | None,
) -> list[dict[str, Any]]:
    """按时间序收集本会话全部 llm_turn usage（llmlog 字段风格）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    paths: list[Path] = []
    if workspace_dir:
        raw = str(workspace_dir).strip()
        if raw and not raw.startswith("工作流") and not raw.startswith("日报"):
            try:
                paths = [resolve_usage_events_path(Path(raw), coara_home=coara_home)]
            except Exception:
                paths = []
    if not paths:
        paths = iter_all_usage_event_paths(coara_home)
    rows: list[tuple[str, dict[str, Any]]] = []
    for events_path in paths:
        if not events_path.is_file():
            continue
        for record in _iter_session_records(events_path, sid):
            if record.get("kind") != "llm_turn":
                continue
            usage = _usage_dict_from_event(record.get("usage") if isinstance(record.get("usage"), dict) else None)
            if usage is None:
                continue
            rows.append((str(record.get("ts") or ""), usage))
    rows.sort(key=lambda r: r[0])
    return [u for _, u in rows]


def enrich_llm_log_detail(
    payload: dict[str, Any],
    *,
    coara_home: Path | None = None,
    workspace_dir: Path | str | None = None,
) -> dict[str, Any]:
    """给 LLM log 详情挂小轮/回合/会话费用（就地改 payload 并返回）。

    - 优先用镜像里已戳的 iteration usage；缺口时用 events.jsonl 按时间对齐补齐
    - 当前 response 视为最后一回合的末尾小轮（写盘时尚未入 conversation）
    - 回合费用 = 其下小轮之和；会话费用 = 各回合之和
    """
    provider = payload.get("provider_name") or payload.get("provider")
    model = payload.get("model")
    conversation = payload.get("conversation")
    if not isinstance(conversation, list):
        conversation = []
        payload["conversation"] = conversation

    flat: list[dict[str, Any]] = []
    for round_obj in conversation:
        if not isinstance(round_obj, dict):
            continue
        for it in round_obj.get("iterations") or []:
            if isinstance(it, dict):
                flat.append(it)

    response = payload.get("response") if isinstance(payload.get("response"), dict) else None
    response_usage = response.get("usage") if response and isinstance(response.get("usage"), dict) else None

    missing = [it for it in flat if not isinstance(it.get("usage"), dict)]
    if missing or (response is not None and not response_usage):
        event_usages = _collect_session_llm_usages(
            str(payload.get("session_id") or ""),
            coara_home=coara_home,
            workspace_dir=workspace_dir or payload.get("workspace"),
        )
        slots = len(flat) + (1 if response is not None else 0)
        if event_usages and slots > 0:
            take = event_usages[-slots:] if len(event_usages) >= slots else list(event_usages)
            # 右对齐：末尾对应最新小轮 / 当前 response
            offset = slots - len(take)
            for i, usage in enumerate(take):
                slot = offset + i
                if slot < len(flat):
                    if not isinstance(flat[slot].get("usage"), dict):
                        flat[slot]["usage"] = dict(usage)
                elif response is not None and not response_usage:
                    response["usage"] = dict(usage)
                    response_usage = response["usage"]

    session_cost = 0.0
    session_in = 0
    session_out = 0
    session_cache = 0
    session_iters = 0
    session_unpriced_any = False

    for round_obj in conversation:
        if not isinstance(round_obj, dict):
            continue
        round_cost = 0.0
        round_in = 0
        round_out = 0
        round_cache = 0
        round_iters = 0
        round_unpriced_any = False
        for it in round_obj.get("iterations") or []:
            if not isinstance(it, dict):
                continue
            usage = it.get("usage")
            if not isinstance(usage, dict):
                continue
            enrich_usage_with_cost(usage, provider=provider, model=model, coara_home=coara_home)
            round_cost += float(usage.get("cost_total") or 0)
            # 与计费一致：有效输入 = total_prompt_tokens（含 cache_read/creation）
            round_in += _prompt_tokens_from_usage(usage)
            round_out += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            round_cache += _cache_read_from_usage(usage)
            round_iters += 1
            if usage.get("cost_state") == "unpriced":
                round_unpriced_any = True
                session_unpriced_any = True
        round_obj["cost_total"] = round(round_cost, 6)
        round_obj["cost_total_display"] = format_usage_money(round_cost)
        round_obj["input_tokens"] = round_in
        round_obj["output_tokens"] = round_out
        round_obj["cache_read_tokens"] = round_cache
        round_obj["input_display"] = format_token_count(round_in)
        round_obj["output_display"] = format_token_count(round_out)
        round_obj["llm_turns"] = round_iters
        round_obj["cost_state"] = (
            "priced" if round_cost > 0 else ("unpriced" if round_iters and round_unpriced_any else "zero")
        )
        session_cost += round_cost
        session_in += round_in
        session_out += round_out
        session_cache += round_cache
        session_iters += round_iters

    # 当前 response = 最后一回合的末尾小轮（尚未写入 conversation）
    if response is not None and isinstance(response.get("usage"), dict):
        usage = response["usage"]
        enrich_usage_with_cost(usage, provider=provider, model=model, coara_home=coara_home)
        add_cost = float(usage.get("cost_total") or 0)
        add_in = _prompt_tokens_from_usage(usage)
        add_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        add_cache = _cache_read_from_usage(usage)
        session_cost += add_cost
        session_in += add_in
        session_out += add_out
        session_cache += add_cache
        session_iters += 1
        resp_unpriced = usage.get("cost_state") == "unpriced"
        if resp_unpriced:
            session_unpriced_any = True
        if conversation:
            last = conversation[-1]
            if isinstance(last, dict):
                last["cost_total"] = round(float(last.get("cost_total") or 0) + add_cost, 6)
                last["cost_total_display"] = format_usage_money(last["cost_total"])
                last["input_tokens"] = int(last.get("input_tokens") or 0) + add_in
                last["output_tokens"] = int(last.get("output_tokens") or 0) + add_out
                last["cache_read_tokens"] = int(last.get("cache_read_tokens") or 0) + add_cache
                last["input_display"] = format_token_count(last["input_tokens"])
                last["output_display"] = format_token_count(last["output_tokens"])
                last["llm_turns"] = int(last.get("llm_turns") or 0) + 1
                last_unpriced = resp_unpriced or any(
                    isinstance(it, dict)
                    and isinstance(it.get("usage"), dict)
                    and it["usage"].get("cost_state") == "unpriced"
                    for it in (last.get("iterations") or [])
                )
                last["cost_state"] = (
                    "priced"
                    if float(last["cost_total"]) > 0
                    else ("unpriced" if last["llm_turns"] and last_unpriced else "zero")
                )

    cache_hit_rate = (session_cache / session_in) if session_in > 0 else None
    payload["session_cost"] = {
        "llm_turns": session_iters,
        "rounds": len([r for r in conversation if isinstance(r, dict)]),
        "input_tokens": session_in,
        "output_tokens": session_out,
        "cache_read_tokens": session_cache,
        "cache_hit_rate": round(cache_hit_rate, 4) if cache_hit_rate is not None else None,
        "input_display": format_token_count(session_in),
        "output_display": format_token_count(session_out),
        "cost_total": round(session_cost, 6),
        "cost_total_display": format_usage_money(session_cost),
        "cost_state": (
            "priced" if session_cost > 0 else ("unpriced" if session_iters and session_unpriced_any else "zero")
        ),
    }
    # 兼容先前挂的 session_usage（events 聚合）；有 session_cost 时前端优先用它
    return payload


def resolve_usage_path_for_root(root: Any) -> Path:
    from src.core.config import config_manager

    coara_home = config_manager.config.coara_home if config_manager._config is not None else None
    return resolve_default_usage_path(root.foreground_coara.workspace_dir, coara_home=coara_home)


def format_summary_text(summary: UsageSummary) -> str:
    return "\n".join(_format_summary_lines(summary))


def _format_tool_line(tools: Counter[str], *, limit: int = 10) -> str | None:
    if not tools:
        return None
    return ", ".join(f"{name}×{count}" for name, count in tools.most_common(limit))


def _format_fetch_line(
    *,
    fetch_blocked: Counter[str],
    fetch_quality_sum: float,
    fetch_quality_count: int,
) -> list[str]:
    lines: list[str] = []
    if fetch_quality_count:
        avg_q = fetch_quality_sum / fetch_quality_count
        lines.append(f"网页抓取质量均分：{avg_q:.2f}（成功 {fetch_quality_count} 次）")
    if fetch_blocked:
        blocked = "、".join(f"{name}×{count}" for name, count in fetch_blocked.most_common())
        lines.append(f"网页抓取被拦：{blocked}")
    return lines


def _format_provider_outcome_stats(
    *,
    ok: Counter[str],
    empty: Counter[str],
    error: Counter[str],
) -> str | None:
    names = sorted(set(ok) | set(empty) | set(error))
    if not names:
        return None
    parts: list[str] = []
    for name in names:
        if ok[name]:
            parts.append(f"{name} 成功×{ok[name]}")
        if empty[name]:
            parts.append(f"{name} 空结果×{empty[name]}")
        if error[name]:
            parts.append(f"{name} 失败×{error[name]}")
    return "、".join(parts)


def _format_search_line(
    *,
    search_calls: int,
    search_result_count_sum: int,
    search_provider_ok: Counter[str],
    search_provider_empty: Counter[str],
    search_provider_error: Counter[str],
) -> list[str]:
    if search_calls <= 0:
        return []
    lines: list[str] = []
    if search_result_count_sum:
        avg_results = search_result_count_sum / search_calls
        lines.append(f"联网搜索：{search_calls} 次，平均 {avg_results:.1f} 条结果")
    else:
        lines.append(f"联网搜索：{search_calls} 次")
    provider_line = _format_provider_outcome_stats(
        ok=search_provider_ok,
        empty=search_provider_empty,
        error=search_provider_error,
    )
    if provider_line:
        lines.append(f"搜索渠道：{provider_line}")
    return lines


def _format_fetch_search_lines(summary: _UsageBucket) -> list[str]:
    lines: list[str] = []
    lines.extend(
        _format_fetch_line(
            fetch_blocked=summary.fetch_blocked,
            fetch_quality_sum=summary.fetch_quality_sum,
            fetch_quality_count=summary.fetch_quality_count,
        )
    )
    lines.extend(
        _format_search_line(
            search_calls=summary.search_calls,
            search_result_count_sum=summary.search_result_count_sum,
            search_provider_ok=summary.search_provider_ok,
            search_provider_empty=summary.search_provider_empty,
            search_provider_error=summary.search_provider_error,
        )
    )
    return lines


def _format_tool_usage_lines(summary: _UsageBucket, *, tools_label: str = "常用工具") -> list[str]:
    lines: list[str] = []
    tool_line = _format_tool_line(summary.tools)
    if tool_line:
        lines.append(f"{tools_label}：{tool_line}")
    lines.extend(_format_fetch_search_lines(summary))
    return lines


def _format_summary_lines(summary: UsageSummary) -> list[str]:
    lines = [
        f"时间范围：最近 {summary.days} 天",
        f"对话次数：{len(summary.sessions)}",
        f"模型回合：{summary.llm_turns}",
        f"工具调用：{summary.tool_calls}（失败 {summary.tool_errors}）",
        (
            "Token："
            f"输入 {summary.input_tokens:,} · "
            f"输出 {summary.output_tokens:,} · "
            f"缓存命中 {summary.cache_read_tokens:,}"
        ),
    ]
    lines.extend(_format_tool_usage_lines(summary))
    return lines


def format_session_usage_lines(summary: SessionUsageSummary) -> list[str]:
    lines = [
        f"模型回合：{summary.llm_turns}",
        f"工具调用：{summary.tool_calls}（失败 {summary.tool_errors}）",
        (
            "Token："
            f"输入 {summary.input_tokens:,} · "
            f"输出 {summary.output_tokens:,} · "
            f"缓存命中 {summary.cache_read_tokens:,}"
        ),
    ]
    lines.extend(_format_tool_usage_lines(summary))
    if summary.llm_turns == 0 and summary.tool_calls == 0:
        lines.append("（这一轮对话还没有用量记录，聊几句再看）")
    return lines


def parse_usage_chat_args(command: str) -> tuple[str, int]:
    """Return mode ('session' | 'window') and day count for /usage."""
    parts = command.strip().split()
    if len(parts) <= 1:
        return "session", 7
    arg = parts[1].lower()
    if arg in {"session", "s", "here", "now"}:
        return "session", 7
    if arg in {"week", "7d"}:
        return "window", 7
    if arg.isdigit():
        return "window", max(1, int(arg))
    return "session", 7


@dataclass(slots=True)
class _TokenBucket:
    llm_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    cost_miss: float = 0.0
    cost_hit: float = 0.0
    cost_out: float = 0.0
    cost_total: float = 0.0
    # 价格表里找不到价格的回合数：它们的费用按 0 计，必须显式暴露——
    # 否则「没花费」与「没配价」在端上长得一模一样（历史模型名换过好几轮）。
    unpriced_llm_turns: int = 0


def _hit_rate(cache_read: int, prompt_tokens: int) -> float:
    """聚合命中率：Σ cache_read / Σ prompt（公式同 llm.usage.cumulative_prompt_cache_hit_ratio）。

    dashboard 口径要 0.0 而非 None——聚合视图里"无命中"是确定的 0，不是未知。
    """
    return round(cache_read / prompt_tokens, 4) if prompt_tokens > 0 else 0.0


def _token_dict(bucket: _TokenBucket) -> dict[str, Any]:
    # bucket.input_tokens is effective prompt size (see _add_llm_turn).
    input_tokens = bucket.input_tokens
    cache_read = bucket.cache_read_tokens
    # 展示字段（*_display / cost_state / cache_hit_level 等）由 usage_display 单源注入，
    # Web / Android / devtools 只渲染，不再各自格式化
    return decorate_token_totals(
        {
            "llm_turns": bucket.llm_turns,
            "input_tokens": input_tokens,
            "output_tokens": bucket.output_tokens,
            "cache_read_tokens": cache_read,
            "reasoning_tokens": bucket.reasoning_tokens,
            "cache_hit_rate": _hit_rate(cache_read, input_tokens),
            "cost_miss": round(bucket.cost_miss, 6),
            "cost_hit": round(bucket.cost_hit, 6),
            "cost_out": round(bucket.cost_out, 6),
            "cost_total": round(bucket.cost_total, 6),
            "unpriced_llm_turns": bucket.unpriced_llm_turns,
        }
    )


def _add_llm_turn(
    bucket: _TokenBucket,
    record: dict[str, Any],
    pricing_map: dict[str, Any] | None = None,
) -> None:
    usage = record.get("usage") or {}
    bucket.llm_turns += 1
    bucket.input_tokens += _prompt_tokens_from_usage(usage)
    bucket.output_tokens += int(usage.get("output_tokens") or 0)
    bucket.cache_read_tokens += _cache_read_from_usage(usage)
    bucket.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
    if pricing_map:
        from src.runtime.usage_pricing import compute_turn_cost, lookup_pricing

        provider = str(record.get("provider") or "").strip()
        model = str(record.get("model") or "").strip()
        pricing = lookup_pricing(pricing_map, provider, model)
        if pricing is None:
            bucket.unpriced_llm_turns += 1
        cost = compute_turn_cost(usage, pricing)
        bucket.cost_miss += cost["cost_miss"]
        bucket.cost_hit += cost["cost_hit"]
        bucket.cost_out += cost["cost_out"]
        bucket.cost_total += cost["cost_total"]


def _usage_event_files(usage_dir: Path) -> list[Path]:
    """usage 目录下的全部事件文件（活动文件 + 全部轮转归档），按文件名排序。

    活动文件为 ``events.jsonl``；``usage_store`` 把写满的文件轮转为
    ``events.<时间戳>.jsonl`` 且永不删除，归档一并纳入统计——否则轮转等于
    历史费用蒸发（2026-09-12 实际发生过：v8 空间 99% 的历史被轮转归档后，
    当日费用从 ¥68.66 掉到 ¥6.18）。
    """
    if not usage_dir.is_dir():
        return []
    try:
        candidates = sorted(usage_dir.glob("events*.jsonl*"))
    except OSError:
        return []
    return [path for path in candidates if path.is_file()]


def iter_all_usage_event_paths(coara_home: Path | None) -> list[Path]:
    """List every usage event file under each workspace slot (+ legacy cwd fallback).

    一个工作空间可能有多份（活动文件 + 归档），读取顺序不影响聚合结果。
    """
    paths: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            return
        seen.add(resolved)
        if path.is_file():
            paths.append(path)

    def _add_dir(usage_dir: Path) -> None:
        for path in _usage_event_files(usage_dir):
            _add(path)

    if coara_home is not None:
        workspaces_root = Path(coara_home).expanduser().resolve() / "workspaces"
        if workspaces_root.is_dir():
            for child in sorted(workspaces_root.iterdir()):
                if child.is_dir():
                    _add_dir(child / "usage")
    # Local trial mode: usage under cwd when coara_home is unset.
    _add_dir(Path.cwd() / ".coara" / "usage")
    return paths


def _workspace_id_from_usage_path(events_path: Path) -> str:
    """``…/workspaces/<id>/usage/events.jsonl`` → ``<id>``."""
    parts = events_path.resolve().parts
    try:
        idx = parts.index("workspaces")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return ""


_WS_ID_DIGEST_RE = re.compile(r"^(.+)-([0-9a-f]{10})$", re.IGNORECASE)


def _slug_from_workspace_id(workspace_id: str) -> str | None:
    """``v8-bd788e1c25`` → ``v8``; ``nx-c34a1b2c3d`` → ``nx``."""
    match = _WS_ID_DIGEST_RE.match(workspace_id.strip())
    return match.group(1) if match else None


def _load_registry_workspace_aliases(coara_home: Path | None) -> dict[str, str]:
    """Map workspace storage id → registered alias."""
    if coara_home is None:
        return {}
    try:
        from src.workspace.registry import WorkspaceRegistry

        doc = WorkspaceRegistry(Path(coara_home)).load()
    except Exception:
        return {}
    return {
        str(entry_id): str(entry.name).strip()
        for entry_id, entry in doc.workspaces.items()
        if str(getattr(entry, "name", "") or "").strip()
    }


def resolve_workspace_display_name(
    workspace_id: str,
    *,
    aliases: dict[str, str] | None = None,
) -> str:
    """Single display rule: registry alias → path slug → storage id."""
    ws_id = (workspace_id or "").strip()
    if not ws_id:
        return "(unknown)"
    alias = (aliases or {}).get(ws_id)
    if alias:
        return alias
    return _slug_from_workspace_id(ws_id) or ws_id


def summarize_usage_dashboard(
    *,
    days: int = 7,
    workspace_id: str | None = None,
    coara_home: Path | None = None,
    recent_limit: int = 80,
    event_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Aggregate LLM token usage across workspaces for the Web dashboard."""
    from src.runtime.usage_attribution import AGENT_KIND_LABELS, parse_agent_kind
    from src.runtime.usage_display import load_model_display_map, resolve_model_label
    from src.runtime.usage_pricing import compute_turn_cost, load_pricing_map, pricing_is_configured

    window_days = max(1, int(days))
    cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
    filter_ws = str(workspace_id or "").strip() or None
    pricing_map = load_pricing_map(coara_home)
    # 展示层目录名：只贴 label，不改事件、不分桶键
    display_map = load_model_display_map(coara_home)

    totals = _TokenBucket()
    by_kind: dict[str, _TokenBucket] = {}
    by_workspace: dict[str, _TokenBucket] = {}
    by_day: dict[str, _TokenBucket] = {}
    by_model: dict[str, _TokenBucket] = {}
    model_labels: dict[str, str] = {}
    recent: list[dict[str, Any]] = []
    earliest: datetime | None = None
    latest: datetime | None = None

    paths = event_paths if event_paths is not None else iter_all_usage_event_paths(coara_home)
    aliases = _load_registry_workspace_aliases(coara_home)

    for events_path in paths:
        path_ws_id = _workspace_id_from_usage_path(events_path)
        for record in iter_usage_events(events_path):
            if record.get("kind") != "llm_turn":
                continue
            kind = parse_agent_kind(record.get("agent_kind"))
            if kind is None:
                continue
            ts = _parse_ts(str(record.get("ts") or ""))
            if ts is not None and ts < cutoff:
                continue

            rec_ws_id = str(record.get("workspace_id") or "").strip() or path_ws_id
            if filter_ws and rec_ws_id != filter_ws:
                continue

            kind_bucket = by_kind.setdefault(kind, _TokenBucket())
            ws_key = rec_ws_id or "(unknown)"
            ws_bucket = by_workspace.setdefault(ws_key, _TokenBucket())
            display_name = resolve_workspace_display_name(ws_key, aliases=aliases)

            provider = str(record.get("provider") or "").strip()
            model = str(record.get("model") or "").strip()
            if model or provider:
                # 分桶键 = 事件原文 provider/model（基础数据不动）
                model_key = f"{provider}/{model}" if provider and model else (model or provider)
                # label 仅展示用，不回写、不合并桶
                model_label = resolve_model_label(provider, model, display_map)
            else:
                model_key = "(unknown)"
                model_label = "（未知模型）"
            model_bucket = by_model.setdefault(model_key, _TokenBucket())
            model_labels[model_key] = model_label

            _add_llm_turn(totals, record, pricing_map)
            _add_llm_turn(kind_bucket, record, pricing_map)
            _add_llm_turn(ws_bucket, record, pricing_map)
            _add_llm_turn(model_bucket, record, pricing_map)

            if ts is not None:
                if earliest is None or ts < earliest:
                    earliest = ts
                if latest is None or ts > latest:
                    latest = ts
                day_key = ts.astimezone().date().isoformat()
                day_bucket = by_day.setdefault(day_key, _TokenBucket())
                _add_llm_turn(day_bucket, record, pricing_map)

            usage = record.get("usage") or {}
            model_key_for_cost = f"{provider}/{model}" if provider and model else (model or provider)
            pricing = pricing_map.get(model_key_for_cost) if pricing_map else None
            cost = compute_turn_cost(usage, pricing)
            recent_row = {
                "ts": record.get("ts") or "",
                "agent_kind": kind,
                "agent_label": AGENT_KIND_LABELS.get(kind, kind),
                "workspace_id": rec_ws_id,
                "workspace_name": display_name,
                "model": model,
                "provider": provider,
                "model_label": model_label if (model or provider) else "（未知模型）",
                "session_id": record.get("session_id") or "",
                "turn_id": record.get("turn_id") or "",
                "coara_name": record.get("coara_name") or "",
                "input_tokens": _prompt_tokens_from_usage(usage),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": _cache_read_from_usage(usage),
                "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                "pricing_configured": bool(pricing is not None and pricing_is_configured(pricing)),
                "cost_miss": cost["cost_miss"],
                "cost_hit": cost["cost_hit"],
                "cost_out": cost["cost_out"],
                "cost_total": cost["cost_total"],
            }
            recent_row["input_display"] = format_token_count(recent_row["input_tokens"])
            recent_row["output_display"] = format_token_count(recent_row["output_tokens"])
            recent_row["cache_read_display"] = format_token_count(recent_row["cache_read_tokens"])
            recent_row["reasoning_display"] = format_token_count(recent_row["reasoning_tokens"])
            recent_row["cost_total_display"] = format_usage_money(recent_row["cost_total"])
            recent_row["cost_state"] = cost_state(recent_row["cost_total"], recent_row["input_tokens"])
            recent_row["ts_display"] = format_ts_display(recent_row["ts"])
            recent.append(recent_row)

    recent.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    recent = recent[: max(1, recent_limit)]

    kind_rows = []
    for kind, bucket in sorted(by_kind.items(), key=lambda item: (-item[1].input_tokens, item[0])):
        row = _token_dict(bucket)
        row["agent_kind"] = kind
        row["label"] = AGENT_KIND_LABELS.get(kind, kind)
        kind_rows.append(row)

    workspace_rows = []
    for ws_key, bucket in sorted(by_workspace.items(), key=lambda item: (-item[1].input_tokens, item[0])):
        row = _token_dict(bucket)
        row["workspace_id"] = ws_key
        row["workspace_name"] = resolve_workspace_display_name(ws_key, aliases=aliases)
        workspace_rows.append(row)

    day_rows = []
    for day_key in sorted(by_day.keys(), reverse=True):
        row = _token_dict(by_day[day_key])
        row["day"] = day_key
        day_rows.append(row)

    model_rows = []
    for model_key, bucket in sorted(by_model.items(), key=lambda item: (-item[1].input_tokens, item[0])):
        row = _token_dict(bucket)
        row["model_key"] = model_key
        row["label"] = model_labels.get(model_key, model_key)
        mp = pricing_map.get(model_key)
        row["pricing"] = {
            "input": mp.input_per_m if mp else 0.0,
            "cache_hit": mp.cache_hit_per_m if mp else 0.0,
            "output": mp.output_per_m if mp else 0.0,
        }
        model_rows.append(row)

    return {
        "days": window_days,
        "workspace_filter": filter_ws,
        "note": "命中率 = 缓存命中 ÷ 总输入。含主会话与子智能体，不含工作流引擎与压缩。",
        "window": {
            "from": earliest.isoformat() if earliest else None,
            "to": latest.isoformat() if latest else None,
            "from_display": format_day_display(earliest.isoformat()) if earliest else "",
            "to_display": format_day_display(latest.isoformat()) if latest else "",
        },
        "totals": _token_dict(totals),
        "by_agent_kind": kind_rows,
        "by_model": model_rows,
        "by_workspace": workspace_rows,
        "by_day": day_rows,
        "recent": recent,
        "workspaces": [
            {"id": ws_key, "name": resolve_workspace_display_name(ws_key, aliases=aliases)}
            for ws_key, _ in sorted(by_workspace.items(), key=lambda item: item[0])
        ],
    }


def summarize_usage_detail(
    *,
    days: int = 7,
    workspace_id: str | None = None,
    coara_home: Path | None = None,
    limit: int = 500,
    event_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """最近 N 天消费明细：会话 → 大轮(turn) → 小轮(iteration)，逐层收敛。

    每个小轮含四项费用（miss / create / hit / out）与合计；无 turn_id 的历史
    记录并入 turn_id="" 的「历史记录」组，费用照常计算。
    """
    from src.runtime.usage_attribution import AGENT_KIND_LABELS, parse_agent_kind
    from src.runtime.usage_display import load_model_display_map, resolve_model_label
    from src.runtime.usage_pricing import compute_turn_cost, load_pricing_map, pricing_is_configured

    window_days = max(1, int(days))
    cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
    filter_ws = str(workspace_id or "").strip() or None
    pricing_map = load_pricing_map(coara_home)
    display_map = load_model_display_map(coara_home)

    paths = event_paths if event_paths is not None else iter_all_usage_event_paths(coara_home)
    aliases = _load_registry_workspace_aliases(coara_home)

    # 扫描时用最小堆只保留最新 limit 条，避免先全量收集再切片占内存
    cap = max(1, int(limit))
    heap: list[tuple[str, int, dict[str, Any]]] = []
    seq = 0
    for events_path in paths:
        path_ws_id = _workspace_id_from_usage_path(events_path)
        for record in iter_usage_events(events_path):
            if record.get("kind") != "llm_turn":
                continue
            kind = parse_agent_kind(record.get("agent_kind"))
            if kind is None:
                continue
            ts = _parse_ts(str(record.get("ts") or ""))
            if ts is not None and ts < cutoff:
                continue
            rec_ws_id = str(record.get("workspace_id") or "").strip() or path_ws_id
            if filter_ws and rec_ws_id != filter_ws:
                continue
            usage = record.get("usage") or {}
            provider = str(record.get("provider") or "").strip()
            model = str(record.get("model") or "").strip()
            model_key = f"{provider}/{model}" if provider and model else (model or provider)
            # 明细「是否配价」只认精确 key，避免「同 provider 仅一价」回退误标
            pricing = pricing_map.get(model_key) if pricing_map else None
            cost = compute_turn_cost(usage, pricing)
            ts_key = str(record.get("ts") or "")
            input_tokens = _prompt_tokens_from_usage(usage)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_read = _cache_read_from_usage(usage)
            row = {
                "ts": ts_key,
                "session_id": str(record.get("session_id") or ""),
                "turn_id": str(record.get("turn_id") or ""),
                "coara_name": record.get("coara_name") or "",
                "agent_kind": kind,
                "agent_label": AGENT_KIND_LABELS.get(kind, kind),
                "workspace_name": resolve_workspace_display_name(rec_ws_id, aliases=aliases),
                "model": model,
                "provider": provider,
                "model_label": resolve_model_label(provider, model, display_map),
                "iteration": record.get("iteration"),
                "pricing_configured": bool(pricing is not None and pricing_is_configured(pricing)),
                "cost_miss": round(cost["cost_miss"], 6),
                "cost_hit": round(cost["cost_hit"], 6),
                "cost_out": round(cost["cost_out"], 6),
                "cost_total": round(cost["cost_total"], 6),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_hit_rate": _hit_rate(cache_read, input_tokens),
            }
            decorate_token_totals(row)
            row["ts_display"] = format_ts_display(ts_key)
            seq += 1
            heapq.heappush(heap, (ts_key, seq, row))
            if len(heap) > cap:
                heapq.heappop(heap)

    items = [entry[2] for entry in sorted(heap, key=lambda e: (e[0], e[1]), reverse=True)]

    sessions: dict[str, dict[str, Any]] = {}
    for row in items:
        sid = row["session_id"] or "(无会话)"
        session = sessions.setdefault(
            sid,
            {
                "session_id": sid,
                "coara_name": row["coara_name"],
                "agent_label": row["agent_label"],
                "workspace_name": row["workspace_name"],
                "llm_turns": 0,
                "cost_total": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "turns": {},
            },
        )
        session["llm_turns"] += 1
        session["cost_total"] += row["cost_total"]
        session["input_tokens"] += row["input_tokens"]
        session["output_tokens"] += row["output_tokens"]
        session["cache_read_tokens"] += row["cache_read_tokens"]
        tid = row["turn_id"] or ""
        turn = session["turns"].setdefault(
            tid,
            {
                "turn_id": tid,
                "ts": row["ts"],
                "cost_total": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "iterations": [],
            },
        )
        turn["cost_total"] += row["cost_total"]
        turn["input_tokens"] += row["input_tokens"]
        turn["output_tokens"] += row["output_tokens"]
        turn["cache_read_tokens"] += row["cache_read_tokens"]
        turn["iterations"].append(row)

    session_rows: list[dict[str, Any]] = []
    for session in sessions.values():
        turns = []
        for turn in session["turns"].values():
            turn["iterations"].sort(key=lambda r: (str(r.get("ts") or ""), int(r.get("iteration") or 0)))
            turn["cost_total"] = round(turn["cost_total"], 6)
            turn["cache_hit_rate"] = _hit_rate(turn["cache_read_tokens"], turn["input_tokens"])
            decorate_token_totals(turn)
            turn["ts_display"] = format_ts_display(str(turn.get("ts") or ""))
            turns.append(turn)
        turns.sort(key=lambda t: str(t.get("ts") or ""), reverse=True)
        session["turns"] = turns
        session["cost_total"] = round(session["cost_total"], 6)
        session["cache_hit_rate"] = _hit_rate(session["cache_read_tokens"], session["input_tokens"])
        decorate_token_totals(session)
        session_rows.append(session)
    session_rows.sort(key=lambda s: float(s["cost_total"]), reverse=True)

    # 明细自带合计（仅覆盖本次返回的 limit 条），让 devtools 头部行也只渲染不算数
    total_input = sum(int(s["input_tokens"]) for s in session_rows)
    total_cache = sum(int(s["cache_read_tokens"]) for s in session_rows)
    detail_totals = decorate_token_totals(
        {
            "sessions": len(session_rows),
            "turns": sum(len(s["turns"]) for s in session_rows),
            "llm_turns": sum(int(s["llm_turns"]) for s in session_rows),
            "input_tokens": total_input,
            "output_tokens": sum(int(s["output_tokens"]) for s in session_rows),
            "cache_read_tokens": total_cache,
            "cost_total": round(sum(float(s["cost_total"]) for s in session_rows), 6),
            "cache_hit_rate": _hit_rate(total_cache, total_input),
        }
    )

    return {
        "days": window_days,
        "limit": len(items),
        "sessions": session_rows,
        "totals": detail_totals,
    }


def _parse_day_key(raw: str) -> datetime | None:
    """``YYYY-MM-DD`` → 本地当日零点（aware）；非法输入返回 None。"""
    text = raw.strip()
    if len(text) != 10:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    local_tz = datetime.now().astimezone().tzinfo or UTC
    return datetime(day.year, day.month, day.day, tzinfo=local_tz)


def summarize_usage_range(
    *,
    date_from: str,
    date_to: str,
    coara_home: Path | None = None,
    event_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """任意日区间 [date_from, date_to]（本地日、含两端）的三维交叉聚合。

    与 dashboard 的滚动 N 天窗不同，这里按明确的日区间过滤（手机端日历热图
    点选/整周/整月用），除总览外还产出三组交叉：工作空间×智能体、工作空间×模型、
    智能体×模型，由端上按主维度切换 + 行内展开呈现，无需多次请求。
    """
    from src.runtime.usage_attribution import AGENT_KIND_LABELS, parse_agent_kind
    from src.runtime.usage_display import load_model_display_map, resolve_model_label
    from src.runtime.usage_pricing import load_pricing_map

    start = _parse_day_key(date_from)
    end_day = _parse_day_key(date_to)
    if start is None or end_day is None or end_day < start:
        return {
            "error": "invalid_range",
            "from": date_from,
            "to": date_to,
            "totals": _token_dict(_TokenBucket()),
            "by_day": [],
            "by_workspace": [],
            "by_agent_kind": [],
            "by_model": [],
            "workspace_agents": {},
            "workspace_models": {},
            "agent_models": {},
            "workspaces": [],
        }
    cutoff_start = start.astimezone(UTC)
    cutoff_end = (end_day + timedelta(days=1)).astimezone(UTC)
    pricing_map = load_pricing_map(coara_home)
    display_map = load_model_display_map(coara_home)

    totals = _TokenBucket()
    by_kind: dict[str, _TokenBucket] = {}
    by_workspace: dict[str, _TokenBucket] = {}
    by_day: dict[str, _TokenBucket] = {}
    by_model: dict[str, _TokenBucket] = {}
    model_labels: dict[str, str] = {}
    ws_agents: dict[str, dict[str, _TokenBucket]] = {}
    ws_models: dict[str, dict[str, _TokenBucket]] = {}
    agent_models: dict[str, dict[str, _TokenBucket]] = {}

    paths = event_paths if event_paths is not None else iter_all_usage_event_paths(coara_home)
    aliases = _load_registry_workspace_aliases(coara_home)

    for events_path in paths:
        path_ws_id = _workspace_id_from_usage_path(events_path)
        for record in iter_usage_events(events_path):
            if record.get("kind") != "llm_turn":
                continue
            kind = parse_agent_kind(record.get("agent_kind"))
            if kind is None:
                continue
            ts = _parse_ts(str(record.get("ts") or ""))
            if ts is None:
                # 无时间戳的历史记录无法落入日区间，跳过（dashboard 里它们计入滚动窗）
                continue
            if ts < cutoff_start or ts >= cutoff_end:
                continue

            rec_ws_id = str(record.get("workspace_id") or "").strip() or path_ws_id or "(unknown)"
            provider = str(record.get("provider") or "").strip()
            model = str(record.get("model") or "").strip()
            if model or provider:
                # 分桶键 = 事件原文；label 仅展示
                model_key = f"{provider}/{model}" if provider and model else (model or provider)
                model_label = resolve_model_label(provider, model, display_map)
            else:
                model_key = "(unknown)"
                model_label = "（未知模型）"
            model_labels.setdefault(model_key, model_label)

            _add_llm_turn(totals, record, pricing_map)
            _add_llm_turn(by_kind.setdefault(kind, _TokenBucket()), record, pricing_map)
            _add_llm_turn(by_workspace.setdefault(rec_ws_id, _TokenBucket()), record, pricing_map)
            _add_llm_turn(by_model.setdefault(model_key, _TokenBucket()), record, pricing_map)
            _add_llm_turn(
                by_day.setdefault(ts.astimezone().date().isoformat(), _TokenBucket()),
                record,
                pricing_map,
            )
            _add_llm_turn(ws_agents.setdefault(rec_ws_id, {}).setdefault(kind, _TokenBucket()), record, pricing_map)
            _add_llm_turn(
                ws_models.setdefault(rec_ws_id, {}).setdefault(model_key, _TokenBucket()),
                record,
                pricing_map,
            )
            _add_llm_turn(
                agent_models.setdefault(kind, {}).setdefault(model_key, _TokenBucket()),
                record,
                pricing_map,
            )

    def _rows(buckets: dict[str, _TokenBucket], label_of) -> list[dict[str, Any]]:
        rows = []
        for key, bucket in sorted(buckets.items(), key=lambda item: (-item[1].input_tokens, item[0])):
            row = _token_dict(bucket)
            row.update(label_of(key))
            rows.append(row)
        return rows

    def _nested(nested: dict[str, dict[str, _TokenBucket]], label_of) -> dict[str, list[dict[str, Any]]]:
        return {outer: _rows(inner, label_of) for outer, inner in nested.items()}

    return {
        "from": date_from,
        "to": date_to,
        "note": "命中率 = 缓存命中 ÷ 总输入。含主会话与子智能体，不含工作流引擎与压缩。",
        "totals": _token_dict(totals),
        "by_day": _rows(by_day, lambda key: {"day": key}),
        "by_workspace": _rows(
            by_workspace,
            lambda key: {
                "workspace_id": key,
                "workspace_name": resolve_workspace_display_name(key, aliases=aliases),
            },
        ),
        "by_agent_kind": _rows(
            by_kind,
            lambda key: {"agent_kind": key, "label": AGENT_KIND_LABELS.get(key, key)},
        ),
        "by_model": _rows(by_model, lambda key: {"model_key": key, "label": model_labels.get(key, key)}),
        "workspace_agents": _nested(
            ws_agents,
            lambda key: {"agent_kind": key, "label": AGENT_KIND_LABELS.get(key, key)},
        ),
        "workspace_models": _nested(
            ws_models,
            lambda key: {"model_key": key, "label": model_labels.get(key, key)},
        ),
        "agent_models": _nested(agent_models, lambda key: {"model_key": key, "label": model_labels.get(key, key)}),
        "workspaces": [
            {"id": ws_key, "name": resolve_workspace_display_name(ws_key, aliases=aliases)}
            for ws_key in sorted(by_workspace.keys())
        ],
    }


__all__ = [
    "SessionUsageSummary",
    "UsageSummary",
    "format_session_usage_lines",
    "format_summary_text",
    "iter_all_usage_event_paths",
    "iter_usage_events",
    "parse_usage_chat_args",
    "resolve_default_usage_path",
    "resolve_usage_path_for_root",
    "resolve_workspace_display_name",
    "session_cache_hit_ratio",
    "summarize_session",
    "summarize_session_stats",
    "summarize_session_token_cost",
    "enrich_usage_with_cost",
    "enrich_llm_log_detail",
    "summarize_usage",
    "summarize_usage_dashboard",
    "summarize_usage_detail",
    "summarize_usage_range",
]
