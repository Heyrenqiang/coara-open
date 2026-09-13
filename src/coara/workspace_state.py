"""Per-workspace session state save/restore.

Enables smooth workspace switching: when the user switches from workspace A to
workspace B and back, workspace A's conversation context (message history,
session id) is restored so they can continue where they left off.

If more than ``session_stale_seconds()`` (config ``session.idle_timeout_seconds``,
default 7200) have elapsed since the workspace was
last active, the session is considered stale and a fresh session is started —
both when restoring from disk and when switching into a cached in-memory
session (see ``RootCoara.switch_workspace``).

Persistence model
-----------------
* In-memory state is held by the ``WorkspaceSession`` architecture (one
  ``CoaraBase`` per non-default workspace) — instant switch within a running
  process.
* On-disk index at ``<coara_home>/workspaces/<workspace_id>/session_state.json``
  storing the last ``session_id`` and ``last_updated`` (主会话).
* Flow 第二主体索引：``flow_session_state.json``（独立文件，不覆盖主会话索引）。
* Full agent ``message_history`` (user / assistant / tool_calls / tool_results /
  mid-turn continuations) is the append-only session event log
  ``<coara_home>/workspaces/<workspace_id>/session_events.jsonl``
  (唯一事实源；恢复 = 事件重放投影，见 ``src/session_log/``；主会话与
  Flow 共用此文件，靠 ``session_id`` + ``agent_kind`` 区分)。
* UI 对话行（Web hydrate / dashboard 会话列表）由 L1 事件带经
  ``src/session_log/conversation_projection.py`` 投影派生：Web hydrate 过滤
  ``source=web``；Flow hydrate 用工作流系统带过滤 ``source=web-flow``；
  回合回撤/历史截断由 history/shadow 影子语义自动覆盖。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from src.core.coara_home import resolve_coara_home, workspace_id_for
from src.core.json_store import write_text_atomic
from src.core.logger import logger

SESSION_STALE_SECONDS = 7200
TURN_IN_FLIGHT_FILENAME = "turn_in_flight.json"
FLOW_SESSION_STATE_FILENAME = "flow_session_state.json"
FLOW_TURN_IN_FLIGHT_FILENAME = "flow_turn_in_flight.json"
REVEALED_TOOLS_FILENAME = "revealed_tools.json"
RESTART_NOTICE_FILENAME = "restart_notice.json"

# 恢复时发现 in-flight 标记（进程在回合进行中被杀）注入历史的注记
INTERRUPTED_SESSION_NOTE = (
    "上次回合因进程中断（强制结束、断电或崩溃）未正常结束，"
    "该轮的用户消息与模型回复可能未保存到本会话历史；"
    "中断前已完成的工具操作（如文件修改）仍然生效。"
    "请基于当前实际状态继续，不要假设上一轮已完成。"
)


def session_stale_seconds() -> float:
    """Effective staleness threshold in seconds.

    Single source of truth: ``session.idle_timeout_seconds`` (default 7200,
    same clock as the Root idle watcher). ``0`` disables the mechanism —
    sessions never go stale. ``SESSION_STALE_SECONDS`` is only the fallback
    when config is unavailable.
    """
    try:
        from src.core.config import config_manager

        cfg = config_manager.config
        return float(getattr(getattr(cfg, "session", None), "idle_timeout_seconds", SESSION_STALE_SECONDS))
    except Exception:
        return float(SESSION_STALE_SECONDS)


def is_session_stale(last_updated: float) -> bool:
    timeout = session_stale_seconds()
    if timeout <= 0:
        return False
    return time.time() - last_updated > timeout


def format_last_conversation_clock(last_updated: float, *, now: float | None = None) -> str:
    """Local clock for last message, e.g. ``14:30`` / ``昨天 14:30`` / ``3月2日 14:30``."""
    from datetime import datetime

    ts = datetime.fromtimestamp(last_updated)
    now_dt = datetime.fromtimestamp(now if now is not None else time.time())
    hm = f"{ts.hour:02d}:{ts.minute:02d}"
    if ts.date() == now_dt.date():
        return hm
    if (now_dt.date() - ts.date()).days == 1:
        return f"昨天 {hm}"
    if ts.year == now_dt.year:
        return f"{ts.month}月{ts.day}日 {hm}"
    return f"{ts.year}年{ts.month}月{ts.day}日 {hm}"


def workspace_session_has_conversation(coara: Any) -> bool:
    """True when the session has real chat beyond context-module seeds."""
    from src.coara.injections.context_modules import is_context_module_seed
    from src.core.types import MessageRole
    from src.utils.message_content import message_content_to_text

    for msg in getattr(coara, "message_history", None) or []:
        role = getattr(msg, "role", None)
        if role == MessageRole.ASSISTANT:
            if getattr(msg, "tool_calls", None):
                return True
            if message_content_to_text(getattr(msg, "content", "") or "").strip():
                return True
        elif role == MessageRole.TOOL_RESULT:
            return True
        elif role == MessageRole.USER:
            text = message_content_to_text(getattr(msg, "content", "") or "")
            if is_context_module_seed(text) or text.startswith("<系统"):
                continue
            if text.strip():
                return True
    return False


def format_workspace_switch_session_note(
    *,
    session_renewed: bool,
    last_active: float | None = None,
    now: float | None = None,
) -> str:
    """Session note after a workspace switch (no parentheses).

    ``session_renewed`` means this switch landed on a fresh/empty session
    (idle beyond timeout, or never had a real conversation) — show ``新会话``.
    Otherwise show the last conversation clock time.
    """
    if session_renewed:
        return "新会话"
    if last_active is not None:
        return f"上次对话 {format_last_conversation_clock(last_active, now=now)}"
    return "继续上次会话"


def format_workspace_switch_message(
    workspace_name: str,
    *,
    session_renewed: bool,
    last_active: float | None = None,
    now: float | None = None,
) -> str:
    """Full user-facing switch line, e.g. ``已切换到工作空间 foo 上次对话 14:30``."""
    note = format_workspace_switch_session_note(
        session_renewed=session_renewed,
        last_active=last_active,
        now=now,
    )
    return f"已切换到工作空间 {workspace_name} {note}"


def _state_dir(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    resolved_home = resolve_coara_home(workspace_path, coara_home)
    workspace_id = workspace_id_for(workspace_path)
    state_dir = resolved_home / "workspaces" / workspace_id
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _state_file_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/session_state.json``."""
    return _state_dir(workspace_path, coara_home=coara_home) / "session_state.json"


