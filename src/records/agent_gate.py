"""Write gate — reject secrets; require provenance for remember."""

from __future__ import annotations

import re

from src.records.agent_types import GateResult, MemorySource, Sensitivity, SourceType

SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"sk-ant-[a-zA-Z0-9-]{20,}", re.IGNORECASE),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-._~+/]+=*"),
]


def contains_secret(content: str) -> bool:
    """Return True if content looks like it contains secrets."""
    text = content or ""
    return any(p.search(text) for p in SECRET_PATTERNS)


def classify_sensitivity(content: str) -> Sensitivity:
    """Heuristic sensitivity tag (MVP: secret rejected earlier; else internal)."""
    lower = (content or "").lower()
    if any(k in lower for k in ("私人", "隐私", "personal", "private")):
        return "personal"
    return "internal"


def memory_gate(
    content: str,
    *,
    source: MemorySource | None = None,
    source_type: SourceType = "explicit",
) -> GateResult:
    """Five-step write gate (MVP: secret reject + provenance required).

    LLM 「该不该记」评分在 MVP 对显式 remember 跳过，避免额外 LLM 调用。
    """
    text = (content or "").strip()
    if not text:
        return GateResult(allowed=False, reason="内容为空，拒绝记录")

    if contains_secret(text):
        return GateResult(allowed=False, reason="包含敏感信息，拒绝记录")

    if source is None or not str(source.session_id or "").strip():
        return GateResult(allowed=False, reason="缺少来源信息，拒绝记录")

    return GateResult(
        allowed=True,
        source_type=source_type,
        sensitivity=classify_sensitivity(text),
    )
