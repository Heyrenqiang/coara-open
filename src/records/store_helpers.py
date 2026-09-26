"""records 域共享的存储小工具——agent / user 两侧 store 的唯一实现。

此前 ``agent_store.py`` 与 ``user_store.py`` 各自逐字维护一份；改口径只许改这里。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def now_local() -> datetime:
    """当前本地时间（aware）。"""
    return datetime.now(UTC).astimezone()


def now_iso() -> str:
    """本地时间 ISO 串（秒级）。"""
    return now_local().isoformat(timespec="seconds")


def normalize_for_hash(text: str) -> str:
    """Normalize content before hashing (trim, lower, strip punctuation noise)."""
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w一-鿿\s]", "", s, flags=re.UNICODE)
    return s


def content_hash(text: str) -> str:
    digest = hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def generate_record_id(prefix: str, when: datetime | None = None) -> str:
    """``{prefix}_{YYYYMMDD_HHMMSS}_{6位随机}``（mem_ / col_ 等）。"""
    ts = (when or now_local()).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}"


def slugify(title: str, fallback: str) -> str:
    """标题 → 文件名 slug（保留 CJK，空则回退 fallback，最长 80）。"""
    raw = (title or "").strip().lower()
    raw = re.sub(r"[^\w一-鿿\-]+", "_", raw, flags=re.UNICODE)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw:
        raw = fallback
    return raw[:80]


def parse_dt(val: Any) -> datetime | None:
    """宽容解析 frontmatter 元数据时间：None/空串/已解析对象/ISO 串。"""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except Exception:
        return None
