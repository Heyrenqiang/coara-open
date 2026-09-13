"""Per-tool spill thresholds and preview shaping (Qwen-aligned layered budgets).

See ``docs/TOOL_OUTPUT_FRAMEWORK.md`` §Spill policy.
"""

from __future__ import annotations

from enum import StrEnum

from src.core.types import ToolOutputStoreConfig


class SpillKeep(StrEnum):
    """Which portion of oversized output to emphasize in the model-facing preview."""

    HEAD = "head"
    TAIL = "tail"
    BOTH = "both"


# Tools that bound output at execute() — skip per-tool spill unless batch budget forces it.
SELF_MANAGED_TOOLS = frozenset(
    {
        "read",
        "write",
        "edit",
        "delete",
        "web_fetch",
        "web_search",
    }
)

# Default per-tool byte thresholds (UTF-8). None threshold = self-managed / exempt from per-tool spill.
_DEFAULT_TOOL_THRESHOLDS: dict[str, tuple[int | None, SpillKeep]] = {
    "grep": (20_000, SpillKeep.BOTH),
    "glob": (20_000, SpillKeep.BOTH),
    "shell": (30_000, SpillKeep.TAIL),
    "delegate": (32_000, SpillKeep.TAIL),
}


def resolve_spill_policy(
    tool_name: str,
    settings: ToolOutputStoreConfig,
    *,
    tool_category: str | None = None,
) -> tuple[int | None, SpillKeep]:
    """Return (threshold_bytes, preview_keep). ``None`` threshold = no per-tool spill."""
    if tool_name in SELF_MANAGED_TOOLS:
        return None, SpillKeep.BOTH

    override = (settings.tool_thresholds or {}).get(tool_name)
    if override is not None:
        keep = _DEFAULT_TOOL_THRESHOLDS.get(tool_name, (settings.spill_threshold_bytes, SpillKeep.BOTH))[1]
        return int(override), keep

    if tool_name in _DEFAULT_TOOL_THRESHOLDS:
        threshold, keep = _DEFAULT_TOOL_THRESHOLDS[tool_name]
        return threshold, keep

    return settings.spill_threshold_bytes, SpillKeep.BOTH


def build_spill_preview(
    content: str,
    *,
    head_chars: int,
    tail_chars: int,
    keep: SpillKeep,
) -> str:
    """Build model-facing preview snippet before/after full spill to disk."""
    if not content:
        return ""
    if keep == SpillKeep.HEAD:
        if len(content) <= head_chars:
            return content
        return content[:head_chars] + "\n…"
    if keep == SpillKeep.TAIL:
        if len(content) <= tail_chars:
            return content
        return "…\n" + content[-tail_chars:]
    # BOTH — mirror Qwen head/tail with explicit marker
    if len(content) <= head_chars + tail_chars + 40:
        return content
    return (
        content[:head_chars]
        + "\n\n--- [PREVIEW TRUNCATED — use read(ref=…) for full body] ---\n\n"
        + content[-tail_chars:]
    )
