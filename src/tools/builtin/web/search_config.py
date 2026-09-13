"""web_search runtime config (auto-mode RRF freshness weight)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.config import config_manager

DEFAULT_FRESHNESS_RRF_WEIGHT = 0.08


@dataclass(slots=True)
class WebSearchConfig:
    freshness_rrf_weight: float = DEFAULT_FRESHNESS_RRF_WEIGHT


def load_web_search_config(raw_config: dict[str, Any] | None = None) -> WebSearchConfig:
    raw = raw_config
    if raw is None and config_manager._config is not None:
        raw = getattr(config_manager, "_raw_config", {}) or {}
    section = (raw or {}).get("web_search") or {}
    weight = float(section.get("freshness_rrf_weight", DEFAULT_FRESHNESS_RRF_WEIGHT))
    if weight < 0:
        weight = 0.0
    return WebSearchConfig(freshness_rrf_weight=weight)
