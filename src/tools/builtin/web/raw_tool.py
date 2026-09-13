"""Raw web search tool with multi-provider execution."""

from __future__ import annotations

import asyncio
import contextlib
import html
import os
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.tools.content_policy import filter_search_results
from src.tools.security import wrap_external_content

from .date_extractor import DateExtractor
from .search_config import load_web_search_config

ProviderHandler = Callable[[str, int], Awaitable[list[dict[str, str]]]]

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_HTTP_CLIENT_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)

_RECENCY_STRIP_PATTERN = re.compile(
    r"\b(?:latest|current|recent|news|update|updates)\b|最新|最近|当前|动态|消息|新闻",
    re.IGNORECASE,
)
_YEAR_TOKEN = re.compile(r"(?<!\d)20\d{2}(?!\d)")


def _provider_outcome(name: str, status: str, *, count: int = 0, error: str = "") -> dict[str, Any]:
    outcome: dict[str, Any] = {"name": name, "status": status}
    if status == "ok":
        outcome["count"] = count
    elif status == "error" and error:
        outcome["error"] = error[:160]
    return outcome


def _exc_detail(exc: BaseException) -> str:
    """Format an exception for logs; httpx timeout classes carry an empty str."""
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class WebSearchRawToolInvocation(ToolInvocation):
    """Invocation for web search."""

    def __init__(self, params: dict[str, Any], tool: WebSearchRawTool):
        super().__init__(params)
        self.query = tool.parse_query(params)
        if not self.query:
            raise ValueError("Missing required parameter: query")
        self._tool = tool

    def get_description(self) -> str:
        preview = self.query
        if len(preview) > 40:
            preview = preview[:40] + "…"
        return f"Search: {preview}"

    async def execute(self, signal=None) -> ToolResult:
        return await self._tool._execute_search(self.query, signal=signal)


