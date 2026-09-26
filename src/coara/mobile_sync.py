"""Matrix hidden payloads for mobile slash-command panels"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from src.core.logger import logger

# Throttle activity-driven status pushes so CLI chatter does not flood Matrix.
_STATUS_PUSH_MIN_INTERVAL_S = 60.0
_last_status_push_mono: float = 0.0

# fire-and-forget 推送任务登记：防引用被 GC 提前回收，异常经 done_callback
# 记 debug（与 Root._bg_tasks / web_server._bg_tasks 同款模式，模块级宿主）
_pending_tasks: set[asyncio.Task[Any]] = set()


def _spawn_sync_task(awaitable: Any) -> None:
    task = asyncio.ensure_future(awaitable)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)

    def _on_done(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.opt(exception=exc).debug("mobile sync push task failed")

    task.add_done_callback(_on_done)


MODELS_START = "[COARA_MODELS]"
MODELS_END = "[/COARA_MODELS]"
WORKSPACES_START = "[COARA_WORKSPACES]"
WORKSPACES_END = "[/COARA_WORKSPACES]"
THINKING_START = "[COARA_THINKING]"
THINKING_END = "[/COARA_THINKING]"
USAGE_START = "[COARA_USAGE]"
USAGE_END = "[/COARA_USAGE]"
STATUS_START = "[COARA_STATUS]"
STATUS_END = "[/COARA_STATUS]"

# Envelope (start, end) pairs used by is_mobile_sync_query for anchored checks.
_SYNC_ENVELOPES = (
    (MODELS_START, MODELS_END),
    (WORKSPACES_START, WORKSPACES_END),
    (THINKING_START, THINKING_END),
    (USAGE_START, USAGE_END),
    (STATUS_START, STATUS_END),
)


def _wrap(start: str, end: str, body: dict[str, Any]) -> str:
    return f"{start}\n{json.dumps(body, ensure_ascii=False)}\n{end}"


def format_models_payload(*, current: str, choices: list[dict[str, Any]], workspace: str = "") -> str:
    # workspace = 视图空间名：手机端据此确认回推归属，防止切空间竞态下
    # 用旧空间的 models 回推结算 pendingWorkspaceSwitch（空间名+旧模型错配分隔线）
    return _wrap(
        MODELS_START,
        MODELS_END,
        {"type": "models", "current": current, "choices": choices, "workspace": workspace},
    )


def format_workspaces_payload(*, active_id: str, workspaces: list[dict[str, Any]]) -> str:
    return _wrap(
        WORKSPACES_START,
        WORKSPACES_END,
        {"type": "workspaces", "active_id": active_id, "workspaces": workspaces},
    )


def format_thinking_payload(
    *,
    enabled: bool,
    source: str,
    note: str,
    model: str,
    level: str = "",
    supports_levels: bool = False,
) -> str:
    return _wrap(
        THINKING_START,
        THINKING_END,
        {
            "type": "thinking",
            "enabled": enabled,
            "source": source,
            "note": note,
            "model": model,
            "level": level,
            "supports_levels": supports_levels,
        },
    )


def format_usage_payload(
    *,
    summary: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_turns: int = 0,
) -> str:
    return _wrap(
        USAGE_START,
        USAGE_END,
        {
            "type": "usage",
            "summary": summary,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "llm_turns": llm_turns,
        },
    )


def format_status_payload(
    *,
    summary: str,
    workspace: str = "",
    session_id: str = "",
    last_user_activity_at: int = 0,
    idle_timeout_seconds: int = 7200,
    session_event: str | None = None,
    model: str | None = None,
) -> str:
    """Build ``[COARA_STATUS]`` JSON"""
    body: dict[str, Any] = {
        "type": "status",
        "summary": summary,
        "workspace": workspace,
        "session_id": session_id,
        "last_user_activity_at": int(last_user_activity_at),
        "idle_timeout_seconds": int(idle_timeout_seconds),
    }
    if session_event is not None:
        body["session_event"] = session_event
    if model is not None:
        body["model"] = model
    return _wrap(STATUS_START, STATUS_END, body)


# at startup as ``(room_id, text) -> awaitable``. The ingress prefilter runs
_SYNC_SENDER: Any = None


def configure_mobile_sync_sender(sender: Any) -> None:
    """Register the room-text sender used for sync queries/payloads."""
    global _SYNC_SENDER
    _SYNC_SENDER = sender


def reset_status_push_throttle_for_tests() -> None:
    """Clear activity-push throttle (tests only)."""
    global _last_status_push_mono
    _last_status_push_mono = 0.0


def _pinned_end_view_id(root: Any, end: str) -> str | None:
    """MagicMock-safe pin 探测：优先 RootCoara.pinned_view_id，否则读存储字段。"""
    pinned = getattr(root, "pinned_view_id", None)
    if callable(pinned):
        try:
            vid = pinned(end)
            return vid if isinstance(vid, str) and vid else None
        except Exception:
            return None
    attr = f"_{end}_view_workspace_id"
    vid = getattr(root, attr, None)
    return vid if isinstance(vid, str) and vid else None


def _view_coara(root: Any) -> Any:
    """matrix 手机端视图空间 coara；未独立绑定或替身 root 时回退前台。

    与 ``ingress_helpers.matrix_view_coara`` 同构；本模块不反向依赖 matrix_client。
    """
    view_id = _pinned_end_view_id(root, "matrix")
    if view_id:
        resolver = getattr(root, "resolve_matrix_view_coara", None)
        if callable(resolver):
            try:
                return resolver()
            except Exception:
                logger.debug("resolve matrix view coara failed; fall back to foreground", exc_info=True)
    try:
        return root.foreground_coara
    except Exception:
        return root


def _resolve_status_room_id(root: Any) -> str:
    notify = getattr(root, "matrix_notify", None)
    if notify is None:
        return ""
    resolve = getattr(notify, "resolve_room_id", None)
    if callable(resolve):
        return str(resolve() or "").strip()
    return ""


def _resolve_push_target(root: Any, fallback_room_id: str) -> str:
    """Room the phone is actually syncing, else the configured notify room."""
    return _resolve_status_room_id(root) or (fallback_room_id or "").strip()


def _push_built_payload(root: Any, build: Any, *, fallback_room_id: str, label: str) -> None:
    """Best-effort push of one panel payload; no-op without sender/room."""
    if _SYNC_SENDER is None:
        return
    target = _resolve_push_target(root, fallback_room_id)
    if not target:
        return
    try:
        payload = build(root)
    except Exception as exc:
        logger.warning(f"mobile {label} push: build failed: {exc}")
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(f"mobile {label} push: no running loop")
        return
    _spawn_sync_task(_SYNC_SENDER(target, payload))


def push_workspaces_payload(root: Any, *, fallback_room_id: str = "") -> None:
    """Push ``[COARA_WORKSPACES]`` after a switch or registry change.

    Best-effort: no-op when the sender is unconfigured or no room is known.
    """
    _push_built_payload(root, build_workspaces_payload, fallback_room_id=fallback_room_id, label="workspaces")


def push_models_payload(root: Any, *, fallback_room_id: str = "") -> None:
    """Push ``[COARA_MODELS]`` — each workspace slot has its own provider/model."""
    _push_built_payload(root, build_models_payload, fallback_room_id=fallback_room_id, label="models")


def push_thinking_payload(root: Any, *, fallback_room_id: str = "") -> None:
    """Push ``[COARA_THINKING]`` — thinking support depends on the active model."""
    _push_built_payload(root, build_thinking_payload, fallback_room_id=fallback_room_id, label="thinking")


def push_model_switch_payloads(
    root: Any,
    *,
    fallback_room_id: str = "",
    model_label: str = "",
) -> None:
    """Push models + thinking + status after a model switch"""
    push_models_payload(root, fallback_room_id=fallback_room_id)
    push_thinking_payload(root, fallback_room_id=fallback_room_id)
    push_status_payload(
        root,
        force=True,
        fallback_room_id=fallback_room_id,
        session_event="model_switch",
        model_label=model_label,
    )


def push_status_payload(
    root: Any,
    *,
    force: bool = False,
    fallback_room_id: str = "",
    session_event: str | None = None,
    workspace_name: str | None = None,
    model_label: str | None = None,
) -> None:
    """Push ``[COARA_STATUS]`` so the phone follows Root's idle clock / session_id"""
    global _last_status_push_mono
    if _SYNC_SENDER is None:
        return
    target = _resolve_push_target(root, fallback_room_id)
    if not target:
        return
    now = time.monotonic()
    if not force and _last_status_push_mono > 0 and now - _last_status_push_mono < _STATUS_PUSH_MIN_INTERVAL_S:
        return
    try:
        payload = build_status_payload(
            root,
            session_event=session_event,
            workspace_name=workspace_name,
            model_label=model_label,
        )
    except Exception as exc:
        logger.warning(f"mobile status push: build failed: {exc}")
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("mobile status push: no running loop")
        return
    _last_status_push_mono = now
    _spawn_sync_task(_SYNC_SENDER(target, payload))


