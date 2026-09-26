"""LLM 请求超时预算（config.yaml ``llm.*`` 可调）.

- ``request_timeout_seconds``：单次请求的无响应读超时（默认 300s）。语义是
  「两次 socket 读之间」的最长间隔，流式每收到一个分片即重置——挡不住滴漏式响应。
- ``total_timeout_seconds``：单次调用全程硬上限（默认 1200s，含连接、首字节与
  流式消费/重试全程）。超过即以 LLMError 抛回回合并 abort 卡死的连接，
  界面上能看到明确的超时失败而不是无限等待。
- ``stream_idle_timeout_seconds``：流式空闲看门狗（默认 300s）。流式消费期间
  每收到一帧（正文/思考/工具调用/usage 任一）计时归零重数；连续超时才判流死。
  长思考回合的静默期不再被总预算误杀——这是主判据，总预算只兜底半开连接。
"""

from __future__ import annotations

_READ_DEFAULT = 300.0
_TOTAL_DEFAULT = 1200.0
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
        pass  # 配置读取回落：有意静默用内置默认（热路径，不记日志）
    return None


def llm_request_timeout_seconds() -> float:
    """单次请求读超时（无响应判定），默认 300s."""
    value = _configured("request_timeout_seconds")
    return max(_READ_MIN, value) if value is not None else _READ_DEFAULT


def llm_total_timeout_seconds() -> float:
    """含重试的总预算，默认 1200s（20 分钟硬上限，只兜底半开连接死等）."""
    value = _configured("total_timeout_seconds")
    return max(_TOTAL_MIN, value) if value is not None else _TOTAL_DEFAULT


_STREAM_IDLE_DEFAULT = 300.0
_STREAM_IDLE_MIN = 30.0


def llm_stream_idle_timeout_seconds() -> float:
    """流式空闲看门狗：两帧之间的最长间隔，默认 300s.

    流式消费的主超时判据——每收到一帧计时归零；连续超时才判流死。
    与 request_timeout_seconds 的区别：后者是 SDK socket 读超时（连接层），
    本项是采集层语义超时（整帧聚合后仍有意义，如服务端慢速聚合转发）。
    """
    value = _configured("stream_idle_timeout_seconds")
    return max(_STREAM_IDLE_MIN, value) if value is not None else _STREAM_IDLE_DEFAULT
