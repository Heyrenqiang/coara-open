"""Context compression prompt loader (``src/coara/prompts/injections/compression.md``).

提示词资产组织：agents/ 为 agent 配置+提示词，injections/ 为注入提示词
（janitor.md、compression.md 等同性质的注入提醒内容）。context 域经路径
读取，不产生 import 依赖。
"""

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
