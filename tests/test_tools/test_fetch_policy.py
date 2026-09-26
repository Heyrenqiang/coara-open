from __future__ import annotations

import pytest

from src.tools.builtin.web.fetch_policy import (
    FetchSessionTracker,
    extract_readable_text,
    load_fetch_policy_config,
    score_extracted_text,
)


def test_extract_readable_text_prefers_article() -> None:
    html = """
    <html><body>
      <nav>Home | About</nav>
      <article><h1>Title</h1><p>First paragraph with enough content.</p><p>Second paragraph.</p></article>
      <footer>Copyright</footer>
    </body></html>
    """
    text = extract_readable_text(html)
    assert "Title" in text
    assert "First paragraph" in text
    assert "Home | About" not in text


def test_score_extracted_text_penalizes_short_nav() -> None:
    nav = "Home About Contact Login " * 20
    article = "Cloudflare Workers deployment guide. " * 40
    assert score_extracted_text(nav) < score_extracted_text(article)


@pytest.mark.asyncio
async def test_fetch_session_tracker_blocks_duplicate_url() -> None:
    policy = load_fetch_policy_config({"web_fetch": {"skip_duplicate_url": True, "max_per_session": 10}})
    session = "sess-dedup"
    assert await FetchSessionTracker.check(session, "https://example.com/a", policy) is None
    await FetchSessionTracker.record(session, "https://example.com/a")
    assert await FetchSessionTracker.check(session, "https://example.com/a", policy) == "duplicate_url"


@pytest.mark.asyncio
async def test_fetch_session_tracker_domain_limit() -> None:
    policy = load_fetch_policy_config({"web_fetch": {"max_per_domain": 2, "max_per_session": 20}})
    session = "sess-domain"
    await FetchSessionTracker.record(session, "https://news.example.com/1")
    await FetchSessionTracker.record(session, "https://blog.example.com/2")
    reason = await FetchSessionTracker.check(session, "https://docs.example.com/3", policy)
    assert reason == "domain_limit"
