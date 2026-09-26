"""Thinking 行的轮换短语：整池洗牌袋 + 使用提示 + 每日定制短语.

词库来自 `docs/中文加载词库.md`（结构化副本 `loading_phrases.json`，两池：
witty 幽默梗 / quotes 名言改造版）。进程启动与 /new、切换工作空间时
整池打乱成袋（`reshuffle`），轮换时按 60 秒窗口顺序播放——袋多大一轮就多长
（常驻池约 365 条 → 六小时余才穷尽），展现在用户面前的是随机序列，
而不是「同一小批反复循环」。daily 定制词与使用提示一并进袋。
提示内容以 docs/manual/16-CLI与WebUI.md 为准。

daily 每日派发后还会结合当天工作写一批定制短语，落盘在
``records/agent/loading_phrases_custom.json``（同目录的 curator_state.json
旁）。文件带 ``date`` 字段，**只当天有效**：是今天则混入批次小头，
过期/缺失/损坏一律回落纯常驻词库（一日抛，旧文件不会误播）。启动加载、
每次 reshuffle 重读。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from src.core.logger import logger


def _load_library() -> dict[str, Any]:
    """加载内置词库；缺失/损坏时给最小兜底池，绝不让 CLI 渲染崩在模块导入。"""
    try:
        return json.loads(Path(__file__).with_name("loading_phrases.json").read_text(encoding="utf-8"))
    except Exception:
        logger.warning("loading_phrases.json missing or unreadable; using minimal fallback pool")
        return {
            "witty": ["正在为你干活……", "马上就好……", "思考中……"],
            "quotes": [],
        }


_DATA = _load_library()
WITTY_POOL: tuple[str, ...] = tuple(_DATA["witty"])
QUOTE_POOL: tuple[str, ...] = tuple(_DATA["quotes"])

CUSTOM_PHRASES_FILENAME = "loading_phrases_custom.json"

# 使用提示（快捷键以 docs/manual/16-CLI与WebUI.md 为准）
TIPS: tuple[str, ...] = (
    "小技巧：Ctrl+C 打断当前回合",
    "小技巧：Alt+V 粘贴剪贴板图片",
    "小技巧：@ 开头补全文件路径",
    "小技巧：/new 开新会话",
    "小技巧：/model 切换模型",
    "小技巧：/thinking 开关思考模式",
    "小技巧：Ctrl+C 可打断回复",
    "小技巧：/compact 压缩过长会话",
)

# 每批＝整池洗牌袋：常驻池（幽默+名言）全量 + 当日定制 + 使用提示，打乱后顺序播放。
# 旧版每批只抽 27 条、约 27 分钟就转完一圈，用户会反复看到同样几句（感知成「总是这几条」）。
_ROTATE_SECONDS = 60

# 定制短语文件位置（模块级 set/get，避免 import 环：cli ← records ← coara）
_custom_phrases_path: Path | None = None


def set_custom_phrases_path(path: Path | None) -> None:
    global _custom_phrases_path
    _custom_phrases_path = Path(path) if path is not None else None


def custom_payload(*, agent_dir: Path | None = None) -> dict[str, Any]:
    """当天定制短语载荷（CLI 轮换与 Web 下发共用同一来源）。

    一日抛语义不变：文件 date 不是今天则按过期返回空 phrases；
    缺失/损坏同样回落。agent_dir 缺省时读已登记的
    _custom_phrases_path（runtime bootstrap 设置）。
    """
    path = Path(agent_dir) / CUSTOM_PHRASES_FILENAME if agent_dir is not None else _custom_phrases_path
    if path is None or not path.is_file():
        return {"date": "", "phrases": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(f"custom loading phrases unreadable: {path}")
        return {"date": "", "phrases": []}
    phrases_raw = raw.get("phrases") if isinstance(raw, dict) else None
    if not isinstance(phrases_raw, list):
        return {"date": "", "phrases": []}
    day = str(raw.get("date") or "").strip()
    phrases = [str(p).strip() for p in phrases_raw if str(p).strip()]
    if not day or day != time.strftime("%Y-%m-%d"):
        return {"date": day, "phrases": []}
    return {"date": day, "phrases": phrases}


def _load_custom_pool() -> tuple[str, ...]:
    return tuple(custom_payload().get("phrases", []))


def _sample_batch() -> list[str]:
    """整池洗牌袋：常驻池全量 + 当日定制 + 使用提示，打乱后顺序播放。

    池子多大，一轮就多长（常驻池约 365 条 → 每小时 60 条 → 六个多小时才穷尽），
    且顺序是打乱的——展现在用户面前的就是随机序列，而不是「同一小批反复循环」。
    """
    batch = list(WITTY_POOL) + list(QUOTE_POOL) + list(_load_custom_pool()) + list(TIPS)
    random.shuffle(batch)
    return batch


# 进程级批次：启动即抽一批；/new、切空间时 reshuffle 换一批
_BATCH: list[str] = _sample_batch()


def reshuffle() -> list[str]:
    """重新抽样一批并打乱（/new、workspace_switched、session_started 时调用）."""
    global _BATCH
    _BATCH = _sample_batch()
    return _BATCH


def current_batch() -> tuple[str, ...]:
    return tuple(_BATCH)


def current_phrase(now: float | None = None, *, offset: int = 0) -> str:
    """按 monotonic 时钟每 _ROTATE_SECONDS 秒轮换一条（无状态，可测试）."""
    t = time.monotonic() if now is None else now
    return _BATCH[(offset + int(t // _ROTATE_SECONDS)) % len(_BATCH)]
