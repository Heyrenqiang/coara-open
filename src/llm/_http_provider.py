"""Shared HTTP lifecycle logic for SDK-based LLM providers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from src.core.logger import logger


def llm_request_timeout_seconds() -> float:
    """单次请求读超时：见 ``src.llm.timeouts``（此处保留兼容导出）."""
    from src.llm.timeouts import llm_request_timeout_seconds as _impl

    return _impl()


def product_user_agent() -> str:
    """User-Agent 标识：provider 控制台/日志里显示 coara 而非 SDK 默认名."""
    try:
        from importlib.metadata import version

        return f"coara/{version('coara')}"
    except Exception:
        logger.debug("UA version 解析失败，回落为裸 coara 标识")
        return "coara"


def init_sdk_client(client_cls: type[Any], api_key: str | None, base_url: str | None = None) -> Any | None:
    """Create SDK client, or ``None`` when ``api_key`` is empty (lazy init on first call)."""
    if not (api_key or "").strip():
        return None
    return client_cls(**sdk_client_kwargs(api_key, base_url, client_cls=client_cls))


def _force_direct_connection(client: Any) -> Any:
    """清掉客户端的代理挂载，LLM 调用一律直连。

    anthropic 的 ``DefaultAsyncHttpxClient`` 在构造时**无条件**读环境/系统代理
    （SDK 源码 ``_base_client.py``：``get_environment_proxies()`` → 逐个建
    ``AsyncHTTPTransport(proxy=...)`` 挂成 mount），传 ``trust_env=False`` 也拦不住。
    于是客户端只要在「系统代理开着」的那一刻构造，代理地址就焊死在对象上：用户随后
    关掉代理工具，这个客户端便永久连接失败（报 APIConnectionError: Connection error），
    而此时环境变量里已看不到任何代理痕迹——极难排查。
    需要走代理时请显式配置，不跟随系统/环境代理。
    """
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        for pattern in list(mounts):
            # None = 该模式不使用代理（与 httpx 处理 NO_PROXY 同语义）
            mounts[pattern] = None
    return client


def _build_sdk_http_client(client_cls: type[Any] | None, timeout: Any) -> Any:
    """Anthropic SDK 0.89+ requires httpx2; OpenAI still accepts httpx."""
    module = getattr(client_cls, "__module__", "") if client_cls else ""
    name = getattr(client_cls, "__name__", "") if client_cls else ""
    if "anthropic" in module or name == "AsyncAnthropic":
        try:
            from anthropic import DefaultAsyncHttpxClient

            return _force_direct_connection(DefaultAsyncHttpxClient(timeout=timeout, trust_env=False))
        except ImportError:
            try:
                from httpx2 import AsyncClient as Httpx2AsyncClient

                return _force_direct_connection(Httpx2AsyncClient(timeout=timeout, trust_env=False))
            except ImportError:
                pass
    import httpx

    return _force_direct_connection(httpx.AsyncClient(timeout=timeout, trust_env=False))


def sdk_client_kwargs(
    api_key: str | None,
    base_url: str | None = None,
    *,
    client_cls: type[Any] | None = None,
) -> dict[str, Any]:
    """Constructor kwargs for OpenAI/Anthropic SDK clients.

    - 显式 httpx 超时：连接 15s / 读取(无响应) 默认 300s 可配 / 写 60s / 连接池 15s；
      不设时 SDK 默认 600s 且无任何界面反馈，大上下文请求会像「卡死」
    - ``max_retries=0``：SDK 内置重试不可见不可控，重试统一交给
      ``src.llm.retry``（带退避、日志、Retry-After 与总预算上限）
    - ``default_headers.User-Agent``：覆盖 SDK 默认的 ``AsyncAnthropic/Python x.y.z``
      或 ``OpenAI/Python x.y.z``，第三方 provider 控制台（如 Kimi/MiniMax）显示的是 coara
    """
    import httpx

    timeout = httpx.Timeout(
        connect=15.0,
        read=llm_request_timeout_seconds(),
        write=60.0,
        pool=15.0,
    )
    kwargs: dict[str, Any] = {
        "api_key": api_key or "",
        "max_retries": 0,
        # 顶层 timeout 不传：超时已由 http_client 承载（下见 _build_sdk_http_client）。
        # 新版 anthropic SDK 校验顶层 timeout 只认自家 Timeout 类型，传 httpx.Timeout
        # 直接 TypeError（CI 曾踩：本地 0.89 宽松放行，runner 拉新版即炸）。
        "default_headers": {"User-Agent": product_user_agent()},
        "http_client": _build_sdk_http_client(client_cls, timeout),
    }
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


class HTTPProviderMixin:
    """Mixin that provides ``close()``, ``abort()`` and ``_abort_and_recreate()``
    for providers backed by an async HTTP client (OpenAI, Anthropic, etc.).

    Subclasses must set the class attribute ``_client_cls`` to the SDK client
    class they use (e.g. ``AsyncOpenAI`` or ``AsyncAnthropic``).
    """

    _client_cls: type[Any]

    # 子类必须提供：本 provider 的 API key 与缺失时的报错实现
    api_key: str

    # abort 状态由本 mixin 维护（类级 None 兜底；子类在 __init__ 里置实例初值）
    _abort_lock: asyncio.Lock | None = None
    _abort_task: asyncio.Task[None] | None = None
    _closed: bool = False

    def _require_api_key(self) -> None:
        """Raise when no usable API key is configured (subclass contract)."""
        raise NotImplementedError

    def ensure_client(self) -> Any:
        """Return live SDK client; create on first use after key check."""
        client = getattr(self, "client", None)
        if client is not None:
            return client
        self._require_api_key()
        self.client = self._client_cls(
            **sdk_client_kwargs(self.api_key, getattr(self, "base_url", None), client_cls=self._client_cls)
        )
        self._closed = False
        return self.client

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        abort_task = self._abort_task
        if abort_task is not None and not abort_task.done():
            abort_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abort_task
        client = getattr(self, "client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def abort(self) -> None:
        """Forcefully abort pending requests by closing and recreating the HTTP client."""
        if self._closed:
            return
        abort_task = self._abort_task
        if abort_task is not None and not abort_task.done():
            # 已有 abort 在途：复用即可，无需并发第二个重建
            return
        # 上一个 abort task 可能已完成（此时 client 已重建），但二次中止
        # 需要再关一次新建的 client——直接 return 会让第二次 abort 静默失效；
        # _abort_and_recreate 内部经 _closed 检查避免与 close() 竞态复活 client
        self._abort_task = asyncio.create_task(self._abort_and_recreate())

    async def _abort_and_recreate(self) -> None:
        abort_lock = self._abort_lock
        if abort_lock is None:
            abort_lock = asyncio.Lock()
            self._abort_lock = abort_lock
        async with abort_lock:
            if self._closed:
                return
            client = getattr(self, "client", None)
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            self.client = self._client_cls(
                **sdk_client_kwargs(self.api_key, getattr(self, "base_url", None), client_cls=self._client_cls)
            )
            self._closed = False
