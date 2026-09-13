"""Turn-level retry when an LLM provider rejects input for content moderation (e.g. MiniMax 1026)."""

from __future__ import annotations

from typing import Any

from src.core.errors import LLMError, ProviderContentPolicyError
from src.core.types import Message
from src.tools.content_policy import (
    apply_tool_result_removals,
    is_provider_content_policy_error,
    load_content_policy_config,
    should_attempt_llm_recovery,
    strip_external_tool_results,
)


async def complete_turn_with_content_policy_recovery(
    coara,
    system_prompt: str,
    turn_messages: list[Message],
    signal,
    *,
    llm_input_summary: dict[str, Any] | None = None,
):
    cfg = load_content_policy_config()
    provider_name = getattr(getattr(coara, "provider", None), "name", "") or ""
    strips_total = 0
    messages = turn_messages

    while True:
        try:
            return await coara._complete_turn(system_prompt, messages, signal)
        except LLMError as exc:
            if not is_provider_content_policy_error(exc):
                raise
            if not should_attempt_llm_recovery(provider_name, cfg):
                raise ProviderContentPolicyError(str(exc)) from exc
            if strips_total >= cfg.max_llm_recovery_strips:
                raise ProviderContentPolicyError(
                    "Provider content policy blocked this turn after removing external tool results. "
                    "Try /new, switch provider, or ask for a shorter answer without web_fetch."
                ) from exc

            messages, stripped, stripped_ids = strip_external_tool_results(messages, max_strips=1)
            if stripped == 0:
                raise ProviderContentPolicyError(str(exc)) from exc

            coara.message_history = apply_tool_result_removals(coara.message_history, stripped_ids)
            strips_total += stripped
            coara._emit_trace(
                "content_policy_recovery",
                f"Stripped {stripped} external tool result(s) after provider content policy error; retrying",
                level="warning",
                payload={
                    "strips_total": strips_total,
                    "provider": provider_name,
                    "stripped_tool_call_ids": stripped_ids,
                    "llm_input": llm_input_summary,
                    "error": str(exc)[:500],
                },
            )
