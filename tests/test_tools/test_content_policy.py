from __future__ import annotations

from src.core.errors import LLMError
from src.core.types import Message, MessageRole
from src.tools.content_policy import (
    REMOVED_PLACEHOLDER,
    apply_tool_result_removals,
    filter_search_results,
    is_external_tool_content,
    is_provider_content_policy_error,
    load_content_policy_config,
    should_attempt_llm_recovery,
    strip_external_tool_results,
    url_blocked,
)
from src.tools.security import wrap_external_content


def test_url_blocked_respects_config() -> None:
    blocked = load_content_policy_config({"content_policy": {"block_domains": ["ntdtv.com"]}})
    assert url_blocked("https://www.ntdtv.com/gb/foo", blocked) == "ntdtv.com"
    assert url_blocked("https://example.com/foo", blocked) is None

    open_cfg = load_content_policy_config({"content_policy": {"block_domains": []}})
    assert url_blocked("https://www.ntdtv.com/x", open_cfg) is None


def test_filter_search_results_drops_blocked_urls() -> None:
    kept, blocked = filter_search_results(
        [{"url": "https://example.com/a"}, {"url": "https://www.ntdtv.com/gb/x"}],
    )
    assert blocked == 1 and kept[0]["url"].endswith("example.com/a")


def test_strip_external_tool_results_lifo() -> None:
    messages = [
        Message(
            role=MessageRole.TOOL_RESULT,
            content=wrap_external_content("body", "https://example.com"),
            tool_call_id="t1",
        ),
        Message(role=MessageRole.TOOL_RESULT, content="plain", tool_call_id="t2"),
        Message(
            role=MessageRole.TOOL_RESULT, content=wrap_external_content("newer", "https://b.com"), tool_call_id="t3"
        ),
    ]
    stripped_msgs, count, ids = strip_external_tool_results(messages, max_strips=1)
    assert count == 1 and ids == ["t3"]
    assert stripped_msgs[-1].content == REMOVED_PLACEHOLDER
    assert is_external_tool_content(stripped_msgs[-3].content)


def test_apply_tool_result_removals_syncs_history() -> None:
    history = [
        Message(role=MessageRole.TOOL_RESULT, content=wrap_external_content("x", "https://a.com"), tool_call_id="c1"),
    ]
    assert apply_tool_result_removals(history, ["c1"])[0].content == REMOVED_PLACEHOLDER


def test_is_provider_content_policy_error() -> None:
    assert is_provider_content_policy_error(LLMError("input new_sensitive (1026)"))
    wrapped = LLMError("API error")
    wrapped.__cause__ = LLMError("input new_sensitive (1026)")
    assert is_provider_content_policy_error(wrapped)
    assert not is_provider_content_policy_error(LLMError("HTTP 500 internal error"))


def test_should_attempt_llm_recovery_respects_provider() -> None:
    policy = load_content_policy_config({"content_policy": {"llm_recovery_providers": ["minimax"]}})
    assert should_attempt_llm_recovery("minimax", policy)
    assert not should_attempt_llm_recovery("xiaomi", policy)
