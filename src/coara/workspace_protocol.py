"""工作空间概况（ws.md）系统内部维护触发。

janitor 是主会话的**系统子智能体**（走 delegate + background），但不对 Root LLM
暴露：仅 idle / 手动 /new / 启动补扫派发。派发永远用目标空间自己的会话 spawn
（父会话即目标空间会话），会话历史经继承进入 janitor 上下文，无需额外快照。

本模块负责：
- 构造任务信封（只带概况文件路径；职责规则唯一源在 janitor.md）
- 每工作空间单飞：已有在飞任务时标 dirty，结束后再跑一轮
- 冷却防抖：两次派发间隔低于 ``session.janitor_min_interval_seconds`` 时只挂一个
  延迟定时器（不丢弃），到点跑一次
- janitor 完成后把新概况替换进存活会话的环境种子（仅概况段，前缀其余部分不动）
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
from pathlib import Path
from typing import Any

from src.core.coara_home import workspace_id_for
from src.core.json_store import write_json_atomic
from src.core.logger import logger

# 概况替换：只在会话还年轻时做（替换使分叉点之后的 turn 重新过一遍），
# 且在 turn 边界外等待，最长等这么久。
OVERVIEW_REFRESH_MAX_MESSAGES = 40
_OVERVIEW_REFRESH_RETRY_S = 5.0
_OVERVIEW_REFRESH_MAX_WAIT_S = 180.0

# Per-process single-flight registry: workspace_id -> flight state.
# Kept here so /new and idle share one lock without racing through Root fields only.
_janitor_flights: dict[str, dict[str, Any]] = {}
# 冷却：workspace_id -> 最近一次真实派发（monotonic）。
_janitor_last_dispatch: dict[str, float] = {}
# 冷却期内的延迟派发：workspace_id -> {handle, payload, root}。同空间只挂一个。
_janitor_deferred: dict[str, dict[str, Any]] = {}
# 失败退避：workspace_id -> (activity_epoch, 失败时刻 monotonic)。仅进程内，
# 重启后由启动补扫兜底，无需持久化。
_janitor_failures: dict[str, tuple[float, float]] = {}
# 已消耗补派名额的 (workspace_id, epoch)：每 epoch 进程内最多补派一次，防失败循环。
_janitor_retried: set[tuple[str, float]] = set()
# 概况替换任务去重：workspace_id 集合。
_overview_refresh_pending: set[str] = set()

# 失败后允许同 epoch 进程内补派一次的退避窗口
JANITOR_RETRY_BACKOFF_SECONDS = 600.0


def session_history_path(workspace_dir: str, coara_home: str) -> str:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/session_events.jsonl``（事实源）。

    历史名保留旧函数签名（janitor prompt 等消费方仍叫「主会话快照」路径）；
    指向已改为事件日志。
    """
    workspace_id = workspace_id_for(workspace_dir)
    return str(Path(coara_home) / "workspaces" / workspace_id / "session_events.jsonl")


JANITOR_ACTIVITY_FILE = "janitor_activity.json"