def _flow_state_file_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """Flow 第二主体索引：系统级（工作流目录），与工作空间无关。"""
    from src.workflow.paths import workflow_assets_root

    return workflow_assets_root(coara_home=coara_home) / FLOW_SESSION_STATE_FILENAME


def _turn_in_flight_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/turn_in_flight.json``."""
    return _state_dir(workspace_path, coara_home=coara_home) / TURN_IN_FLIGHT_FILENAME


def _flow_turn_in_flight_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    from src.workflow.paths import workflow_assets_root

    return workflow_assets_root(coara_home=coara_home) / FLOW_TURN_IN_FLIGHT_FILENAME


# Guards the tmp-write + replace pair. Async callers offload
# save_session_state() via asyncio.to_thread, so this lock only ever
# serializes worker threads — it never blocks the event loop.
_save_lock = threading.Lock()


def save_session_state(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
    last_updated: float | None = None,
) -> None:
    """Persist the last session_id and timestamp to disk.

    Called by ``persist_session_to_disk`` after turns, ``/new``, and shutdown
    so the next launch can restore the session and check the staleness timeout.
    """
    state_file = _state_file_path(workspace_path, coara_home=coara_home)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "last_updated": last_updated or time.time(),
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with _save_lock:
        try:
            write_text_atomic(state_file, data)
        except OSError as exc:
            # 会话索引持久化失败降级为告警，不抛穿回合刷屏（下次启动走新会话兜底）
            logger.warning(f"workspace state: 会话状态保存失败 {state_file}: {exc}")


def save_flow_session_state(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
    last_updated: float | None = None,
) -> None:
    """Persist Flow 第二主体的 session_id（独立文件，永不写主会话 session_state.json）。"""
    state_file = _flow_state_file_path(workspace_path, coara_home=coara_home)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "last_updated": last_updated or time.time(),
        "agent_kind": "flow",
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with _save_lock:
        try:
            write_text_atomic(state_file, data)
        except OSError as exc:
            logger.warning(f"workspace state: flow 会话状态保存失败 {state_file}: {exc}")


def mark_turn_in_flight(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
    turn_id: str = "",
) -> None:
    """回合开始时原子写入 in-flight 标记（tmp+replace，每回合一写）。

    进程在回合进行中被杀时标记保留在磁盘，下次 ``recover_session`` 据此
    注入「上次回合未完成」注记；正常回合结束由 ``clear_turn_in_flight`` 清除
    """
    marker = _turn_in_flight_path(workspace_path, coara_home=coara_home)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "started_at": time.time(),
    }
    data = json.dumps(payload, ensure_ascii=False)
    with _save_lock:
        write_text_atomic(marker, data)


def clear_turn_in_flight(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> None:
    """回合正常收尾后清除 in-flight 标记（标记不存在时为空操作）。"""
    marker = _turn_in_flight_path(workspace_path, coara_home=coara_home)
    with _save_lock, contextlib.suppress(OSError):
        marker.unlink()


def mark_flow_turn_in_flight(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
    turn_id: str = "",
) -> None:
    """Flow 第二主体回合 in-flight 标记（独立文件，不碰主会话标记）。"""
    marker = _flow_turn_in_flight_path(workspace_path, coara_home=coara_home)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "started_at": time.time(),
    }
    data = json.dumps(payload, ensure_ascii=False)
    with _save_lock:
        write_text_atomic(marker, data)


def clear_flow_turn_in_flight(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> None:
    marker = _flow_turn_in_flight_path(workspace_path, coara_home=coara_home)
    with _save_lock, contextlib.suppress(OSError):
        marker.unlink()


def consume_turn_in_flight(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
) -> dict[str, Any] | None:
    """读取并清除 in-flight 标记。

    仅当标记属于 ``session_id`` 时返回其内容；陈旧或归属不符的标记
    一并清除并返回 None（自愈，不留垃圾文件）
    """
    marker = _turn_in_flight_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    with _save_lock, contextlib.suppress(OSError):
        marker.unlink()
    if not isinstance(data, dict):
        return None
    if str(data.get("session_id") or "") != session_id:
        return None
    return data


def _restart_notice_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/restart_notice.json``."""
    return _state_dir(workspace_path, coara_home=coara_home) / RESTART_NOTICE_FILENAME