def maybe_push_status_for_activity(root: Any) -> None:
    """Throttled status push after user activity (CLI / Web / Matrix)."""
    push_status_payload(root, force=False)


def event_matches_matrix_view(root: Any, payload: dict[str, Any]) -> bool:
    """仅当手机当前视图空间 = 事件发生空间时刷新（其它空间不刷手机）"""
    event_wid = str(payload.get("workspace_id") or "").strip()
    if not event_wid:
        return True
    pinned = getattr(root, "pinned_view_id", None)
    matrix_wid = pinned("matrix") if callable(pinned) else None
    return not (matrix_wid and str(matrix_wid) != event_wid)


def push_status_for_session_started(root: Any, event: Any, *, fallback_room_id: str = "") -> None:
    """``session_started`` 订阅回调：同空间任一端 /new，手机画「新会话」分隔线。

    手机自己发起的 /new（``matrix_new_command``）已本地画线，跳过避免重复。
    """
    payload = getattr(event, "payload", None) or {}
    if str(payload.get("interrupt_source") or "") == "matrix_new_command":
        return
    if not event_matches_matrix_view(root, payload):
        return
    push_status_payload(root, force=True, fallback_room_id=fallback_room_id, session_event="new_session")


def push_restart_completed(root: Any, *, fallback_room_id: str = "") -> None:
    """重启成功分割线（09-26 口径）：新内核就绪后由 matrix runner 调用。

    手机端靠 [COARA_STATUS] session_event=restarted 画「已重启」线（前端把事件
    映射成分割线标签）；web 的 divider 落带在 main 启动链已做。幂等由
    ``consume_restart_divider`` 保证（一条重启记录只画一次）。
    """
    push_status_payload(root, force=True, fallback_room_id=fallback_room_id, session_event="restarted")