def janitor_activity_path(workspace_dir: str, coara_home: str) -> Path:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/janitor_activity.json``."""
    workspace_id = workspace_id_for(workspace_dir)
    return Path(coara_home) / "workspaces" / workspace_id / JANITOR_ACTIVITY_FILE


def load_janitor_activity(workspace_dir: str, coara_home: str) -> float | None:
    """已维护的 activity epoch（跨重启去重；无记录或损坏返回 None）。"""
    path = janitor_activity_path(workspace_dir, coara_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = float(data.get("activity_at") or 0)
        return value if value > 0 else None
    except Exception:
        return None


def save_janitor_activity(workspace_dir: str, coara_home: str, activity_at: float) -> None:
    """持久化已维护的 activity epoch：一个会话一生只维护一次，重启不再补扫同一会话。

    只在 janitor 运行完成时由完成钩子写入（派发时不写）：进程在维护中途被杀时
    磁盘上没有标记，重启 catch-up 会重新派发该 epoch，维护不再永久丢失。
    """
    path = janitor_activity_path(workspace_dir, coara_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"activity_at": activity_at})
    except Exception as exc:
        logger.warning(f"janitor activity persist failed: {exc}")


def has_real_conversation_in_history(history_path: str, *, session_id: str | None = None) -> bool:
    """True when history contains real chat beyond env seeds.

    ``history_path`` 可为两种形态（事件溯源改造兼容）：
    - 旧格式 JSON（janitor 快照）：读 messages 判定（单会话快照，无需过滤）
    - ``session_events.jsonl``：读事件流按消息事件判定（工具/助手实质内容）。
      jsonl 跨会话共存，必须传 ``session_id`` 只看目标会话——否则 /new 后
      旧会话事件会污染新会话判定（旧快照线单会话文件无此问题）
    """
    import json

    p = Path(history_path)
    try:
        if p.suffix == ".jsonl":
            if not session_id:
                # jsonl 跨会话共存：不过滤会把旧会话事件算进当前会话（脚枪），
                # 误触发 janitor catch-up。生产调用方必须传 session_id。
                raise ValueError("has_real_conversation_in_history(jsonl) requires session_id")
            from src.coara.injections.context_modules import is_context_module_seed
            from src.session_log.store import read_events

            data_events = read_events(p, session_id=session_id)
            if not data_events:
                return False

            for event in data_events:
                kind = str(event.get("kind") or "")
                payload = event.get("payload") or {}
                if kind == "tool/result":
                    return True
                if kind == "assistant/message":
                    if payload.get("tool_calls") or str(payload.get("content") or "").strip():
                        return True
                elif kind == "user/message":
                    text = str(payload.get("content") or "")
                    if is_context_module_seed(text) or text.startswith("<系统"):
                        continue
                    if text.strip():
                        return True
            return False

        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False

    from src.coara.injections.context_modules import is_context_module_seed

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").lower()
        content = m.get("content") or ""
        if role == "assistant":
            if m.get("tool_calls") or str(content).strip():
                return True
        elif role == "tool" or role == "tool_result":
            return True
        elif role == "user":
            text = str(content)
            if is_context_module_seed(text) or text.startswith("<系统"):
                continue
            if text.strip():
                return True
    return False


def ws_protocol_path(workspace_dir: str) -> str:
    """Resolve ``<workspace_dir>/.coara/ws.md`` (workspace overview carrier)."""
    return str(Path(workspace_dir) / ".coara" / "ws.md")


def _janitor_duty_body() -> str:
    """janitor.md 正文（注入词）；缺失则空串。"""
    md_path = Path(__file__).resolve().parent / "prompts" / "injections" / "janitor.md"
    try:
        return md_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_janitor_prompt(*, workspace_dir: str, kind: str = "") -> str:
    """本轮维护的一条 <系统提醒>：动态参数 + janitor.md，不再另注一次。

    动态参数：概况文件路径（产出目标）+ 空间性质（用户声明的 kind；空串表示未声明）。
    会话历史已由维护管道载入；职责规则随本信封一次带齐。
    """
    lines = [f"<系统提醒>概况文件：{ws_protocol_path(workspace_dir)}"]
    if kind:
        lines.append(f"空间性质：{kind}")
    duty = _janitor_duty_body()
    if duty:
        lines.append("")
        lines.append(duty)
    lines.append("</系统提醒>")
    return "\n".join(lines)


def _workspace_kind(root: Any, workspace_dir: str) -> str:
    """Read the workspace's declared kind (empty when undeclared)."""
    try:
        wm = getattr(root, "workspace_manager", None)
        if wm is None:
            return ""
        entry = wm.registry.get_by_id(workspace_id_for(workspace_dir))
        if entry is None:
            return ""
        return str(getattr(entry.kind, "value", "") or "")
    except Exception:  # noqa: BLE001
        return ""


