"""Daily agent: random-morning dispatch + immediate digest delivery.

State file: ``<agent_dir>/curator_state.json``
Digests: ``<agent_dir>/digests/YYYY-MM-DD.md`` (keyed by dispatch day)

Each day a random instant in the morning window [06:00, 09:00] local time is
persisted as ``next_run_at``. When the tick reaches it, curation runs at once,
covering the continuous range from the previous run's end (``last_run_at``) to
now — nothing between midnight and the random slot is lost. The finished
digest is delivered immediately; missed windows (late start / downtime) run on
the next tick.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.core.logger import logger
from src.workspace.types import ViewCapability

DAILY_WORKSPACE_NAME = "daily"
# 记录空间与 daily 合并（docs/空间模型与内容注册表.md §2 persona 首个真实用例）：
# 条目展示名=记录（对话主体 persona=daily）；path 保 .internal/daily 不换
# （registry 主键是 path，换 path 断链录像带/会话档案）
DAILY_WORKSPACE_DISPLAY_NAME = "记录"
_CURATOR_STATE_NAME = "curator_state.json"
_DIGESTS_DIRNAME = "digests"

# Random run window (local curator timezone), inclusive of both ends.
_RUN_HOUR_START = 6
_RUN_HOUR_END = 9

# Single-flight: one in-process daily run at a time.
_curator_flight: dict[str, Any] = {"task_id": None, "started_at": 0.0}

# Self-healing knobs: a flight whose completion event was lost is force-cleared
# after this many seconds; a failed run may retry after the backoff.
_FLIGHT_STALE_SECONDS = 1800.0
_FAILURE_BACKOFF_SECONDS = 3600.0

# Persisted inflight marker TTL: a process dying mid-run would otherwise pin
# the date forever (the completion event that clears it never arrives).
_INFLIGHT_STALE_SECONDS = 7200.0


def reset_curator_flight_for_tests() -> None:
    _curator_flight["task_id"] = None
    _curator_flight["started_at"] = 0.0


def _active_flight_id() -> str | None:
    """In-flight task id, or None; stale flights (lost completion event) self-heal."""
    task_id = _curator_flight.get("task_id")
    if not task_id:
        return None
    started = float(_curator_flight.get("started_at") or 0.0)
    if started and time.monotonic() - started > _FLIGHT_STALE_SECONDS:
        logger.warning(f"daily flight stale (>{_FLIGHT_STALE_SECONDS:.0f}s), clearing: {task_id}")
        _curator_flight["task_id"] = None
        _curator_flight["started_at"] = 0.0
        return None
    return str(task_id)


def _records_cfg() -> Any:
    try:
        from src.core.config import config_manager

        cfg = config_manager.config
        return getattr(cfg, "records", None) if cfg else None
    except Exception:
        return None


def curator_enabled() -> bool:
    mem = _records_cfg()
    if mem is None:
        return False
    if not getattr(mem, "enabled", False):
        return False
    return bool(getattr(mem, "daily_curator_enabled", True))


def curator_timezone() -> ZoneInfo:
    mem = _records_cfg()
    name = "Asia/Shanghai"
    if mem is not None:
        name = str(getattr(mem, "curator_timezone", None) or name).strip() or name
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def local_now(*, tz: ZoneInfo | None = None) -> datetime:
    zone = tz or curator_timezone()
    return datetime.now(zone)


def agent_dir_from_root(root: Any) -> Path | None:
    """Return agent records root (digests / curator_state live here)."""
    store = getattr(root, "records_store", None)
    if store is not None:
        agent_root = getattr(store, "agent_root", None)
        if agent_root is not None:
            return Path(agent_root)
        agent = getattr(store, "agent", None)
        if agent is not None:
            return Path(agent.root)
    return None


def curator_state_path(agent_dir: Path) -> Path:
    return Path(agent_dir) / _CURATOR_STATE_NAME


def digests_dir(agent_dir: Path) -> Path:
    return Path(agent_dir) / _DIGESTS_DIRNAME


def digest_path(agent_dir: Path, day: date | str) -> Path:
    key = day.isoformat() if isinstance(day, date) else str(day)
    return digests_dir(agent_dir) / f"{key}.md"


def load_curator_state(agent_dir: Path) -> dict[str, Any]:
    path = curator_state_path(agent_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"memory curator state load failed: {exc}")
        return {}


def save_curator_state(agent_dir: Path, state: dict[str, Any]) -> None:
    path = curator_state_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_dt(raw: Any, *, tz: ZoneInfo) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _fmt_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def compute_next_run_at(
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
    rng: random.Random | None = None,
    day: date | None = None,
) -> datetime:
    """Pick a random run instant in the morning window of *day* (default: today).

    Window is the local calendar morning ``[06:00, 09:00]``. If *now* is
    already past today's window, return *now* (catch-up run ASAP).
    """
    zone = tz or curator_timezone()
    current = now.astimezone(zone) if now is not None else local_now(tz=zone)
    picker = rng or random.Random()
    target = day or current.date()
    start = datetime.combine(target, dt_time(_RUN_HOUR_START, 0), tzinfo=zone)
    end = datetime.combine(target, dt_time(_RUN_HOUR_END, 0), tzinfo=zone)

    if target != current.date():
        low = start
    elif current >= end:
        return current
    else:
        # Earliest eligible instant is max(now, window start).
        low = start if current < start else current
    span = int((end - low).total_seconds())
    if span <= 0:
        return low
    return low + timedelta(seconds=picker.randint(0, span))


def _coverage_start(state: dict[str, Any], *, current: datetime, zone: ZoneInfo) -> datetime:
    """Lower bound of the next curation range (continuous coverage).

    ``last_run_at`` marks where the previous run's coverage ended. Legacy
    installs only have ``last_run_date`` (midnight semantics: coverage ended at
    the following midnight) — used once as a migration fallback. Fresh installs
    start from today's midnight: nothing exists before it anyway.
    """
    parsed = _parse_dt(state.get("last_run_at"), tz=zone)
    if parsed is not None:
        return parsed
    legacy = str(state.get("last_run_date") or "").strip()
    if legacy:
        try:
            day = date.fromisoformat(legacy)
            return datetime.combine(day + timedelta(days=1), dt_time(0, 0), tzinfo=zone)
        except ValueError:
            pass
    return datetime.combine(current.date(), dt_time(0, 0), tzinfo=zone)


def _workspace_names_for_prompt(root: Any | None) -> list[str]:
    if root is None:
        return []
    manager = getattr(root, "workspace_manager", None)
    if manager is None:
        return []
    try:
        entries = list(manager.list_workspaces())
    except Exception:
        return []
    names: list[str] = []
    for entry in entries:
        name = str(getattr(entry, "name", "") or "").strip()
        if name:
            names.append(name)
    return names


def _sample_reference_phrases(rng: random.Random | None = None) -> list[str]:
    """从内载轮播词库随机抽样供 daily 写定制轮播语时参考味道。"""
    try:
        from src.records.loading_phrases import QUOTE_POOL, WITTY_POOL

        r = rng or random.Random()
        return r.sample(list(WITTY_POOL), 4) + r.sample(list(QUOTE_POOL), 2)
    except Exception:
        return []


def build_curator_prompt(
    *,
    for_date: date,
    range_start: datetime,
    range_end: datetime,
    agent_dir: Path,
    digest_file: Path,
    workspace_names: list[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    span = f"{_fmt_dt(range_start)} 至 {_fmt_dt(range_end)}"
    # 信封只带动态事实；职责规则唯一源在 daily.md（对齐 janitor 派发模式）
    names = [n for n in (workspace_names or []) if n]
    lines = [
        f"<系统提醒>整理时段：{span}",
        f"日报日期：{for_date.isoformat()}",
        f"记录根目录（agent）：{agent_dir}",
        f"Digest 目标文件：{digest_file}",
        f"已登记工作空间：{', '.join(names)}" if names else "已登记工作空间：未知",
    ]
    refs = _sample_reference_phrases(rng)
    if refs:
        lines.append("轮播语参考词（从词库随机抽样 感受味道 不要照抄）：")
        lines.extend(f"- {p}" for p in refs)
    return "\n".join(lines)


def daily_workspace_dir(root: Any) -> Path | None:
    """daily internal 空间的工作目录：``<coara_home>/workspaces/.internal/daily``。

    落点选择理由：record 的 provenance 取 ``parent.workspace_dir`` 作为条目归属
    工作空间标签——daily 面向全部工作空间，锚到任何真实空间都会污染归属；
    也绝不能锚到某个用户空间目录（其 VFS 会吃掉该空间的工具绑定、rules、skills）。
    因此给 daily 一个独立的系统目录：放 coara_home 的 workspaces/ 下（与录像带/
    session_state 的物理落点同族，``.internal`` 前缀从视觉上与用户空间 id 区分），
    而不是 agent_dir 下（records/agent 是记忆数据面，不是工作目录）。
    """
    wm = getattr(root, "workspace_manager", None)
    home = getattr(wm, "coara_home", None) if wm is not None else None
    if home is None:
        return None
    return Path(home) / "workspaces" / ".internal" / DAILY_WORKSPACE_NAME


def ensure_daily_workspace_entry(root: Any) -> Any | None:
    """惰性确保记录空间以 kind=internal 登记在册（registry 单一事实源）。

    记录空间 = 记录时间线主页 + 与 daily 对话：条目名=记录，persona=daily。
    provider/model 由 ``_daily_llm_params()`` 解析（records.daily_provider/model →
    全局默认），落进条目字段——WorkspaceSession 创建时经 entry_llm_override 读取。
    幂等：旧条目（name=daily）原地改名补身份，不动 id/path。
    """
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return None
    path = daily_workspace_dir(root)
    if path is None:
        return None
    # 只建工作目录，不碰 coara_home 结构（隔离测试环境允许 home 不存在）
    path.mkdir(parents=True, exist_ok=True)
    provider, model = _daily_llm_params()
    entry = wm.registry.ensure_internal_workspace(
        path,
        name=DAILY_WORKSPACE_DISPLAY_NAME,
        view=ViewCapability.ALL,
        provider=provider,
        model=model,
        summary="记录（系统展示空间：记录时间线主页，与 daily 对话）",
        content_type="records",
        storefront="display",
        home_view="/records",
        persona=DAILY_WORKSPACE_NAME,
    )
    return entry


async def ensure_daily_workspace_session(root: Any) -> Any | None:
    """确保 daily 空间的 WorkspaceSession（含专属 persona/工具面/事件身份）。"""
    entry = ensure_daily_workspace_entry(root)
    if entry is None:
        return None
    ensure = getattr(root, "ensure_workspace_session", None)
    if ensure is None:
        return None
    try:
        return await ensure(entry)
    except Exception as exc:
        logger.error(f"daily workspace session ensure failed: {exc}")
        return None


def mark_pending_digest(agent_dir: Path, for_date: date) -> None:
    state = load_curator_state(agent_dir)
    state["pending_digest"] = for_date.isoformat()
    state.pop("inflight_date", None)
    state.pop("inflight_since", None)
    # Coverage only advances on success: a failed run must not skip content.
    zone = curator_timezone()
    range_end = _parse_dt(state.get("inflight_range_end"), tz=zone)
    if range_end is not None:
        state["last_run_at"] = range_end.isoformat()
    state.pop("inflight_range_start", None)
    state.pop("inflight_range_end", None)
    delivered = state.get("delivered_digest")
    if delivered == for_date.isoformat():
        state.pop("delivered_digest", None)
    # Legacy field from the delivery-window era: delivery is immediate now.
    state.pop("delivery_at", None)
    save_curator_state(agent_dir, state)


def mark_curator_inflight(
    agent_dir: Path,
    for_date: date,
    *,
    range_start: datetime | None = None,
    range_end: datetime | None = None,
) -> None:
    state = load_curator_state(agent_dir)
    state["inflight_date"] = for_date.isoformat()
    state["inflight_since"] = time.time()
    if range_start is not None:
        state["inflight_range_start"] = range_start.isoformat()
    if range_end is not None:
        state["inflight_range_end"] = range_end.isoformat()
    save_curator_state(agent_dir, state)


def _inflight_is_stale(state: dict[str, Any], *, now: float | None = None) -> bool:
    """True when the persisted inflight marker is older than the death TTL.

    Markers written before the timestamp existed count as stale: the process
    that wrote them is gone (otherwise it would have rewritten the marker).
    """
    try:
        since = float(state.get("inflight_since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    if since <= 0.0:
        return True
    return (now if now is not None else time.time()) - since > _INFLIGHT_STALE_SECONDS


def clear_stale_inflight(agent_dir: Path, *, now: float | None = None) -> bool:
    """Drop a dead inflight marker so the run becomes dispatchable again."""
    state = load_curator_state(agent_dir)
    inflight = str(state.get("inflight_date") or "").strip()
    if not inflight or not _inflight_is_stale(state, now=now):
        return False
    logger.warning(f"daily inflight stale (>{_INFLIGHT_STALE_SECONDS:.0f}s), clearing: {inflight}")
    state.pop("inflight_date", None)
    state.pop("inflight_since", None)
    state.pop("inflight_range_start", None)
    state.pop("inflight_range_end", None)
    save_curator_state(agent_dir, state)
    return True


def promote_inflight_if_digest_ready(agent_dir: Path) -> bool:
    """When digest file appears for inflight date, mark pending for delivery."""
    state = load_curator_state(agent_dir)
    inflight = str(state.get("inflight_date") or "").strip()
    if not inflight:
        return False
    path = digest_path(agent_dir, inflight)
    if not path.is_file():
        return False
    try:
        day = date.fromisoformat(inflight)
    except ValueError:
        state.pop("inflight_date", None)
        state.pop("inflight_since", None)
        save_curator_state(agent_dir, state)
        return False
    mark_pending_digest(agent_dir, day)
    return True


def note_daily_failure(agent_dir: Path, for_date: date) -> None:
    """Record a failed run: clear inflight so the slot is not lost forever.

    Retries are throttled by ``_FAILURE_BACKOFF_SECONDS`` in
    ``curator_run_due``.
    """
    state = load_curator_state(agent_dir)
    state.pop("inflight_date", None)
    state.pop("inflight_since", None)
    state.pop("inflight_range_start", None)
    state.pop("inflight_range_end", None)
    state["last_failure"] = {"date": for_date.isoformat(), "ts": time.time()}
    save_curator_state(agent_dir, state)


def handle_daily_completion(agent_dir: Path, for_date: date) -> str:
    """Settle state after the daily task ends: promote on success, record failure."""
    if promote_inflight_if_digest_ready(agent_dir):
        return "promoted"
    state = load_curator_state(agent_dir)
    if str(state.get("inflight_date") or "").strip() == for_date.isoformat():
        logger.warning(f"daily run for {for_date.isoformat()} ended without digest")
        note_daily_failure(agent_dir, for_date)
        return "failed"
    return "none"


def curator_run_due(
    *,
    agent_dir: Path,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> bool:
    """True when the persisted random run slot has arrived (or was missed).

    One run per day: a digest keyed by today means today's slot is done and
    ``next_run_at`` is rolled into tomorrow's window. Failure retries are
    throttled by the backoff.
    """
    zone = curator_timezone()
    current = now.astimezone(zone) if now is not None else local_now(tz=zone)
    # A process dying mid-run pins inflight_date forever; the TTL clears the
    # corpse so the slot is dispatched again (tick + startup both land here).
    cleared_inflight = clear_stale_inflight(agent_dir)
    state = load_curator_state(agent_dir)
    if str(state.get("inflight_date") or "").strip():
        return False
    if _active_flight_id():
        return False

    today = current.date()
    if digest_path(agent_dir, today).is_file():
        # Today already ran: make sure the next slot sits in tomorrow's window.
        next_run = _parse_dt(state.get("next_run_at"), tz=zone)
        if next_run is None or next_run.date() <= today:
            chosen = compute_next_run_at(now=current, tz=zone, rng=rng, day=today + timedelta(days=1))
            state["next_run_at"] = chosen.isoformat()
            save_curator_state(agent_dir, state)
        return False

    failure = state.get("last_failure")
    if isinstance(failure, dict):
        try:
            ts = float(failure.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and time.time() - ts < _FAILURE_BACKOFF_SECONDS:
            return False

    next_run = _parse_dt(state.get("next_run_at"), tz=zone)
    if next_run is None or next_run.date() < today:
        # Fresh install = 无任何历史痕迹且无死 inflight：首日绝不跑（新装用户
        # 没有可整理内容，空日报只烧 token），slot 直接排到明天窗口。
        if next_run is None and not state and not cleared_inflight:
            chosen = compute_next_run_at(
                now=current, tz=zone, rng=rng, day=today + timedelta(days=1)
            )
        else:
            # 有历史（失败退避/覆盖推进）或死 inflight 刚清：今天仍要补跑——
            # 选今天的随机槽位（窗口已过则坍缩为 now，下一 tick 立即派发）。
            chosen = compute_next_run_at(now=current, tz=zone, rng=rng)
        state["next_run_at"] = chosen.isoformat()
        save_curator_state(agent_dir, state)
        next_run = chosen
    return current >= next_run


def _daily_llm_params() -> tuple[str | None, str | None]:
    """Resolve daily's (provider, model): 专属配置 → 全局默认（llm_preferences）。

    daily 面向全部工作空间，绝不跟随前台工作空间绑定（即 delegate 的继承链）。
    返回 (None, None) 表示解析不出有效 provider（未初始化等），调用方退化为继承。
    """
    try:
        from src.core.config import config_manager
        from src.llm.registry import provider_registry

        mem = _records_cfg()
        provider = str(getattr(mem, "daily_provider", None) or "").strip() or None
        model = str(getattr(mem, "daily_model", None) or "").strip() or None
        if provider is not None:
            if provider_registry.has(provider):
                if model is None:
                    model = (provider_registry.get(provider).default_model or "").strip() or None
                return provider, model
            logger.warning(f"records.daily_provider '{provider}' 未注册，回退全局默认 provider")
        cfg = config_manager.config
        if cfg is None:
            return None, None
        provider = str(getattr(cfg, "default_provider", "") or "").strip() or None
        model = str(getattr(cfg, "default_model", "") or "").strip() or None
        if provider is not None and not provider_registry.has(provider):
            return None, None
        return provider, model
    except Exception:
        return None, None


async def dispatch_daily(
    root: Any,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """在 daily internal 空间的会话上跑一个整理回合（覆盖区间截至 *now*）。

    daily 不再是 delegate 派发的子智能体：它是 kind=internal 的系统工作空间，
    有自己的会话/上下文/录像带。本函数确保该空间会话存在，在其 coara 上直接
    跑 process_message（source=background）；单飞用流程自管的 asyncio.Task，
    完成钩子（清 flight + handle_daily_completion）挂在 task done callback。
    """
    if not curator_enabled():
        return None
    agent_dir = agent_dir_from_root(root)
    if agent_dir is None:
        return None

    # 无有效 provider（未配置 key / 默认模型）绝不 dispatch：daily 是后台
    # LLM 回合，新用户装完没填 key 就触发会烧 token 或反复失败。
    provider, _model = _daily_llm_params()
    if provider is None:
        logger.info("daily dispatch skipped: no usable provider configured")
        return None

    zone = curator_timezone()
    current = now.astimezone(zone) if now is not None else local_now(tz=zone)
    target = current.date()

    flight_id = _active_flight_id()
    if flight_id:
        logger.info(f"daily coalesce: already running {flight_id}")
        return flight_id

    session = await ensure_daily_workspace_session(root)
    if session is None:
        logger.error("daily dispatch failed: internal workspace session unavailable")
        return None
    coara = getattr(session, "coara", None)
    if coara is None:
        logger.error("daily dispatch failed: workspace session has no coara")
        return None

    state = load_curator_state(agent_dir)
    range_start = _coverage_start(state, current=current, zone=zone)
    range_end = current

    digests_dir(agent_dir).mkdir(parents=True, exist_ok=True)
    out = digest_path(agent_dir, target)
    prompt = build_curator_prompt(
        for_date=target,
        range_start=range_start,
        range_end=range_end,
        agent_dir=agent_dir,
        digest_file=out,
        workspace_names=_workspace_names_for_prompt(root),
        rng=rng,
    )

    task_id = f"daily-{target.isoformat()}-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        stream = coara.process_message(
            prompt,
            trust_level="owner",
            show_tool_summary=False,
            source="background",
            turn_id=task_id,
        )
        async for _ in stream:
            pass

    try:
        task = asyncio.create_task(_run(), name=task_id)
    except Exception as exc:
        logger.error(f"daily dispatch failed: {exc}")
        return None

    _curator_flight["task_id"] = task_id
    _curator_flight["started_at"] = time.monotonic()
    mark_curator_inflight(agent_dir, target, range_start=range_start, range_end=range_end)

    def _on_done(done_task: asyncio.Task) -> None:
        if _curator_flight.get("task_id") == task_id:
            _curator_flight["task_id"] = None
            _curator_flight["started_at"] = 0.0
        if done_task.cancelled():
            logger.warning(f"daily run cancelled: {task_id}")
        else:
            exc = done_task.exception()
            if exc is not None:
                logger.warning(f"daily run failed: {exc}")
        try:
            handle_daily_completion(agent_dir, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"daily completion settle failed: {exc}")

    task.add_done_callback(_on_done)
    # 防 GC 提前回收协程（与 Root._bg_tasks 同理）
    bg_tasks = getattr(root, "_bg_tasks", None)
    if isinstance(bg_tasks, set):
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    logger.info(f"daily dispatched for {target.isoformat()} task_id={task_id}")
    return task_id


async def maybe_dispatch_curator(root: Any) -> str | None:
    """If the random run slot has arrived, dispatch daily once."""
    if not curator_enabled():
        return None
    agent_dir = agent_dir_from_root(root)
    if agent_dir is None:
        return None
    if not curator_run_due(agent_dir=agent_dir):
        return None
    return await dispatch_daily(root)


def _read_digest(path: Path) -> str:
    """Read the digest file in full — delivery is never truncated."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def maybe_deliver_pending_digest(root: Any) -> bool:
    """Deliver pending digest once, immediately (CLI + Matrix display only; never enters LLM context).

    设计上日报不进任何会话历史：跨空间内容注入单空间上下文会干扰工作；
    LLM 需要时可经 local_search 检索（digest 已镜像进记录流）。
    """
    if not curator_enabled():
        return False
    agent_dir = agent_dir_from_root(root)
    if agent_dir is None:
        return False

    state = load_curator_state(agent_dir)
    pending = str(state.get("pending_digest") or "").strip()
    if not pending:
        return False
    if str(state.get("delivered_digest") or "").strip() == pending:
        return False

    path = digest_path(agent_dir, pending)
    if not path.is_file():
        # Curator still running or failed — wait.
        return False

    body = _read_digest(path)
    if not body:
        return False

    # Digest Markdown 已自带标题（如 ``# 日报 · …``）；投递不要再加前缀，
    # 否则手机端 ``☰ # …`` 会破坏 AT1 渲染。
    notice = body

    # 前端 scrollback 镜像（best-effort；daemon 无头/无前端注册时为 no-op）。
    try:
        from src.coara.frontend import get_frontend

        fe = get_frontend()
        fe.scrollback_write("")
        fe.scrollback_write(notice)
        fe.scrollback_write("")
    except Exception:
        pass

    # Matrix
    try:
        bridge = getattr(root, "matrix_notify", None)
        if bridge is not None and hasattr(bridge, "send_to_user"):
            await bridge.send_to_user(notice)
    except Exception as exc:
        logger.warning(f"memory digest Matrix notify failed: {exc}")

    state["delivered_digest"] = pending
    # Keep pending until delivered so restart can retry; clear after success.
    state.pop("pending_digest", None)
    state.pop("delivery_at", None)
    save_curator_state(agent_dir, state)
    # 日报投递成功：daily 定制的 Thinking 轮播语（若有）即刻生效
    try:
        from src.records import loading_phrases

        loading_phrases.set_custom_phrases_path(agent_dir / loading_phrases.CUSTOM_PHRASES_FILENAME)
        loading_phrases.reshuffle()
    except Exception:
        pass
    logger.info(f"memory digest delivered for {pending}")
    return True


async def curator_tick(root: Any) -> None:
    """Idle-watcher hook: dispatch if due, then try deliver."""
    try:
        await maybe_dispatch_curator(root)
    except Exception as exc:
        logger.warning(f"daily dispatch tick failed: {exc}")
    try:
        agent_dir = agent_dir_from_root(root)
        if agent_dir is not None:
            promote_inflight_if_digest_ready(agent_dir)
    except Exception as exc:
        logger.warning(f"daily promote tick failed: {exc}")
    try:
        await maybe_deliver_pending_digest(root)
    except Exception as exc:
        logger.warning(f"memory digest deliver tick failed: {exc}")
