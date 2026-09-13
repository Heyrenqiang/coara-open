"""内核录像带录制器：所有端、所有空间、所有会话统一落带的单点。

录像带是内核的对话存档，不是某一端的显示缓存。本模块把落带从「端注入的回调」
提升为内核组件：任何模块拿到一帧与它的归属（workspace / session / subject）就能
落带，不需要知道这帧来自 web、手机还是 CLI。

- 线：``resolve_web_view_path(workspace_dir, subject, session_id)``
- 序号：``WebViewStore`` 的跨进程文件锁分配，同一条线内单调不回退
- 失败：只记日志，绝不抛给回合

与端的关系：端只把帧交进来，落不落、落在哪条线一律由归属决定——端不参与判定。
归属缺失时宁可不落，也不猜（猜就是丢数据的一种）。
"""

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
    """进程内共享的视图存储：录制器与读取端（WebServer）必须是同一份实例。

    两份实例虽然靠跨进程文件锁仍不会撞号，但会各自持有不同的内存镜像；
    统一成一份，落带与快照读到的就是同一条线。
    """
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
    """把一帧写进它自己那条线，返回分配到的 view_seq。

    不落带的帧（子智能体过程帧）与落带失败都返回 None——该类帧没有可对账的
    序号，端侧按「丢弃 + 记日志」处理。帧须自带 ``workspace_dir`` 与
    ``session_id``，``subject`` 缺省 root。
    """
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