class WebSearchRawTool(BaseTool):
    """Search the web using weighted multi-provider selection."""

    name = "web_search_raw"
    description = """内部底层网页搜索：单轮 query，多 provider 融合返回标题、URL 与摘要"""
    display_name = "WebSearchRaw"
    category = "web"
    kind = ToolKind.FETCH
    DEFAULT_COUNT = 5
    DEFAULT_TIME_WINDOW_DAYS = 30
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "搜索关键词：尽可能多且有辨识度的关键词组合"
                    "涉及时效、最新、当前动态等意图时，必须以当前日期为基准构造 query"
                ),
            },
        },
        "required": ["query"],
    }

    PROVIDER_WEIGHTS: dict[str, float] = {
        "exa": 14.39,
        "serper": 12.28,
        "linkup": 8.5,
        "baidu": 6.5,
        "doubao": 10.0,
    }
    PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
        "exa": ("EXA_API_KEY",),
        "serper": ("SERPER_API_KEY",),
        "linkup": ("LINKUP_API_KEY",),
        "doubao": ("DOUBAO_API_KEY",),
    }

    def __init__(self, api_key: str | None = None, provider: str = "auto", rng: random.Random | None = None):
        super().__init__()
        self._api_key = api_key
        self._provider = provider
        self._rng = rng or random.Random()  # noqa: S311
        self._date_extractor = DateExtractor()
        self._http_client: httpx.AsyncClient | None = None
        self._freshness_rrf_weight = load_web_search_config().freshness_rrf_weight

    def create_invocation(self, params: dict[str, Any]) -> WebSearchRawToolInvocation:
        return WebSearchRawToolInvocation(params, self)

    def _combined_rank_score(self, rrf_score: float, freshness_score: float) -> float:
        return rrf_score + self._freshness_rrf_weight * freshness_score

    def _merge_rank_score(self, meta: dict[str, Any], url: str) -> float:
        rrf_scores = meta.get("rrf_scores") or {}
        freshness_scores = meta.get("freshness_scores") or {}
        if url in rrf_scores:
            return self._combined_rank_score(float(rrf_scores[url]), float(freshness_scores.get(url, 0.0)))
        if url in freshness_scores:
            return float(freshness_scores[url])
        return 0.0

    @staticmethod
    def parse_query(params: dict[str, Any]) -> str:
        raw = params.get("query", "")
        if isinstance(raw, list) and raw:
            raw = raw[0]
        query = str(raw).strip()
        return query

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=20.0,
                headers={"User-Agent": _BROWSER_USER_AGENT},
                limits=_HTTP_CLIENT_LIMITS,
            )
        return self._http_client

    async def _reset_http_client(self) -> None:
        """Drop the cached client so the next search starts with a fresh transport.

        A client whose transport got into a bad state (e.g. interrupted during a
        network outage) would otherwise fail every subsequent search for the
        lifetime of the process.
        """
        client, self._http_client = self._http_client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _execute_search(
        self,
        query: str,
        signal=None,
    ) -> ToolResult:
        count = self.DEFAULT_COUNT
        requested_provider = (self._provider or "auto").strip().lower() or "auto"

        if requested_provider in {"", "random"}:
            requested_provider = "auto"

        if requested_provider == "auto":
            return await self._execute_multi_search(query, count)

        weighted_order = self._weighted_provider_order()
        provider_order = self._build_provider_order(requested_provider, weighted_order)

        if not provider_order:
            return ToolResult.error(self._missing_provider_error())

        attempts: list[str] = []
        last_error: str | None = None
        provider_outcomes: list[dict[str, Any]] = []

        for provider_name in provider_order:
            handler = self._get_provider_handler(provider_name)
            if handler is None:
                logger.warning(f"Unknown web search provider requested: {provider_name}")
                continue

            attempts.append(provider_name)
            try:
                results = await self._execute_with_query_variants(
                    handler,
                    provider_name,
                    query,
                    count,
                )
            except Exception as exc:
                last_error = f"{provider_name}: {_exc_detail(exc)}"
                logger.warning(f"Web search provider '{provider_name}' failed: {_exc_detail(exc)}")
                provider_outcomes.append(_provider_outcome(provider_name, "error", error=_exc_detail(exc)))
                await self._reset_http_client()
                continue

            cleaned_results = filter_search_results(
                self._sort_single_provider_results(
                    results,
                    count=count,
                    time_window_days=self.DEFAULT_TIME_WINDOW_DAYS,
                )
            )[0]
            if cleaned_results:
                all_variants = self._query_variants(query)
                provider_outcomes.append(_provider_outcome(provider_name, "ok", count=len(cleaned_results)))
                return ToolResult.success(
                    content=self._format_results(cleaned_results),
                    metadata={
                        "query": query,
                        "variants": all_variants,
                        "results": cleaned_results,
                        "count": len(cleaned_results),
                        "provider_outcomes": provider_outcomes,
                        "attempts": attempts,
                        "freshness_scores": {
                            result["url"]: round(DateExtractor.freshness_score(result.get("date_iso", "")), 4)
                            for result in cleaned_results
                            if result.get("url")
                        },
                    },
                )

            last_error = f"{provider_name}: no results"
            provider_outcomes.append(_provider_outcome(provider_name, "empty"))

        if last_error:
            return ToolResult.error(f"Search failed: {last_error}")
        all_variants = self._query_variants(query)
        return ToolResult.success(
            content="No results found for the search query.",
            metadata={
                "query": query,
                "variants": all_variants,
                "results": [],
                "count": 0,
                "provider_outcomes": provider_outcomes,
                "attempts": attempts,
            },
        )

    async def _execute_search_multi(
        self,
        queries: list[str],
        signal=None,
    ) -> ToolResult:
        """Merge results from 2+ queries (concurrent). Single-query callers use _execute_search."""
        if len(queries) < 2:
            return ToolResult.error("Search failed: _execute_search_multi requires at least 2 queries")

        count = self.DEFAULT_COUNT
        all_results: list[dict[str, Any]] = []
        all_rank_scores: dict[str, float] = {}
        all_freshness: dict[str, float] = {}
        seen_urls: set[str] = set()
        merged_outcomes: list[dict[str, Any]] = []

        coros = [self._execute_search(q, signal=signal) for q in queries]
        search_results = await asyncio.gather(*coros, return_exceptions=True)

        for idx, res in enumerate(search_results):
            if isinstance(res, BaseException):
                logger.warning(f"Search query failed: {res}")
                continue
            query_label = queries[idx]
            meta = res.metadata or {}
            for item in meta.get("provider_outcomes") or []:
                if isinstance(item, dict):
                    merged_outcomes.append(dict(item))
            results = list(meta.get("results") or [])
            freshness_scores = dict(meta.get("freshness_scores") or {})
            for item in results:
                url = str(item.get("url", "")).strip()
                if not url or url == "N/A" or url in seen_urls:
                    continue
                seen_urls.add(url)
                item["_source_query"] = query_label
                all_results.append(item)
                rank_score = self._merge_rank_score(meta, url)
                all_rank_scores[url] = max(all_rank_scores.get(url, 0.0), rank_score)
                if url in freshness_scores:
                    all_freshness[url] = freshness_scores[url]

        if not all_results:
            return ToolResult.success(
                content="No results found for the search queries.",
                metadata={
                    "queries": queries,
                    "results": [],
                    "count": 0,
                    "provider_outcomes": merged_outcomes,
                },
            )

        sorted_results = sorted(
            enumerate(all_results),
            key=lambda pair: (-all_rank_scores.get(pair[1].get("url", ""), 0.0), pair[0]),
        )[:count]
        cleaned = filter_search_results(self._sanitize_results([item for _, item in sorted_results], count))[0]

        return ToolResult.success(
            content=self._format_results_multi(cleaned),
            metadata={
                "queries": queries,
                "results": cleaned,
                "count": len(cleaned),
                "provider_outcomes": merged_outcomes,
                "freshness_scores": {
                    item["url"]: round(all_freshness.get(item["url"], 0.0), 4) for item in cleaned if item.get("url")
                },
            },
        )

    RRF_K: int = 60
    """Reciprocal Rank Fusion constant. k=60 is the academic default."""

    async def _execute_multi_search(
        self,
        query: str,
        count: int,
    ) -> ToolResult:
        """Execute search across a small weighted provider set using one query input."""
        available = [p for p in self.PROVIDER_WEIGHTS if self._provider_is_available(p)]
        if not available:
            return ToolResult.error(self._missing_provider_error())
        sample_size = min(3, len(available))
        weighted_order = self._weighted_provider_order()
        all_selected = weighted_order[:sample_size]

        coros: list[Awaitable[tuple[str, list[dict[str, str]]]]] = []
        for provider_name in all_selected:
            handler = self._get_provider_handler(provider_name)
            if handler is not None:
                delay = self._rng.uniform(0.5, 2.0)
                coros.append(
                    self._execute_provider_with_delay(
                        handler,
                        provider_name,
                        query,
                        count,
                        delay,
                    )
                )

        provider_results = await asyncio.gather(*coros, return_exceptions=True)

        url_to_result: dict[str, dict[str, str]] = {}
        url_to_provider_ranks: dict[str, dict[str, int]] = {}
        url_to_first_index: dict[str, int] = {}
        provider_outcomes: list[dict[str, Any]] = []

        for idx, result in enumerate(provider_results):
            provider_name = all_selected[idx] if idx < len(all_selected) else "?"
            if isinstance(result, BaseException):
                logger.warning(f"Web search provider '{provider_name}' failed: {_exc_detail(result)}")
                provider_outcomes.append(_provider_outcome(provider_name, "error", error=_exc_detail(result)))
                await self._reset_http_client()
                continue
            provider_name, items = result
            provider_outcomes.append(_provider_outcome(provider_name, "ok" if items else "empty", count=len(items)))
            if not items:
                continue
            for rank, item in enumerate(items):
                url = str(item.get("url", "")).strip()
                if not url or url == "N/A":
                    continue
                if url not in url_to_result:
                    url_to_result[url] = item
                    url_to_provider_ranks[url] = {}
                    url_to_first_index[url] = idx
                # Record the best (lowest) rank for this provider per URL.
                if provider_name not in url_to_provider_ranks[url]:
                    url_to_provider_ranks[url][provider_name] = rank

        all_variants = self._query_variants(query)
        if not url_to_result:
            return ToolResult.success(
                content="No results found for the search query.",
                metadata={
                    "query": query,
                    "variants": all_variants,
                    "results": [],
                    "count": 0,
                    "provider_outcomes": provider_outcomes,
                },
            )

        # Title-based deduplication as a complement to URL deduplication.
        url_to_result, url_to_provider_ranks = self._deduplicate_by_title(url_to_result, url_to_provider_ranks)

        # RRF scoring with optional recency boost.
        rrf_scores: dict[str, float] = {}
        freshness_scores: dict[str, float] = {}
        combined_scores: dict[str, float] = {}
        for url, ranks in url_to_provider_ranks.items():
            rrf_score = sum(1.0 / (self.RRF_K + rank) for rank in ranks.values())
            freshness_score = DateExtractor.freshness_score(url_to_result[url].get("date_iso", ""))
            rrf_scores[url] = rrf_score
            freshness_scores[url] = freshness_score
            combined_scores[url] = self._combined_rank_score(rrf_score, freshness_score)

        sorted_urls = sorted(
            url_to_result.keys(),
            key=lambda u: (-combined_scores[u], -freshness_scores[u], url_to_first_index[u]),
        )

        final_results = [url_to_result[u] for u in sorted_urls][:count]
        cleaned = filter_search_results(self._sanitize_results(final_results, count))[0]

        return ToolResult.success(
            content=self._format_results(cleaned),
            metadata={
                "query": query,
                "variants": all_variants,
                "results": cleaned,
                "count": len(cleaned),
                "provider_outcomes": provider_outcomes,
                "unique_urls": len(url_to_result),
                "rrf_scores": {u: round(rrf_scores[u], 4) for u in sorted_urls[:count]},
                "freshness_scores": {u: round(freshness_scores[u], 4) for u in sorted_urls[:count]},
            },
        )

    async def _execute_provider_with_delay(
        self,
        handler: ProviderHandler,
        provider_name: str,
        query: str,
        count: int,
        delay: float,
    ) -> tuple[str, list[dict[str, str]]]:
        """Execute a single provider with an optional staggered delay."""
        if delay > 0:
            await asyncio.sleep(delay)
        results = await self._execute_with_query_variants(
            handler,
            provider_name,
            query,
            count,
        )
        return provider_name, results

    async def _execute_with_query_variants(
        self,
        handler: ProviderHandler,
        provider_name: str,
        query: str,
        count: int,
    ) -> list[dict[str, str]]:
        merged: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        variants = self._query_variants(query)
        logger.debug(f"[web_search_raw] query='{query}' -> variants={variants}")
        for variant in variants:
            results = await handler(variant, count)
            for item in results:
                normalized = self._normalize_search_result(
                    item,
                    provider_name=provider_name,
                    matched_query=query,
                    matched_variant=variant,
                )
                url = str(normalized.get("url", "")).strip()
                if not url or url == "N/A" or url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(normalized)
                if len(merged) >= count:
                    return merged
        return merged

    def _weighted_provider_order(self) -> list[str]:
        remaining = self._available_weighted_providers()
        ordered: list[str] = []
        while remaining:
            choice = self._rng.choices(
                [provider for provider, _ in remaining],
                weights=[weight for _, weight in remaining],
                k=1,
            )[0]
            ordered.append(choice)
            remaining = [(provider, weight) for provider, weight in remaining if provider != choice]
        return ordered

    def _build_provider_order(self, requested_provider: str, weighted_order: list[str]) -> list[str]:
        if requested_provider in weighted_order:
            return [requested_provider] + [provider for provider in weighted_order if provider != requested_provider]

        handler = self._get_provider_handler(requested_provider)
        if handler is not None and requested_provider == "baidu":
            return [requested_provider] + [provider for provider in weighted_order if provider != requested_provider]

        return weighted_order

    def _available_weighted_providers(self) -> list[tuple[str, float]]:
        available: list[tuple[str, float]] = []
        for provider_name, weight in self.PROVIDER_WEIGHTS.items():
            if self._provider_is_available(provider_name):
                available.append((provider_name, weight))
        return available

    def _query_variants(self, query: str) -> list[str]:
        """Build search variants from one plain query."""
        normalized_query = self._sanitize_query(query)
        return self._append_current_date_variants(normalized_query)

    def _sanitize_query(self, query: str) -> str:
        normalized = " ".join(query.split())
        normalized = self._strip_recency_terms(normalized)
        normalized = self._strip_standalone_year_tokens(normalized)
        return " ".join(normalized.split())

    def _append_current_date_variants(self, query: str) -> list[str]:
        """Append a single 'latest' variant to a plain keyword query."""
        has_chinese = any(ord(c) > 127 for c in query)
        normalized_query = " ".join(query.split()) or query.strip()

        suffix = "最新" if has_chinese else "latest"
        return [f"{normalized_query} {suffix}".strip()]

    def _strip_recency_terms(self, query: str) -> str:
        stripped = _RECENCY_STRIP_PATTERN.sub(" ", query)
        return " ".join(stripped.split())

    def _strip_standalone_year_tokens(self, query: str) -> str:
        kept_tokens: list[str] = []
        for token in query.split():
            if _YEAR_TOKEN.fullmatch(token):
                continue
            kept_tokens.append(token)
        return " ".join(kept_tokens)

    def _provider_is_available(self, provider_name: str) -> bool:
        env_keys = self.PROVIDER_ENV_KEYS.get(provider_name, ())
        if not env_keys:
            return True
        return all(bool(os.getenv(env_key)) for env_key in env_keys)

    def _get_provider_handler(self, provider_name: str) -> ProviderHandler | None:
        handlers: dict[str, ProviderHandler] = {
            "exa": self._search_exa,
            "serper": self._search_serper,
            "linkup": self._search_linkup,
            "baidu": self._search_baidu,
            "doubao": self._search_doubao,
        }
        return handlers.get(provider_name)

    def _missing_provider_error(self) -> str:
        env_names = sorted({env for values in self.PROVIDER_ENV_KEYS.values() for env in values})
        return (
            "Search failed: no usable web search providers are available. "
            f"Configure one of these environment variables: {', '.join(env_names)}"
        )

    def _sanitize_results(self, results: list[dict[str, str]], count: int) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for item in results:
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            description = str(item.get("description", "")).strip()
            if not (title or url or description):
                continue
            cleaned_item = {
                "title": html.unescape(title or url or "N/A"),
                "url": url or "N/A",
                "description": html.unescape(description),
            }
            for key in ("provider", "matched_query", "matched_variant", "date_text", "date_iso", "date_confidence"):
                if key in item and item.get(key) not in {None, ""}:
                    cleaned_item[key] = str(item.get(key))
            cleaned.append(cleaned_item)
            if len(cleaned) >= count:
                break
        return cleaned

    def _normalize_search_result(
        self,
        item: dict[str, Any],
        *,
        provider_name: str,
        matched_query: str,
        matched_variant: str,
    ) -> dict[str, Any]:
        title = html.unescape(str(item.get("title", "")).strip())
        url = str(item.get("url", "")).strip()
        description = html.unescape(str(item.get("description", "")).strip())
        observed_at, date_text, date_confidence = self._date_extractor.extract(title, description, url)
        normalized: dict[str, Any] = {
            "title": title or url or "N/A",
            "url": url or "N/A",
            "description": description,
            "provider": provider_name,
            "matched_query": matched_query,
            "matched_variant": matched_variant,
        }
        if date_text:
            normalized["date_text"] = date_text
        if observed_at is not None:
            normalized["date_iso"] = observed_at.date().isoformat()
        if date_confidence:
            normalized["date_confidence"] = f"{date_confidence:.2f}"
        return normalized

    def _sort_single_provider_results(
        self,
        results: list[dict[str, Any]],
        *,
        count: int,
        time_window_days: int,
    ) -> list[dict[str, str]]:
        cleaned = self._sanitize_results(results, len(results) or count)
        deduped: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        for item in cleaned:
            url = item.get("url", "")
            title_key = self._normalize_title(item.get("title", ""))
            if url and url in seen_urls:
                continue
            if title_key and title_key in seen_titles:
                continue
            if url:
                seen_urls.add(url)
            if title_key:
                seen_titles.add(title_key)
            deduped.append(item)
        ranked = sorted(
            enumerate(deduped),
            key=lambda pair: (
                -DateExtractor.freshness_score(pair[1].get("date_iso", "")),
                pair[0],
            ),
        )
        return [item for _, item in ranked[:count]]

    async def _search_exa(self, query: str, count: int) -> list[dict[str, str]]:
        api_key = self._api_key or os.getenv("EXA_API_KEY")
        if not api_key:
            return []

        url = "https://api.exa.ai/search"
        payload = {
            "query": query,
            "type": "neural",
            "numResults": count,
            "useAutoprompt": True,
        }
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

        client = self._get_http_client()
        response = await client.post(url, json=payload, headers=headers, timeout=20.0)

        if response.status_code != 200:
            logger.warning(f"Exa returned status {response.status_code}")
            return []

        data = response.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("text", "")[:500],
            }
            for item in data.get("results", [])[:count]
        ]

    async def _search_serper(self, query: str, count: int) -> list[dict[str, str]]:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return []

        url = "https://google.serper.dev/search"
        payload = {"q": query, "gl": "cn", "hl": "zh-cn"}
        headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        }

        client = self._get_http_client()
        response = await client.post(url, json=payload, headers=headers, timeout=20.0)

        if response.status_code != 200:
            logger.warning(f"Serper returned status {response.status_code}")
            return []

        data = response.json()
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "description": item.get("snippet", ""),
            }
            for item in data.get("organic", [])[:count]
        ]
        if data.get("knowledgeGraph"):
            kg = data["knowledgeGraph"]
            results.insert(
                0,
                {
                    "title": kg.get("title", ""),
                    "url": kg.get("website", ""),
                    "description": kg.get("description", ""),
                },
            )
        return results[:count]

    async def _search_linkup(self, query: str, count: int) -> list[dict[str, str]]:
        api_key = os.getenv("LINKUP_API_KEY")
        if not api_key:
            return []

        url = "https://api.linkup.so/v1/search"
        payload = {
            "q": query,
            "depth": "standard",
            "outputType": "searchResults",
            "includeRawContent": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        client = self._get_http_client()
        response = await client.post(url, json=payload, headers=headers, timeout=20.0)

        if response.status_code != 200:
            logger.warning(f"Linkup returned status {response.status_code}")
            return []

        data = response.json()
        return [
            {
                "title": item.get("name", item.get("title", "")),
                "url": item.get("url", ""),
                "description": item.get("content", item.get("snippet", "")),
            }
            for item in data.get("results", [])[:count]
        ]

    async def _search_baidu(self, query: str, count: int) -> list[dict[str, str]]:
        url = f"https://www.baidu.com/s?wd={quote(query)}&rn={count}"
        headers = {"User-Agent": _BROWSER_USER_AGENT}

        client = self._get_http_client()
        response = await client.get(url, headers=headers, timeout=15.0)

        if response.status_code != 200:
            logger.warning(f"Baidu returned status {response.status_code}")
            return []

        html_text = response.text
        blocks = re.findall(
            r'(<div[^>]+class="[^"]*(?:result|c-container)[^"]*"[^>]*>.*?</div>\s*</div>?)',
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not blocks:
            blocks = re.findall(
                r"(<div[^>]+data-tools=.*?</div>\s*</div>?)",
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )

        results: list[dict[str, str]] = []
        for block in blocks:
            title_match = re.search(r"<h3[^>]*>.*?<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>.*?</h3>", block, re.DOTALL)
            if not title_match:
                continue
            desc_match = re.search(
                r'<div[^>]+class="[^"]*(?:content-right_8Zs40|c-abstract|c-span-last)[^"]*"[^>]*>(.*?)</div>',
                block,
                re.DOTALL,
            )
            results.append(
                {
                    "title": self._strip_html(title_match.group(2)),
                    "url": html.unescape(title_match.group(1)),
                    "description": self._strip_html(desc_match.group(1) if desc_match else ""),
                }
            )
            if len(results) >= count:
                break
        return results

    def _strip_html(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", "", value or "")
        return html.unescape(" ".join(text.split()))

    async def _search_doubao(self, query: str, count: int) -> list[dict[str, str]]:
        """Search via Doubao Search API (Volcengine).

        Requires DOUBAO_API_KEY. Endpoint: https://open.feedcoopapi.com/search-api/web-search
        Free tier: 500 searches/month.
        """
        api_key = os.getenv("DOUBAO_API_KEY")
        if not api_key:
            return []

        url = "https://open.feedcoopapi.com/search_api/web_search"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "Query": query,
            "SearchType": "web",
            "Count": min(count, 20),
            "NeedSummary": True,
        }

        client = self._get_http_client()
        try:
            response = await client.post(url, json=payload, headers=headers, timeout=20.0)
        except Exception as exc:
            logger.warning(f"Doubao search request failed: {_exc_detail(exc)}")
            await self._reset_http_client()
            return []

        if response.status_code != 200:
            logger.warning(f"Doubao returned status {response.status_code}: {response.text[:200]}")
            return []

        try:
            data = response.json()
        except Exception:
            logger.warning("Doubao returned invalid JSON")
            return []

        results: list[dict[str, str]] = []
        web_results = data.get("Result", {}).get("WebResults", [])
        for item in web_results[:count]:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": str(item.get("Title", "")),
                    "url": str(item.get("Url", "")),
                    "description": str(item.get("Summary", item.get("Snippet", ""))),
                }
            )
        return results

    def _clean_title(self, title: str) -> str:
        """清理标题,去除重复文本和特殊字符。"""
        if not title:
            return "N/A"

        # 去除HTML实体
        title = html.unescape(title)

        # 检测并去除重复的标题模式(如 "相对论相对论详细介绍")
        # 尝试找到重复的子串(最小长度 2,避免误伤 "API" 等正常缩写)
        for i in range(2, len(title) // 2 + 1):
            prefix = title[:i]
            if title.startswith(prefix * 2):
                # 找到重复,去除重复部分
                title = title[i:]
                break

        # 去除常见的重复后缀(如 "_百度百科百度百科")
        suffixes = ["_百度百科", "_百度知道", "_知乎", "_搜狗百科"]
        for suffix in suffixes:
            double_suffix = suffix * 2
            if double_suffix in title:
                title = title.replace(double_suffix, suffix)

        return title.strip()

    def _normalize_title(self, title: str) -> str:
        """Normalize title for deduplication: strip HTML, remove punctuation, lowercase."""
        text = html.unescape(title or "")
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"[^\w\s]", "", text)
        return " ".join(text.lower().split())

    def _deduplicate_by_title(
        self,
        url_to_result: dict[str, dict[str, str]],
        url_to_provider_ranks: dict[str, dict[str, int]],
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, int]]]:
        """Deduplicate results by exact normalized title match. Keep the one with higher RRF score."""
        title_to_best_url: dict[str, str] = {}

        def _rrf_score(ranks: dict[str, int]) -> float:
            return sum(1.0 / (self.RRF_K + rank) for rank in ranks.values())

        for url, result in url_to_result.items():
            normalized = self._normalize_title(result.get("title", ""))
            if not normalized:
                continue
            if normalized not in title_to_best_url:
                title_to_best_url[normalized] = url
            else:
                existing_url = title_to_best_url[normalized]
                if _rrf_score(url_to_provider_ranks[url]) > _rrf_score(url_to_provider_ranks[existing_url]):
                    title_to_best_url[normalized] = url

        kept = set(title_to_best_url.values())
        new_result = {u: url_to_result[u] for u in kept}
        new_ranks = {u: url_to_provider_ranks[u] for u in kept}
        return new_result, new_ranks

    def _format_results(self, results: list[dict[str, str]]) -> str:
        entries: list[str] = []
        for index, result in enumerate(results, start=1):
            title = self._clean_title(result.get("title", "N/A"))
            url = result.get("url", "N/A")
            head = f"{index}. {title} | {url}"
            date_text = result.get("date_text", "")
            if date_text:
                head += f" | {date_text}"
            provider = result.get("provider", "")
            if provider:
                # 多引擎合并场景需要区分来源引擎
                head += f" [{provider}]"

            description = result.get("description", "")
            if description:
                # 限制描述长度,避免过长
                max_desc_len = 300
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                entries.append(f"{head}\n{description}")
            else:
                entries.append(head)

        return wrap_external_content("\n\n".join(entries).strip(), "web search")

    def _format_results_multi(self, results: list[dict[str, str]]) -> str:
        """Format results from multiple queries, annotating source query per result."""
        entries: list[str] = []
        for index, result in enumerate(results, start=1):
            title = self._clean_title(result.get("title", "N/A"))
            url = result.get("url", "N/A")
            head = f"{index}. {title} | {url}"
            date_text = result.get("date_text", "")
            if date_text:
                head += f" | {date_text}"
            source_query = result.get("_source_query", "")
            if source_query:
                head += f" [query: {source_query}]"

            description = result.get("description", "")
            if description:
                max_desc_len = 300
                if len(description) > max_desc_len:
                    description = description[:max_desc_len] + "..."
                entries.append(f"{head}\n{description}")
            else:
                entries.append(head)

        return wrap_external_content("\n\n".join(entries).strip(), "web search")