async def _delegate_tool_for_workspace(root: Any, workspace_dir: str):
    """取目标空间自己会话的 delegate 工具；会话未加载则按需创建。

    janitor 永远在目标空间的会话上派发：父会话即目标空间会话，历史继承、
    静态提示词、LLM 记录归属全部自然落在目标空间——不借前台会话（借道会
    把前台空间的历史与环境带进目标空间，实测串味：nx 维护读到 v8 的对话）。
    空间未注册时返回 None，由调用方报错放弃，绝不回退前台。
    """
    try:
        wm = getattr(root, "workspace_manager", None)
        if wm is None:
            return None
        entry = wm.registry.get_by_id(workspace_id_for(workspace_dir))
        if entry is None:
            return None
        session = await root.ensure_workspace_session(entry)
        coara = getattr(session, "coara", None)
        if coara is not None:
            return coara._tool_manager.tools.get("delegate")
    except Exception:  # noqa: BLE001
        pass
    return None


def _workspace_entry_llm(root: Any, workspace_dir: str) -> tuple[str | None, str | None]:
    """目标空间的 LLM（entry last-run = 最后正常对话运行的模型）；未绑定返回 (None, None)。

    janitor 无端归属，一定跟随空间 last-run；不再有 janitor 专属配置。
    delegate 父会话保证是目标空间会话，未绑定时由 delegate 继承父会话即可。
    """
    try:
        wm = getattr(root, "workspace_manager", None)
        if wm is None:
            return None, None
        from src.workspace.llm_binding import entry_llm_override

        entry = wm.registry.get_by_id(workspace_id_for(workspace_dir))
        return entry_llm_override(entry)
    except Exception:  # noqa: BLE001
        return None, None


def _janitor_min_interval() -> float:
    """Cooldown seconds between dispatches per workspace (0 = disabled)."""
    try:
        from src.core.config import config_manager

        cfg = config_manager.config
        session = getattr(cfg, "session", None) if cfg else None
        if session is None:
            return 600.0
        return max(0.0, float(getattr(session, "janitor_min_interval_seconds", 600.0)))
    except Exception:
        return 600.0


def _schedule_deferred_dispatch(root: Any, workspace_id: str, delay: float, payload: dict[str, Any]) -> None:
    """Defer one dispatch to *delay* seconds later; never drop the request.

    Only one timer per workspace: a trigger arriving while a timer is pending
    just refreshes the payload (the snapshot was already refreshed by the
    caller), keeping the original fire time.
    """
    existing = _janitor_deferred.get(workspace_id)
    if existing is not None:
        existing["payload"].update(payload)
        if payload.get("history_snapshot") is not None:
            existing["payload"]["history_snapshot"] = payload["history_snapshot"]
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(f"janitor defer skipped (no running loop) for {workspace_id}")
        return
    handle = loop.call_later(delay, _fire_deferred_dispatch, workspace_id)
    _janitor_deferred[workspace_id] = {
        "handle": handle,
        "payload": dict(payload),
        "root": root,
    }
    logger.info(f"janitor deferred for {payload.get('workspace_name')}: fires in {delay:.0f}s")


def _fire_deferred_dispatch(workspace_id: str) -> None:
    entry = _janitor_deferred.pop(workspace_id, None)
    if entry is None:
        return
    payload = entry["payload"]
    root = entry["root"]

    async def _run() -> None:
        try:
            await dispatch_janitor_background(
                root,
                workspace_name=str(payload.get("workspace_name") or ""),
                workspace_dir=str(payload.get("workspace_dir") or ""),
                coara_home=str(payload.get("coara_home") or ""),
                activity_epoch=payload.get("activity_epoch"),
                history_snapshot=payload.get("history_snapshot"),
            )
        except Exception as exc:
            logger.warning(f"janitor deferred dispatch failed for {workspace_id}: {exc}")

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.warning(f"janitor deferred dispatch skipped (no running loop) for {workspace_id}")


