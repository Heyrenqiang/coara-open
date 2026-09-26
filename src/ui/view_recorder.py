"""内核录像带录制器：所有端、所有空间、所有会话统一落带的单点"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.ui.web_views import WebViewStore

_SHARED_STORE: WebViewStore | None = None
_SHARED_LOCK = threading.Lock()
# (workspace_dir, coara_home) -> persist 闭包。闭包只捕获两个值，缓存避免每次
# 落带都重解析 coara_home。
_PERSIST_CACHE: dict[tuple[str, str], Any] = {}
_PERSIST_CACHE_MAX = 64


def shared_view_store() -> WebViewStore:
    """进程内共享的视图存储：录制器与读取端（WebServer）必须是同一份实例"""
    global _SHARED_STORE
    if _SHARED_STORE is None:
        with _SHARED_LOCK:
            if _SHARED_STORE is None:
                _SHARED_STORE = WebViewStore()
    return _SHARED_STORE


def _persist_for(workspace_dir: str, coara_home: Path | None) -> Any:
    key = (workspace_dir, str(coara_home or ""))
    persist = _PERSIST_CACHE.get(key)
    if persist is None:
        persist = shared_view_store().make_persist(workspace_dir, coara_home=coara_home)
        if len(_PERSIST_CACHE) >= _PERSIST_CACHE_MAX:
            _PERSIST_CACHE.clear()
        _PERSIST_CACHE[key] = persist
    return persist


def record_view_frame(frame: dict[str, Any], *, coara_home: Path | None = None) -> int | None:
    """把一帧写进它自己那条线，返回分配到的 view_seq"""
    if not isinstance(frame, dict):
        return None
    workspace_dir = str(frame.get("workspace_dir") or "")
    if not workspace_dir:
        return None
    # 帧形态归一：内核帧把内容放在 payload 里、类别用 kind；端侧流对象的帧是
    # 平铺字段、类别用 type。落带与读取只认后者（make_persist 的形状），这里换算。
    payload = frame.get("payload")
    if isinstance(payload, dict):
        merged = {k: v for k, v in frame.items() if k != "payload"}
        for key, value in payload.items():
            merged.setdefault(key, value)
        frame = merged
    if "type" not in frame and frame.get("kind"):
        frame = {**frame, "type": frame["kind"]}
    try:
        return _persist_for(workspace_dir, coara_home)(frame)
    except Exception as exc:  # noqa: BLE001 — 落带是观察动作，失败绝不中断回合
        logger.warning(f"view recorder failed: {exc}")
        return None
