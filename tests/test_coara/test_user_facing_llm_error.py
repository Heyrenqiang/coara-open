"""用户可见 LLM/回合错误文案映射。"""

from __future__ import annotations

from src.coara.turn_orchestrator import _user_facing_llm_error, _user_facing_turn_error
from src.core.errors import LLMError


def test_usage_limit_403_maps_to_quota_guidance() -> None:
    exc = LLMError(
        "Error code: 403 - {'error': {'message': \"You've reached your 5-hour usage limit. "
        'Your quota will reset...", \'type\': \'access_terminated_error\'}}'
    )
    msg = _user_facing_llm_error(exc)
    assert msg.startswith("Error:")
    assert "额度" in msg
    assert "权限" not in msg  # 不是笼统 403 密钥权限文案


def test_generic_403_still_permission_guidance() -> None:
    msg = _user_facing_llm_error(LLMError("Error code: 403 - Forbidden: model not enabled"))
    assert "访问被拒绝" in msg or "权限" in msg


def test_turn_error_bare_error_gets_friendly_fallback() -> None:
    msg = _user_facing_turn_error(RuntimeError("error"))
    assert msg.startswith("Error:")
    assert msg.lower() != "error: error"
