"""Token counting helpers for context window budgeting.

Hot path (compress / guard) uses a calibrated character heuristic — LLM APIs
already return real usage, and proactive compress + provider-overflow retry are
the safety nets.
"""

from __future__ import annotations

# DeepSeek V3 ratios:
#   1 English char ≈ 0.3 tokens  →  3.33 chars / token
#   1 Chinese  char ≈ 0.6 tokens  →  1.67 chars / token

_EN_CHARS_PER_TOKEN = 3.33
_ZH_TOKENS_PER_CHAR = 0.6


def _estimate_tokens_heuristic(text: str) -> int:
    if not text:
        return 0
    ascii_chars = len(text.encode("ascii", "ignore"))
    non_ascii_chars = len(text) - ascii_chars
    return max(int(ascii_chars / _EN_CHARS_PER_TOKEN + non_ascii_chars * _ZH_TOKENS_PER_CHAR), 1)


def _prepare_text(text: object) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # Lone UTF-16 surrogates (common after Windows/console garble) are valid in
    # Python ``str`` but rejected by UTF-8 JSON writers.
    from src.utils.text_utils import sanitize_surrogates

    return sanitize_surrogates(text)


def estimate_tokens(text: str) -> int:
    """Fast budget estimate (character heuristic). Prefer provider-reported usage."""
    prepared = _prepare_text(text)
    if not prepared:
        return 0
    return _estimate_tokens_heuristic(prepared)
