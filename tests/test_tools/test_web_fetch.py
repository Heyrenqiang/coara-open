from __future__ import annotations

import httpx
import pytest

from src.tools.builtin.web.web_fetch import MAX_FETCH_BYTES, WebFetchToolInvocation


def test_prepare_url_normalizes_and_converts_github_blob_to_raw() -> None:
    invocation = WebFetchToolInvocation(
        {
            "url": "HTTPS://GitHub.com/Owner/Repo/blob/main/README.md/",
            "max_chars": 1200,
        }
    )

    assert invocation.url == "https://raw.githubusercontent.com/Owner/Repo/main/README.md"


def test_validate_url_rejects_blocked_hosts_and_schemes() -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com"})

    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        invocation._validate_url("file://example.com/data.txt")

    with pytest.raises(ValueError, match="Blocked host: localhost"):
        invocation._validate_url("https://localhost/path")

    with pytest.raises(ValueError, match="Blocked private or local address"):
        invocation._validate_url("https://192.168.1.10/path")


@pytest.mark.asyncio
async def test_fetch_text_with_retry_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com"})
    attempts = {"count": 0}

    async def fake_fetch_once() -> tuple[str, str]:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("temporary network issue")
        return "text/plain", "ok"

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(invocation, "_fetch_text_once", fake_fetch_once)
    monkeypatch.setattr("src.tools.builtin.web.web_fetch.asyncio.sleep", fake_sleep)

    content_type, text = await invocation._fetch_text_with_retry()

    assert (content_type, text) == ("text/plain", "ok")
    assert attempts["count"] == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_fetch_text_once_truncates_at_max_fetch_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com"})

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"content-type": "text/plain"}
            self.encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield b"a" * (MAX_FETCH_BYTES + 1)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        def __init__(self, **_: object) -> None:
            return None

        def stream(self, method: str, url: str) -> FakeResponse:
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("src.tools.builtin.web.web_fetch.httpx.AsyncClient", FakeClient)

    content_type, text = await invocation._fetch_text_once()

    assert content_type == "text/plain"
    assert len(text) == MAX_FETCH_BYTES + len(f"\n\n... [download truncated at {MAX_FETCH_BYTES} bytes]")
    assert "download truncated" in text


@pytest.mark.asyncio
async def test_fetch_redirect_to_private_host_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRF 逐跳防护：公网 URL 302 到内网地址被拒。"""
    invocation = WebFetchToolInvocation({"url": "https://example.com/start"})

    class _RedirectResponse:
        status_code = 302
        headers = {"location": "http://169.254.169.254/latest/meta-data"}
        encoding = "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Client:
        def stream(self, method, url):
            return _RedirectResponse()

    with pytest.raises(ValueError, match="Blocked private or local address"):
        await invocation._fetch_following_redirects(_Client())


@pytest.mark.asyncio
async def test_execute_returns_friendly_message_for_403(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = WebFetchToolInvocation({"url": "https://blocked.example.com/article"})

    monkeypatch.setattr(invocation, "_validate_url", lambda url: None)

    async def fake_fetch_text_with_retry() -> tuple[str, str]:
        request = httpx.Request("GET", invocation.url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("Forbidden", request=request, response=response)

    monkeypatch.setattr(invocation, "_fetch_text_with_retry", fake_fetch_text_with_retry)

    result = await invocation.execute()

    assert result.is_error
    content = str(result.content)
    assert "403 Forbidden" in content
    assert "web_search snippets" in content


@pytest.mark.asyncio
async def test_execute_blocks_duplicate_url_in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools.builtin.web.fetch_policy import FetchSessionTracker

    FetchSessionTracker._sessions.clear()  # noqa: SLF001
    invocation = WebFetchToolInvocation({"url": "https://example.com/article"})
    object.__setattr__(invocation, "session_id", "sess-dup-test")
    monkeypatch.setattr(invocation, "_validate_url", lambda url: None)

    async def fake_fetch_text_with_retry() -> tuple[str, str]:
        body = "Detailed article content. " * 80
        return "text/html", body

    monkeypatch.setattr(invocation, "_fetch_text_with_retry", fake_fetch_text_with_retry)

    first = await invocation.execute()
    assert not first.is_error

    second = await invocation.execute()
    assert second.is_error
    assert "duplicate" in str(second.content).lower()


@pytest.mark.asyncio
async def test_execute_rejects_low_quality_content(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com/empty"})
    object.__setattr__(invocation, "session_id", "sess-quality")
    monkeypatch.setattr(invocation, "_validate_url", lambda url: None)

    async def fake_fetch_text_with_retry() -> tuple[str, str]:
        return "text/html", "Home About"

    monkeypatch.setattr(invocation, "_fetch_text_with_retry", fake_fetch_text_with_retry)

    result = await invocation.execute()
    assert result.is_error
    assert "Low-value" in str(result.content) or "too short" in str(result.content).lower()
    assert result.metadata.get("fetch_meta", {}).get("blocked") in {"low_quality", "too_short"}


@pytest.mark.asyncio
async def test_extract_readable_text_used_for_html() -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com/page"})
    html = "<html><body><article><p>" + ("paragraph text. " * 50) + "</p></article></body></html>"
    text = invocation._html_to_text(html)
    assert "paragraph text" in text


@pytest.mark.asyncio
async def test_execute_preserves_current_success_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    invocation = WebFetchToolInvocation({"url": "https://example.com", "max_chars": 8})
    invocation.max_chars = 8
    object.__setattr__(invocation, "session_id", "sess-trunc")

    monkeypatch.setattr(invocation, "_validate_url", lambda url: None)

    async def fake_fetch_text_with_retry() -> tuple[str, str]:
        body = "Detailed plain text body for testing truncation behavior. " * 8
        return "text/plain", body

    monkeypatch.setattr(invocation, "_fetch_text_with_retry", fake_fetch_text_with_retry)

    result = await invocation.execute()

    assert not result.is_error
    assert "... [truncated]" in str(result.content)
    assert result.metadata["fetch_meta"]["chars"] <= invocation.max_chars + 24
    assert result.metadata["url"] == "https://example.com/"
