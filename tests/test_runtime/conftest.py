"""Shared fixtures for runtime spill tests."""

from __future__ import annotations

from src.core.types import ToolOutputStoreConfig


def make_tool_output_settings(**overrides: object) -> ToolOutputStoreConfig:
    base = {
        "enabled": True,
        "spill_threshold_bytes": 25_000,
        "batch_budget_bytes": 200_000,
        "preview_head_chars": 2_000,
        "preview_tail_chars": 8_000,
        "tool_thresholds": {},
        "retention_days": 30,
    }
    base.update(overrides)
    return ToolOutputStoreConfig.model_validate(base)
