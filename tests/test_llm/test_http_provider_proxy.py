"""LLM 客户端必须直连：不随环境/系统代理走。

回归背景：anthropic 的 ``DefaultAsyncHttpxClient`` 构造时无条件读代理（连
``trust_env=False`` 都拦不住），客户端一旦在系统代理开启期间创建，代理地址就焊在
对象上；代理工具关掉后该客户端永久连接失败，而环境里已无任何代理痕迹。
"""

from __future__ import annotations

import httpx
import pytest

from src.llm._http_provider import _build_sdk_http_client


def _proxied_mounts(client: httpx.AsyncClient) -> list[str]:
    mounts = getattr(client, "_mounts", None) or {}
    return [str(pattern) for pattern, transport in mounts.items() if transport is not None]


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk", ["anthropic", "openai"])
async def test_sdk_http_client_ignores_env_proxy(monkeypatch: pytest.MonkeyPatch, sdk: str) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    # 先确认这些变量确实会被 httpx 认成代理，否则本用例空转
    assert httpx._utils.get_environment_proxies()

    if sdk == "anthropic":
        from anthropic import AsyncAnthropic

        client = _build_sdk_http_client(AsyncAnthropic, httpx.Timeout(5.0))
    else:
        from openai import AsyncOpenAI

        client = _build_sdk_http_client(AsyncOpenAI, httpx.Timeout(5.0))

    try:
        assert _proxied_mounts(client) == []
    finally:
        await client.aclose()