def _schedule_overview_refresh(root: Any, workspace_id: str, workspace_dir: str) -> None:
    """After a janitor run, splice the fresh overview into the live session seed.

    Waits for a turn boundary (turns in flight are never touched); gives up
    after ``_OVERVIEW_REFRESH_MAX_WAIT_S``. One pending refresh per workspace —
    the pending run reads the newest ws.md anyway. Locates the standalone
    工作空间概况 message (or legacy combined env+ws seed) by prefix.
    """
    if not workspace_dir or workspace_id in _overview_refresh_pending:
        return
    _overview_refresh_pending.add(workspace_id)

    async def _run() -> None:
        try:
            deadline = time.monotonic() + _OVERVIEW_REFRESH_MAX_WAIT_S
            while True:
                session = getattr(root, "_sessions", {}).get(workspace_id)
                coara = getattr(session, "coara", None) if session is not None else None
                if coara is None:
                    return
                if not coara.has_active_turn():
                    await _splice_session_overview(coara, workspace_dir)
                    return
                if time.monotonic() >= deadline:
                    return
                await asyncio.sleep(_OVERVIEW_REFRESH_RETRY_S)
        except Exception as exc:
            logger.warning(f"overview refresh failed for {workspace_id}: {exc}")
        finally:
            _overview_refresh_pending.discard(workspace_id)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        _overview_refresh_pending.discard(workspace_id)


async def _splice_session_overview(coara: Any, workspace_dir: str) -> bool:
    """Replace the workspace-overview seed message (or legacy combined seed segment).

    Skips when the session is too old (prefix rebuild would be expensive),
    when no overview seed is present, or when the overview is already current.
    """
    from src.coara.injections.context_modules import replace_ws_overview_in_history

    history = getattr(coara, "message_history", None)
    if not history or len(history) > OVERVIEW_REFRESH_MAX_MESSAGES:
        return False
    if not replace_ws_overview_in_history(history, workspace_dir):
        return False
    try:
        await asyncio.to_thread(coara.persist_session_to_disk)
    except Exception as exc:
        logger.warning(f"overview refresh persist failed: {exc}")
    try:
        coara._emit_trace(
            "workspace_overview_refreshed",
            "工作空间概况已更新",
            payload={"workspace_dir": workspace_dir},
        )
    except Exception as exc:  # trace 失败不影响替换结果
        logger.debug(f"overview refresh trace failed: {exc}")
    logger.info(f"workspace overview spliced into live session ({workspace_dir})")
    return True


def _flight_task_running(flight: dict[str, Any]) -> bool:
    """在飞判定：流程自管的 asyncio.Task（机制化后不再经 BackgroundAgentManager）。"""
    task = flight.get("task")
    return task is not None and not task.done()


