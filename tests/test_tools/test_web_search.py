"""Tests for web_search ranking and config."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.tool_base import ToolResult
from src.tools.builtin.web.raw_tool import WebSearchRawTool
from src.tools.builtin.web.search_config import DEFAULT_FRESHNESS_RRF_WEIGHT, load_web_search_config
from src.tools.builtin.web.strategy_tool import WebSearchTool


def test_load_web_search_config_default() -> None:
    cfg = load_web_search_config({})
    assert cfg.freshness_rrf_weight == DEFAULT_FRESHNESS_RRF_WEIGHT


def test_load_web_search_config_custom_weight() -> None:
    cfg = load_web_search_config({"web_search": {"freshness_rrf_weight": 0.2}})
    assert cfg.freshness_rrf_weight == 0.2


def test_combined_rank_score() -> None:
    tool = WebSearchRawTool()
    tool._freshness_rrf_weight = 0.1
    assert tool._combined_rank_score(0.5, 1.0) == 0.6


def test_merge_rank_score_prefers_rrf_combined() -> None:
    tool = WebSearchRawTool()
    tool._freshness_rrf_weight = 0.1
    meta = {
        "rrf_scores": {"https://a.example": 0.5},
        "freshness_scores": {"https://a.example": 1.0},
    }
    assert tool._merge_rank_score(meta, "https://a.example") == 0.6


def test_merge_rank_score_falls_back_to_freshness() -> None:
    tool = WebSearchRawTool()
    meta = {"freshness_scores": {"https://b.example": 0.75}}
    assert tool._merge_rank_score(meta, "https://b.example") == 0.75


@pytest.mark.asyncio
async def test_execute_search_multi_requires_multiple_queries() -> None:
    tool = WebSearchRawTool()
    out = await tool._execute_search_multi(["only one"])
    assert out.is_error
    assert "at least 2 queries" in out.content


@pytest.mark.asyncio
async def test_strategy_single_round_preserves_raw_order() -> None:
    ordered_results = [
        {"title": "High RRF", "url": "https://high.example", "description": "a"},
        {"title": "Low RRF", "url": "https://low.example", "description": "b"},
    ]
    raw_tool = WebSearchRawTool()
    raw_tool._execute_search = AsyncMock(  # type: ignore[method-assign]
        return_value=ToolResult.success(
            content="formatted",
            metadata={
                "results": ordered_results,
                "count": 2,
                "rrf_scores": {"https://high.example": 0.9, "https://low.example": 0.1},
                "freshness_scores": {"https://high.example": 0.1, "https://low.example": 0.9},
            },
        )
    )
    tool = WebSearchTool(raw_search_tool=raw_tool)

    out = await tool._execute_search("user query")

    assert [r["url"] for r in out.metadata.get("results") or []] == [
        "https://high.example",
        "https://low.example",
    ]


@pytest.mark.asyncio
async def test_execute_search_multi_merges_by_rank_not_freshness_only() -> None:
    tool = WebSearchRawTool()
    res_a = ToolResult.success(
        content="a",
        metadata={
            "results": [{"title": "A", "url": "https://a.example", "description": ""}],
            "rrf_scores": {"https://a.example": 0.8},
            "freshness_scores": {"https://a.example": 0.2},
        },
    )
    res_b = ToolResult.success(
        content="b",
        metadata={
            "results": [{"title": "B", "url": "https://b.example", "description": ""}],
            "rrf_scores": {"https://b.example": 0.3},
            "freshness_scores": {"https://b.example": 0.95},
        },
    )
    tool._execute_search = AsyncMock(side_effect=[res_a, res_b])  # type: ignore[method-assign]
    tool._freshness_rrf_weight = 0.08

    out = await tool._execute_search_multi(["q1", "q2"])

    urls = [r["url"] for r in out.metadata.get("results") or []]
    assert urls == ["https://a.example", "https://b.example"]
