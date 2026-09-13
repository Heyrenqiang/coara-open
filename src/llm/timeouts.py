"""LLM 请求超时预算（config.yaml ``llm.*`` 可调）.

- ``request_timeout_seconds``：单次请求的无响应读超时（默认 300s）。语义是
  「两次 socket 读之间」的最长间隔，流式每收到一个分片即重置——挡不住滴漏式响应。
- ``total_timeout_seconds``：单次调用全程硬上限（默认 300s，含连接、首字节与
  流式消费/重试全程）。超过即以 LLMError 抛回回合并 abort 卡死的连接，
  界面上能看到明确的超时失败而不是无限等待。
"""

from __future__ import annotations

_READ_DEFAULT = 300.0
_TOTAL_DEFAULT = 300.0
_READ_MIN = 30.0
_TOTAL_MIN = 60.0


def _configured(key: str) -> float | None:
    try:
        from src.core.config import config_manager

        raw = config_manager.get_raw_config() or {}
        value = (raw.get("llm") or {}).get(key)
        if value is not None:
            return float(value)
    except Exception:
        pass
    return None


def llm_request_timeout_seconds() -> float:
    """单次请求读超时（无响应判定），默认 300s."""
    value = _configured("request_timeout_seconds")
    return max(_READ_MIN, value) if value is not None else _READ_DEFAULT


def llm_total_timeout_seconds() -> float:
    """含重试的总预算，默认 300s（5 分钟硬上限）."""
    value = _configured("total_timeout_seconds")
    return max(_TOTAL_MIN, value) if value is not None else _TOTAL_DEFAULT