def push_mobile_sync_payloads(root: Any, room_id: str) -> None:
    """Best-effort startup push of panel payloads."""
    if not room_id or _SYNC_SENDER is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 与 push_status_payload 同防护：无运行中的事件循环（如同步线程调用）
        # 时无从派发任务，静默跳过即可，不该直接崩
        logger.debug("mobile sync startup push: no running loop")
        return
    for build in (
        build_models_payload,
        build_workspaces_payload,
        build_thinking_payload,
        build_usage_payload,
        build_status_payload,
    ):
        try:
            payload = build(root)
        except Exception as exc:
            logger.warning(f"mobile sync startup push: {build.__name__} skipped: {exc}")
            continue
        _spawn_sync_task(_SYNC_SENDER(room_id, payload))


def is_mobile_sync_query(body: str) -> bool:
    """Phone → bot silent panel query: envelope-anchored with ``type == "query"``"""
    if "query" not in body:
        return False
    stripped = body.strip()
    return any(stripped.startswith(start) and end in stripped for start, end in _SYNC_ENVELOPES)


def try_handle_mobile_sync_query(root: Any, room_id: str, body: str) -> bool:
    """Consume a silent panel query: reply with the hidden payload only (no chat output)."""
    if not is_mobile_sync_query(body):
        return False
    if _SYNC_SENDER is None:
        logger.warning("mobile sync query received but sender not configured (configure_mobile_sync_sender)")
        return True
    try:
        stripped = body.strip()
        if stripped.startswith(MODELS_START):
            payload = build_models_payload(root)
        elif stripped.startswith(WORKSPACES_START):
            payload = build_workspaces_payload(root)
        elif stripped.startswith(USAGE_START):
            payload = build_usage_payload(root)
        elif stripped.startswith(STATUS_START):
            payload = build_status_payload(root)
        else:
            payload = build_thinking_payload(root)
    except Exception as exc:
        logger.warning(f"mobile sync query payload build failed: {exc}")
        return True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("mobile sync query: no running loop, payload not sent")
        return True
    _spawn_sync_task(_SYNC_SENDER(room_id, payload))
    return True


