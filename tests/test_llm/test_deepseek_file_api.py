"""DeepSeek Files API client offline tests (upload / cache / fallback)."""

from __future__ import annotations

import asyncio
import base64

import pytest

from src.llm import deepseek_file_api as dfa


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.url: str | None = None
        self.kwargs: dict | None = None

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.url = url
        self.kwargs = kwargs
        return self._response


@pytest.fixture(autouse=True)
def _clear():
    dfa.clear_cache()
    yield
    dfa.clear_cache()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_success_returns_file_id(monkeypatch):
    client = _FakeClient(_FakeResponse(200, {"id": "file-api-abc"}))
    monkeypatch.setattr(dfa.httpx, "AsyncClient", lambda *a, **k: client)
    result = asyncio.run(dfa.get_file_id("https://api.deepseek.com", "sk", _b64(b"hello"), "image/png"))
    assert result == "file-api-abc"
    assert client.url == "https://api.deepseek.com/files"
    assert client.kwargs["data"]["purpose"] == "user_data"
    assert client.kwargs["data"]["expires_after[seconds]"]


def test_upload_failure_returns_none(monkeypatch):
    client = _FakeClient(_FakeResponse(400, {"error": "boom"}))
    monkeypatch.setattr(dfa.httpx, "AsyncClient", lambda *a, **k: client)
    result = asyncio.run(dfa.get_file_id("https://api.deepseek.com", "sk", _b64(b"hello"), "image/png"))
    assert result is None


def test_cache_reuses_file_id(monkeypatch):
    client = _FakeClient(_FakeResponse(200, {"id": "file-api-abc"}))
    monkeypatch.setattr(dfa.httpx, "AsyncClient", lambda *a, **k: client)
    first = asyncio.run(dfa.get_file_id("https://api.deepseek.com", "sk", _b64(b"hello"), "image/png"))
    assert first == "file-api-abc"
    # 同一图第二次命中缓存，不再发请求（post 未被再次调用）
    url_before = client.url
    second = asyncio.run(dfa.get_file_id("https://api.deepseek.com", "sk", _b64(b"hello"), "image/png"))
    assert second == "file-api-abc"
    assert client.url == url_before


def test_invalid_base64_returns_none():
    result = asyncio.run(dfa.get_file_id("https://api.deepseek.com", "sk", "!!!not-base64!!!", "image/png"))
    assert result is None