def _schedule_dirty_rerun(root: Any, workspace_id: str) -> None:
    """When an in-flight janitor finishes, start one more run if dirty.

    Also the completion path that persists the maintained activity epoch:
    the disk marker is written only when the janitor run actually finishes,
    so a process killed mid-maintenance leaves no marker and the startup
    catch-up re-dispatches the epoch.
    """
    flight = _janitor_flights.get(workspace_id)
    if not flight:
        return
    task_id = str(flight.get("task_id") or "")
    task = flight.get("task")
    if task is None or task.done():
        return
    if flight.get("_done_hooked"):
        return
    flight["_done_hooked"] = True

    def _on_done(done_task: asyncio.Task) -> None:
        flight_now = _janitor_flights.get(workspace_id)
        if flight_now is None:
            return
        if str(flight_now.get("task_id") or "") != task_id:
            return
        dirty = bool(flight_now.get("dirty"))
        payload = {
            "workspace_name": flight_now.get("workspace_name"),
            "workspace_dir": flight_now.get("workspace_dir"),
            "coara_home": flight_now.get("coara_home"),
            "activity_epoch": flight_now.get("activity_epoch"),
            "history_snapshot": flight_now.get("history_snapshot"),
        }
        _janitor_flights.pop(workspace_id, None)
        # 新概况写盘完成：把它替换进存活会话的环境种子（turn 边界外等待）。
        _schedule_overview_refresh(root, workspace_id, str(payload["workspace_dir"] or ""))
        # 维护完成后才落已维护标记；取消/失败不写，重启 catch-up 兜底重派。
        _mark_activity_maintained(payload, done_task)
        if not dirty:
            return
        if not payload["workspace_name"] or not payload["workspace_dir"] or not payload["coara_home"]:
            return

        async def _rerun() -> None:
            try:
                await dispatch_janitor_background(
                    root,
                    workspace_name=str(payload["workspace_name"]),
                    workspace_dir=str(payload["workspace_dir"]),
                    coara_home=str(payload["coara_home"]),
                    activity_epoch=payload["activity_epoch"],
                    history_snapshot=payload.get("history_snapshot"),
                )
            except Exception as exc:
                logger.warning(f"janitor dirty re-run failed for {workspace_id}: {exc}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_rerun())
        except RuntimeError:
            logger.warning(f"janitor dirty re-run skipped (no running loop) for {workspace_id}")

    task.add_done_callback(_on_done)


def _record_janitor_failure(payload: dict[str, Any]) -> None:
    """Record a failed/cancelled janitor run for in-process backoff retry.

    已消耗过补派名额的 epoch 不再记录，防止「失败→退避→补派→再失败」循环。
    """
    epoch = payload.get("activity_epoch")
    workspace_dir = str(payload.get("workspace_dir") or "")
    if epoch is None or not workspace_dir:
        return
    try:
        key = (workspace_id_for(workspace_dir), float(epoch))
    except (TypeError, ValueError):
        return
    if key in _janitor_retried:
        return
    _janitor_failures[key[0]] = (key[1], time.monotonic())


def consume_janitor_retry(workspace_dir: str, activity_epoch: float) -> bool:
    """失败退避窗口已过时返回 True 并消费补派名额（每 epoch 进程内最多一次）。"""
    workspace_id = workspace_id_for(workspace_dir)
    entry = _janitor_failures.get(workspace_id)
    if entry is None:
        return False
    epoch, failed_at = entry
    if epoch != activity_epoch:
        return False
    if time.monotonic() - failed_at < JANITOR_RETRY_BACKOFF_SECONDS:
        return False
    _janitor_failures.pop(workspace_id, None)
    _janitor_retried.add((workspace_id, epoch))
    return True


def janitor_failure_pending(workspace_dir: str, activity_epoch: float) -> bool:
    """同 epoch 仍有未消费的失败记录（退避中或可补派）——不得当成「已维护」。"""
    workspace_id = workspace_id_for(workspace_dir)
    entry = _janitor_failures.get(workspace_id)
    if entry is None:
        return False
    return entry[0] == activity_epoch


def _mark_activity_maintained(payload: dict[str, Any], done_task: asyncio.Task) -> None:
    """Persist the maintained activity epoch once the janitor run finishes."""
    epoch = payload.get("activity_epoch")
    if epoch is None:
        return
    try:
        if done_task.cancelled():
            _record_janitor_failure(payload)
            return
        if done_task.exception() is not None:
            _record_janitor_failure(payload)
            return
        result = done_task.result()
    except Exception:
        _record_janitor_failure(payload)
        return
    if getattr(result, "is_error", False) or getattr(result, "is_cancelled", False):
        logger.warning(
            f"janitor run for {payload.get('workspace_name')} ended with error; "
            "activity epoch left unmarked so catch-up retries it"
        )
        _record_janitor_failure(payload)
        return
    workspace_dir = str(payload.get("workspace_dir") or "")
    coara_home = str(payload.get("coara_home") or "")
    if not workspace_dir or not coara_home:
        return
    try:
        to_save = float(epoch)
    except (TypeError, ValueError):
        logger.warning(f"janitor activity epoch not numeric: {epoch!r}")
        return
    # turn_end 可能已把标记顺延到更新的 epoch；完成钩子不得用旧 epoch 回写覆盖。
    existing = load_janitor_activity(workspace_dir, coara_home)
    if existing is not None and existing > to_save:
        to_save = existing
    try:
        save_janitor_activity(workspace_dir, coara_home, to_save)
    except Exception as exc:
        logger.warning(f"janitor activity persist failed: {exc}")


def _store_history_snapshot(target: dict[str, Any], history_snapshot: list[Any] | None) -> None:
    """压缩派发链：最新压缩前完整历史覆盖旧快照（连续压缩合并时取最后一次）。"""
    if history_snapshot is not None:
        target["history_snapshot"] = copy.deepcopy(history_snapshot)


async def dispatch_janitor_parallel_with_compress(coara: Any, *, original_history: list[Any] | None) -> None:
    """压缩成功后异步派 janitor：基于**压缩前**冻结历史沉淀记录与 ws.md。

    快照在压缩 LLM 启动前已 deepcopy；此处派发不读活会话（那时可能已是摘要）。
    携带当前 activity_epoch，避免 ~2h 空闲扫描对同一段对话再派一模一样的维护。
    失败只记日志，绝不回滚压缩结果。
    """
    root = getattr(coara, "_root_ref", None)
    if root is None:
        return
    wm = getattr(root, "workspace_manager", None)
    if wm is None:
        return
    workspace_dir = str(getattr(coara, "workspace_dir", "") or "")
    if not workspace_dir:
        return
    try:
        resolved = Path(workspace_dir).expanduser().resolve()
        workspace_id = wm.match_path_to_workspace_id(resolved) or workspace_id_for(str(resolved))
        entry = wm.registry.get_by_id(workspace_id) if workspace_id else None
        workspace_name = str(getattr(entry, "name", "") or "") if entry is not None else ""
    except Exception:
        workspace_id = workspace_id_for(workspace_dir)
        workspace_name = ""
    if not workspace_name:
        return

    activity_map = getattr(root, "_workspace_activity_at", None)
    activity_epoch: float | None = None
    if isinstance(activity_map, dict) and workspace_id:
        raw = activity_map.get(workspace_id)
        if raw is not None:
            with contextlib.suppress(TypeError, ValueError):
                activity_epoch = float(raw)

    async def _run() -> None:
        try:
            task_id = await dispatch_janitor_background(
                root,
                workspace_name=workspace_name,
                workspace_dir=workspace_dir,
                coara_home=str(wm.coara_home),
                activity_epoch=activity_epoch,
                history_snapshot=copy.deepcopy(original_history) if original_history else None,
            )
            # 内存单飞标记：与 idle 扫描对齐，避免压缩管家未落盘前空闲又派一次。
            if task_id and activity_epoch is not None and workspace_id:
                maintained = getattr(root, "_janitor_activity_at", None)
                if isinstance(maintained, dict):
                    maintained[workspace_id] = activity_epoch
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"janitor dispatch after compress failed for {workspace_name}: {exc}")

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.warning(f"janitor after compress skipped (no running loop) for {workspace_name}")