def _view_workspace_name(root: Any, view: Any) -> str:
    """解析视图空间名（供 payload 归属标记）；解析失败回退空串（旧端兼容）。"""
    try:
        manager = getattr(root, "workspace_manager", None)
        if manager is None:
            return ""
        from src.workspace.catalog import resolve_foreground_active_name

        return resolve_foreground_active_name(view, manager) or ""
    except Exception:
        return ""


def build_models_payload(root: Any) -> str:
    """Fresh models payload from current config (after list or switch)."""
    from src.core.config import config_manager
    from src.llm.model_catalog import list_model_choices

    view = _view_coara(root)
    current = f"{view.provider_name}·{view.model_name}"
    choices = [
        {"idx": idx, "key": choice.key, "label": choice.label or choice.key}
        for idx, choice in enumerate(list_model_choices(config_manager), start=1)
    ]
    return format_models_payload(current=current, choices=choices, workspace=_view_workspace_name(root, view))


def build_workspaces_payload(root: Any) -> str:
    """Fresh workspaces payload (after list or switch)."""
    from src.coara.commands.workspace import _build_workspace_list_result  # noqa: PLC0415

    result = _build_workspace_list_result(root)
    data = result.data or {}
    # active 按 matrix 视图：手机面板高亮的是手机看中的空间，非全局前台。
    # _build_workspace_list_result 行内 active 标的是全局前台——必须改写。
    view_id = _pinned_end_view_id(root, "matrix")
    active_id = view_id if view_id else str(data.get("active_id") or "")
    workspaces = []
    for raw in data.get("workspaces") or []:
        if not isinstance(raw, dict):
            continue
        # 服务端按端过滤：web_only 空间（工作流画布等）手机端渲染不了，
        # 不下发——与切换入口的 end_allowed 同一把尺，listing 不该比切换更宽。
        if str(raw.get("view") or "all") == "web_only":
            continue
        ws = dict(raw)
        wid = str(ws.get("id") or "")
        wname = str(ws.get("name") or "")
        ws["active"] = bool(active_id) and (wid == active_id or wname == active_id)
        workspaces.append(ws)
    return format_workspaces_payload(
        active_id=active_id,
        workspaces=workspaces,
    )


