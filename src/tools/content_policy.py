"""Content policy helpers: domain blocklist, external tool-result stripping, error detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.core.config import config_manager
from src.core.types import Message, MessageRole
from src.tools.builtin.web.fetch_policy import domain_from_url

EXTERNAL_CONTENT_PREFIX = "[External content from"
REMOVED_PLACEHOLDER = (
    "[Content removed: provider content policy. Use other sources, web_search snippets, or start a new session (/new).]"
)

_DEFAULT_BLOCK_DOMAINS = ("ntdtv.com",)
_POLICY_CODE_RE = re.compile(r"\b(1026|1027)\b")


@dataclass(slots=True)
class ContentPolicyConfig:
    enabled: bool = True
    block_domains: tuple[str, ...] = _DEFAULT_BLOCK_DOMAINS
    llm_recovery_enabled: bool = True
    llm_recovery_providers: tuple[str, ...] = ("minimax",)
    max_llm_recovery_strips: int = 2


def load_content_policy_config(raw_config: dict[str, Any] | None = None) -> ContentPolicyConfig:
    raw = (
        raw_config
        if raw_config is not None
        else (config_manager.get_raw_config() if config_manager._config is not None else {})
    )
    section = (raw or {}).get("content_policy") or {}

    domains = section.get("block_domains")
    if isinstance(domains, list):
        block_domains = tuple(str(d).strip().lower().lstrip(".") for d in domains if str(d).strip())
    else:
        block_domains = _DEFAULT_BLOCK_DOMAINS

    providers = section.get("llm_recovery_providers")
    if isinstance(providers, list) and providers:
        llm_providers = tuple(str(p).strip().lower() for p in providers if str(p).strip())
    else:
        llm_providers = ("minimax",)

    return ContentPolicyConfig(
        enabled=bool(section.get("enabled", True)),
        block_domains=block_domains,
        llm_recovery_enabled=bool(section.get("llm_recovery_enabled", True)),
        llm_recovery_providers=llm_providers,
        max_llm_recovery_strips=max(0, int(section.get("max_llm_recovery_strips", 2) or 2)),
    )


def url_blocked(url: str, policy: ContentPolicyConfig | None = None) -> str | None:
    """Return matched block domain, or None when fetch/search is allowed."""
    cfg = policy or load_content_policy_config()
    if not cfg.enabled or not cfg.block_domains:
        return None
    registrable = domain_from_url(url)
    if not registrable:
        return None
    for blocked in cfg.block_domains:
        needle = blocked.lower().lstrip(".")
        if needle and (registrable == needle or registrable.endswith(f".{needle}")):
            return needle
    return None


def filter_search_results(
    results: list[dict[str, Any]],
    policy: ContentPolicyConfig | None = None,
) -> tuple[list[dict[str, Any]], int]:
    cfg = policy or load_content_policy_config()
    if not cfg.enabled or not results or not cfg.block_domains:
        return results, 0
    kept = [item for item in results if not url_blocked(str(item.get("url", "") or "").strip(), cfg)]
    return kept, len(results) - len(kept)


def is_external_tool_content(content: str | list[Any] | None) -> bool:
    if not isinstance(content, str):
        return False
    if content.startswith(REMOVED_PLACEHOLDER):
        return False
    return content.startswith(EXTERNAL_CONTENT_PREFIX)


def strip_external_tool_results(
    messages: list[Message],
    *,
    max_strips: int = 1,
) -> tuple[list[Message], int, list[str]]:
    """Replace up to ``max_strips`` newest wrapped external tool_result bodies (LIFO)."""
    if max_strips <= 0:
        return list(messages), 0, []
    out = list(messages)
    stripped = 0
    stripped_ids: list[str] = []
    for index in range(len(out) - 1, -1, -1):
        if stripped >= max_strips:
            break
        msg = out[index]
        if msg.role != MessageRole.TOOL_RESULT or not is_external_tool_content(msg.content):
            continue
        out[index] = msg.model_copy(update={"content": REMOVED_PLACEHOLDER})
        stripped += 1
        if msg.tool_call_id:
            stripped_ids.append(msg.tool_call_id)
    return out, stripped, stripped_ids


def apply_tool_result_removals(messages: list[Message], tool_call_ids: list[str]) -> list[Message]:
    """Mirror strip placeholders onto full history via ``tool_call_id``."""
    if not tool_call_ids:
        return list(messages)
    targets = set(tool_call_ids)
    return [
        msg.model_copy(update={"content": REMOVED_PLACEHOLDER})
        if msg.role == MessageRole.TOOL_RESULT and msg.tool_call_id in targets
        else msg
        for msg in messages
    ]


def is_provider_content_policy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if "new_sensitive" in text or "(1026)" in text or "(1027)" in text:
        return True
    if _POLICY_CODE_RE.search(text) and "sensitive" in text:
        return True
    cause = exc.__cause__
    return cause is not None and cause is not exc and is_provider_content_policy_error(cause)


def should_attempt_llm_recovery(provider_name: str, policy: ContentPolicyConfig | None = None) -> bool:
    cfg = policy or load_content_policy_config()
    if not cfg.enabled or not cfg.llm_recovery_enabled:
        return False
    return provider_name.lower() in {p.lower() for p in cfg.llm_recovery_providers}