def schedule_janitor_parallel_with_compress(coara: Any, *, original_history: list[Any] | None) -> None:
    """Fire-and-forget：压缩**成功**后派 janitor（调用方已冻结压缩前历史）。

    名称保留「parallel」历史语义；实际在压缩返回 compressed=True 之后调度，
    快照仍是压缩前原文，不读活会话摘要。
    """
    if not original_history:
        return
    frozen = copy.deepcopy(original_history)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(dispatch_janitor_parallel_with_compress(coara, original_history=frozen))


async def dispatch_janitor_background(
    root: Any,
    *,
    workspace_name: str,
    workspace_dir: str,
    coara_home: str,
    activity_epoch: float | None = None,
    history_snapshot: list[Any] | None = None,
    force: bool = False,
) -> str | None:
    """Schedule background janitor; return ``task_id`` or None.

    Single-flight per workspace: if a janitor is already running, mark dirty
    and return the existing ``task_id`` (no second LLM); when that run finishes,
    one follow-up dispatch runs if still dirty.

    Cooldown: when the last real dispatch happened less than
    ``session.janitor_min_interval_seconds`` ago, the request is deferred (one
    timer per workspace, latest payload wins) instead of dropped. Returns None
    in that case (no task yet). ``force=True`` bypasses the cooldown — used by
    explicit user intents (manual ``/new``) where deferral is surprising.

    ``activity_epoch`` (idle/startup catch-up paths) is carried in the flight
    and persisted as the maintained marker only when the run completes —
    dispatch-time marking would permanently lose the epoch if the process
    died mid-maintenance.
    """
    workspace_id = workspace_id_for(workspace_dir)

    flight = _janitor_flights.get(workspace_id)
    if flight and _flight_task_running(flight):
        flight["dirty"] = True
        flight["workspace_name"] = workspace_name
        flight["workspace_dir"] = workspace_dir
        flight["coara_home"] = coara_home
        if activity_epoch is not None:
            # 在飞的这一轮（及其 dirty 补跑）完成时按新 epoch 落标记
            flight["activity_epoch"] = activity_epoch
        _store_history_snapshot(flight, history_snapshot)
        logger.info(f"janitor coalesce for {workspace_name}: already running task_id={flight.get('task_id')}; dirty=1")
        return str(flight.get("task_id") or "") or None

    min_interval = _janitor_min_interval()
    if min_interval > 0 and not force:
        last = _janitor_last_dispatch.get(workspace_id)
        if last is not None:
            remaining = min_interval - (time.monotonic() - last)
            if remaining > 0:
                defer_payload = {
                    "workspace_name": workspace_name,
                    "workspace_dir": workspace_dir,
                    "coara_home": coara_home,
                    "activity_epoch": activity_epoch,
                }
                _store_history_snapshot(defer_payload, history_snapshot)
                _schedule_deferred_dispatch(
                    root,
                    workspace_id,
                    remaining,
                    defer_payload,
                )
                return None

    # 机制化：janitor 是内核固化维护流程，不是子智能体——内核直接建临时 CoaraBase
    # 跑一轮受限维护回合（janitor_maintenance.run_janitor_maintenance），不经 delegate，
    # 无 sa- 身份/台账/回投。单飞/冷却/落标记/概况回注编排骨架不变。
    from src.coara.janitor_maintenance import run_janitor_maintenance

    prompt = build_janitor_prompt(workspace_dir=workspace_dir, kind=_workspace_kind(root, workspace_dir))
    task = run_janitor_maintenance(
        root,
        workspace_name=workspace_name,
        workspace_dir=workspace_dir,
        coara_home=coara_home,
        prompt=prompt,
        history_snapshot=history_snapshot,
    )
    if task is None:
        logger.error(f"janitor maintenance build failed for {workspace_name}")
        return None

    # 流程无 sa- id；task_id 用语义化的维护回合 id（供日志/单飞键/测试断言）。
    task_id = f"janitor-{workspace_id}-{id(task) & 0xFFFFFFFF:x}"
    _janitor_flights[workspace_id] = {
        "task_id": task_id,
        "task": task,
        "dirty": False,
        "workspace_name": workspace_name,
        "workspace_dir": workspace_dir,
        "coara_home": coara_home,
        "activity_epoch": activity_epoch,
        "history_snapshot": copy.deepcopy(history_snapshot) if history_snapshot else None,
        "_done_hooked": False,
    }
    _janitor_last_dispatch[workspace_id] = time.monotonic()
    _schedule_dirty_rerun(root, workspace_id)
    logger.info(f"janitor maintenance dispatched for {workspace_name} task_id={task_id}")
    return task_id