def build_thinking_payload(root: Any) -> str:
    """Fresh thinking-mode status for the mobile slash panel."""
    from src.llm.active_context import resolve_active_llm
    from src.llm.thinking_mode import (
        classify_thinking_support,
        current_level,
        describe_thinking_status,
        supports_levels,
    )

    ctx = resolve_active_llm(_view_coara(root))
    enabled, source, note = describe_thinking_status(
        ctx.model,
        base_url=ctx.base_url,
        driver=ctx.driver,
    )
    kind = classify_thinking_support(ctx.model, base_url=ctx.base_url, driver=ctx.driver)
    model = f"{ctx.provider_name}/{ctx.model}" if ctx.provider_name else ctx.model
    return format_thinking_payload(
        enabled=enabled,
        source=source,
        note=note,
        model=model,
        level=current_level(),
        supports_levels=supports_levels(kind),
    )


def build_usage_payload(root: Any) -> str:
    """Compact session usage snippet for the slash panel."""
    store = getattr(root, "_usage_store", None)
    if store is not None:
        store.flush(timeout=0.2)

    from src.runtime.usage_query import resolve_default_usage_path, summarize_session_stats

    # 视图空间的 usage 目录与其 session_id：手机面板显示的是手机看中的空间
    view = _view_coara(root)
    try:
        events_path = resolve_default_usage_path(getattr(view, "workspace_dir", None))
    except Exception:
        from src.runtime.usage_query import resolve_usage_path_for_root

        events_path = resolve_usage_path_for_root(root)
    stats = summarize_session_stats(events_path, str(getattr(view, "session_id", "") or ""))
    if stats.llm_turns == 0 and stats.input_tokens == 0 and stats.output_tokens == 0:
        summary = "本会话暂无用量"
    else:
        summary = f"输入 {stats.input_tokens:,} · 输出 {stats.output_tokens:,}"
    return format_usage_payload(
        summary=summary,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        llm_turns=stats.llm_turns,
    )


def build_status_payload(
    root: Any,
    *,
    session_event: str | None = None,
    workspace_name: str | None = None,
    model_label: str | None = None,
) -> str:
    """Compact workspace + conversation depth for the slash status row"""
    from pathlib import Path

    from src.core.config import config_manager
    from src.workspace.catalog import resolve_foreground_active_name

    # 面板整体按 matrix 视图出（方案 D6）：手机看中的空间的对话深度/session/模型
    view = _view_coara(root)
    try:
        status_data = view.get_status()
    except Exception:
        status_data = root.get_status()
    workspace = (workspace_name or "").strip()
    if not workspace:
        manager = getattr(root, "workspace_manager", None)
        if manager is not None:
            workspace = resolve_foreground_active_name(view, manager) or ""
        if not workspace:
            workspace_dir = str(status_data.get("workspace_dir") or "").strip()
            if workspace_dir:
                workspace = Path(workspace_dir).name
    msg_count = int(status_data.get("message_count") or 0)
    summary = f"{workspace} · 对话 {msg_count} 条" if workspace else f"对话 {msg_count} 条"

    session_id = ""
    try:
        session_id = str(getattr(view, "session_id", "") or "")
    except Exception:
        session_id = str(getattr(root, "session_id", "") or "")

    last_activity = float(getattr(root, "_last_user_activity_at", 0.0) or 0.0)
    last_ms = int(last_activity * 1000) if last_activity > 0 else 0

    idle_timeout = 7200.0
    try:
        cfg = config_manager.config
        idle_timeout = float(getattr(getattr(cfg, "session", None), "idle_timeout_seconds", 7200) or 7200)
    except Exception:
        logger.debug("read idle_timeout config for mobile sync failed; using 7200s default", exc_info=True)

    model = (model_label or "").strip()
    if not model and session_event == "model_switch":
        try:
            provider = str(getattr(view, "provider_name", "") or "").strip()
            model_name = str(getattr(view, "model_name", "") or "").strip()
            model = f"{provider}·{model_name}" if provider else model_name
        except Exception:
            model = ""

    return format_status_payload(
        summary=summary,
        workspace=workspace,
        session_id=session_id,
        last_user_activity_at=last_ms,
        idle_timeout_seconds=int(idle_timeout),
        session_event=session_event,
        model=model or None,
    )
