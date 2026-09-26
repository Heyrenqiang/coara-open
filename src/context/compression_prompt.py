"""Context compression prompt loader (``src/coara/prompts/injections/compression.md``)."""

from __future__ import annotations

from pathlib import Path

_COMPRESSION_PROMPT_CACHE: str | None = None


def get_compression_prompt() -> str:
    """Load the system prompt used when compressing conversation history."""
    global _COMPRESSION_PROMPT_CACHE
    if _COMPRESSION_PROMPT_CACHE is not None:
        return _COMPRESSION_PROMPT_CACHE
    md_path = Path(__file__).resolve().parent.parent / "coara" / "prompts" / "injections" / "compression.md"
    md_content = md_path.read_text(encoding="utf-8").strip() if md_path.exists() else ""
    if not md_content:
        raise FileNotFoundError("Missing compression prompt: src/coara/prompts/injections/compression.md")
    _COMPRESSION_PROMPT_CACHE = md_content
    return md_content