def reset_janitor_flights_for_tests() -> None:
    """Clear single-flight / cooldown / refresh state (tests only)."""
    for entry in _janitor_deferred.values():
        handle = entry.get("handle")
        if handle is not None:
            handle.cancel()
    _janitor_deferred.clear()
    _janitor_flights.clear()
    _janitor_last_dispatch.clear()
    _janitor_failures.clear()
    _janitor_retried.clear()
    _overview_refresh_pending.clear()


# ---------------------------------------------------------------------------
# 消息过目：handle=janitor 的事件落收件箱后不再派 LLM review（工具表与主会话一致，
# 主会话不挂 review）。动态留在收件箱，由用户经 /ws updates 处理。
# ---------------------------------------------------------------------------

async def dispatch_janitor_message_review(root: Any, update: Any) -> str | None:
    """handle=janitor：动态已入库；不再派 LLM 过目，留待用户 /ws updates。

    返回 None（无后台 task）。事件源仍会把内容写入工作空间动态收件箱。
    """
    _ = root
    workspace_name = str(getattr(update, "workspace", "") or "").strip()
    message_id = str(getattr(update, "message_id", "") or "").strip()
    logger.info(
        f"janitor LLM review retired; inbox keeps {workspace_name or '?'} "
        f"{message_id or '?'} for /ws updates"
    )
    return None