def append_restart_notice(
    workspace_path: Path,
    items: list[str],
    *,
    coara_home: Path | None = None,
) -> None:
    """追加待注入的重启清算条目（启动恢复发现的本空间被杀后台任务/子智能体）。

    多次发现合并进同一份通知；``restart_at`` 取最近一次写入时间。
    通知在 ``recover_session`` 恢复历史时消费并注入，一次性。
    """
    from src.core.time import now_iso

    path = _restart_notice_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    existing = data.get("items")
    if not isinstance(existing, list):
        existing = []
    merged = [str(it) for it in existing] + [str(it) for it in items if str(it).strip()]
    payload: dict[str, Any] = {"restart_at": now_iso(), "items": merged}
    text = json.dumps(payload, ensure_ascii=False)
    with _save_lock:
        write_text_atomic(path, text)


def consume_restart_notice(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> dict[str, Any] | None:
    """读取并清除待注入的重启清算通知；无通知或内容为空返回 None（自愈）。"""
    path = _restart_notice_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    with _save_lock, contextlib.suppress(OSError):
        path.unlink()
    if not isinstance(data, dict) or not data.get("items"):
        return None
    return data


def _revealed_tools_path(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """Resolve ``<coara_home>/workspaces/<workspace_id>/revealed_tools.json``."""
    return _state_dir(workspace_path, coara_home=coara_home) / REVEALED_TOOLS_FILENAME


def save_revealed_tools(
    workspace_path: Path,
    tool_names: Any,
    *,
    coara_home: Path | None = None,
) -> None:
    """持久化某工作空间已揭示（reveal）的挂起工具名。

    走既有 tmp+replace 原子写；空集合同样落盘覆盖旧状态，
    由 ``ToolManager`` 在 reveal/clear 时调用（#92）
    """
    target = _revealed_tools_path(workspace_path, coara_home=coara_home)
    names = sorted({str(n) for n in tool_names if n})
    payload: dict[str, Any] = {"revealed": names, "saved_at": time.time()}
    data = json.dumps(payload, ensure_ascii=False)
    with _save_lock:
        write_text_atomic(target, data)


def load_revealed_tools(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> list[str]:
    """读取已揭示工具名；文件缺失或损坏返回空列表。"""
    target = _revealed_tools_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    names = data.get("revealed") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n]


def load_session_state(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> tuple[str | None, float]:
    """Read the last (session_id, last_updated) from disk.

    Returns (None, 0.0) if no saved state exists. Older session_state.json
    files with an ``activated_skills`` field simply ignore that field.
    """
    state_file = _state_file_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        lu = data.get("last_updated", 0.0)
        return (str(sid) if sid else None, float(lu))
    except (OSError, json.JSONDecodeError, ValueError):
        return (None, 0.0)


def load_flow_session_state(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> tuple[str | None, float]:
    """Read Flow 第二主体的 (session_id, last_updated)（系统级索引）。"""
    state_file = _flow_state_file_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        lu = data.get("last_updated", 0.0)
        return (str(sid) if sid else None, float(lu))
    except (OSError, json.JSONDecodeError, ValueError):
        return (None, 0.0)


def load_usage_snapshot_for_session(
    session_id: str,
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
) -> dict[str, Any] | None:
    """Load the last provider-usage snapshot for ``session_id`` (from session/meta events)."""
    try:
        projection = replay_session_projection(workspace_path, session_id, coara_home=coara_home)
    except Exception:
        return None
    return projection.usage_snapshot


def replay_session_projection(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
    log_path: Path | None = None,
) -> Any:
    """投影完整恢复状态（消息 + 事件游标 + usage 快照）——恢复主入口。

    治本提速：优先读投影检查点（持久化时顺带缓存的基线投影 + seq 水位），
    命中后只增量投影水位之后的纯追加事件；检查点缺失/过期/增量含影子重写
    （history/shadow）则回退全量投影（倒序快读，仍远快于逐段全解）。恢复结果
    与全量投影逐条一致，录像带永久保存、一字节不动。详见 session_log/checkpoint.py。
    """
    from src.session_log.checkpoint import (
        incremental_needs_full_rebuild,
        load_checkpoint,
        splice_checkpoint_with_increment,
        write_checkpoint,
    )
    from src.session_log.project import project_session
    from src.session_log.store import read_session_events_tail, resolve_session_log_path

    path = log_path or resolve_session_log_path(workspace_path, coara_home=coara_home)
    session_dir = path.parent

    def _full_project() -> Any:
        events = read_session_events_tail(path, session_id)
        proj = project_session(events)
        # 顺带刷新检查点：下次启动走快路径。失败静默（检查点只是提速层）。
        with contextlib.suppress(Exception):
            write_checkpoint(
                session_dir,
                session_id=session_id,
                last_seq=proj.seqs[-1] if proj.seqs else 0,
                messages=proj.messages,
                seqs=proj.seqs,
                usage_snapshot=proj.usage_snapshot,
            )
        return proj

    checkpoint = load_checkpoint(session_dir, session_id)
    if checkpoint is None:
        return _full_project()
    # 增量读水位之后的新增事件；含影子重写则回退全量（影子作废检查点尾部）。
    increment_events = read_session_events_tail(path, session_id, min_seq=checkpoint.last_seq)
    if incremental_needs_full_rebuild(increment_events):
        return _full_project()
    if not increment_events:
        # 检查点新鲜（无新增），直接还原为投影对象（零增量拼接）
        return splice_checkpoint_with_increment(checkpoint, project_session([]))
    increment_proj = project_session(increment_events)
    return splice_checkpoint_with_increment(checkpoint, increment_proj)


def recover_session(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
    label: str = "",
    keep_session_id_on_replay_failure: bool = False,
) -> tuple[str, list[Any], Any] | None:
    """Recover (session_id, message_history, projection) by replaying the session event log.

    Used by ``WorkspaceSession._restore_from_disk`` for every peer workspace.

    Returns ``None`` when there is nothing to restore: no saved state, stale
    session, or an unrecoverable ``session_state.json`` load error.

    ``label`` is only used in warning log messages (workspace id or name).

    重放：按 session_id 过滤事件 → ``project_session`` 投影（含影子语义，
    恢复后 recorder 投影游标由调用方经 ``projection.seqs`` 对齐）。projection
    一并返回，调用方复用，不再二次全量重放（大磁带冷启动省 0.5-3s）。
    ``keep_session_id_on_replay_failure`` 为真时重放失败仍返回 ``(session_id, [], None)``。
    """
    from src.core.logger import logger

    try:
        last_sid, last_updated = load_session_state(
            workspace_path,
            coara_home=coara_home,
        )
    except Exception as exc:
        logger.warning(f"Failed to load session state for {label}: {exc}")
        return None

    if not last_sid or is_session_stale(last_updated):
        return None

    projection = None
    try:
        projection = replay_session_projection(workspace_path, last_sid, coara_home=coara_home)
        # 来源标签已废弃：恢复直接用裸投影（历史归属由投影 source 字段承载，
        # 当前端由情境后缀注入）。不再 retag_history_sources 重打内容标签。
        history = list(projection.messages)
    except Exception as exc:
        logger.warning(f"Failed to replay messages for {label}: {exc}")
        if not keep_session_id_on_replay_failure:
            return None
        history = []

    # 进程在回合进行中被杀时 in-flight 标记仍留在磁盘：注入注记让模型知道
    # 上一轮未正常结束（该轮消息可能未落盘），并清除标记避免重复注入
    if consume_turn_in_flight(workspace_path, last_sid, coara_home=coara_home) is not None:
        from src.core.message_tags import system_info
        from src.core.types import Message, MessageRole

        history.append(Message(role=MessageRole.USER, content=system_info(INTERRUPTED_SESSION_NOTE)))

    # 重启清算：启动恢复时发现的本空间被杀后台任务/子智能体，注入一条注记，
    # 让模型知道它们不会再有结果（避免基于「任务还在跑」的陈旧假设行动）
    notice = consume_restart_notice(workspace_path, coara_home=coara_home)
    if notice is not None:
        from src.core.message_tags import system_info
        from src.core.types import Message, MessageRole

        restart_at = str(notice.get("restart_at") or "").strip()
        items = [str(it) for it in notice.get("items") or [] if str(it).strip()]
        if items:
            head = f"coara 进程曾于 {restart_at} 重启。" if restart_at else "coara 进程曾重启。"
            lines = [
                head + "重启时以下后台任务/子智能体被终止，不会再有结果：",
                *(f"- {it}" for it in items),
            ]
            history.append(Message(role=MessageRole.USER, content=system_info("\n".join(lines))))

    return last_sid, history, projection


def consume_flow_turn_in_flight(
    workspace_path: Path,
    session_id: str,
    *,
    coara_home: Path | None = None,
) -> dict[str, Any] | None:
    """读取并清除 Flow in-flight 标记（逻辑同主会话，文件独立）。"""
    marker = _flow_turn_in_flight_path(workspace_path, coara_home=coara_home)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    with _save_lock, contextlib.suppress(OSError):
        marker.unlink()
    if not isinstance(data, dict):
        return None
    if str(data.get("session_id") or "") != session_id:
        return None
    return data


def recover_flow_session(
    workspace_path: Path,
    *,
    coara_home: Path | None = None,
    label: str = "flow",
    keep_session_id_on_replay_failure: bool = False,
) -> tuple[str, list[Any]] | None:
    """恢复 Flow 第二主体：(session_id, message_history)。

    与 ``recover_session`` 同构，但读 ``flow_session_state.json`` / flow in-flight，
    事件仍来自同一条 ``session_events.jsonl``（按 flow 的 session_id 过滤）。
    """
    from src.core.logger import logger

    try:
        last_sid, last_updated = load_flow_session_state(
            workspace_path,
            coara_home=coara_home,
        )
    except Exception as exc:
        logger.warning(f"Failed to load flow session state for {label}: {exc}")
        return None

    if not last_sid or is_session_stale(last_updated):
        return None

    from src.session_log.store import workflow_session_log_path

    try:
        projection = replay_session_projection(
            workspace_path,
            last_sid,
            coara_home=coara_home,
            log_path=workflow_session_log_path("flow", coara_home=coara_home),
        )
        history = list(projection.messages)
    except Exception as exc:
        logger.warning(f"Failed to replay flow messages for {label}: {exc}")
        if not keep_session_id_on_replay_failure:
            return None
        history = []

    if consume_flow_turn_in_flight(workspace_path, last_sid, coara_home=coara_home) is not None:
        from src.core.message_tags import system_info
        from src.core.types import Message, MessageRole

        history.append(Message(role=MessageRole.USER, content=system_info(INTERRUPTED_SESSION_NOTE)))

    return last_sid, history
