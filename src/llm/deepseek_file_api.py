"""DeepSeek Files API client — upload image once, cache file_id, expire cleanup.

按 DeepSeek 官方 guides/files_api：``POST /files``（multipart, purpose=user_data）
拿 file_id，后续请求用 ``{"type":"file","file_id":...}`` 引用。按内容 hash 缓存
复用，避免重复上传；上传时设 expires_after 有效期，过期后重新上传。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time

import httpx

_FILE_API_TIMEOUT = 60.0
_DEFAULT_EXPIRES_SECONDS = 30 * 24 * 3600  # 30 天（官方允许 1h–30d）
# 缓存比文件有效期略早过期，避免用到已过期 file_id 触发 400
_EXPIRES_SAFETY = 6 * 3600

_CACHE: dict[str, dict] = {}  # content_hash -> {"file_id": str, "expires_at": float}
_LOCK = asyncio.Lock()


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files_url(base_url: str) -> str:
    """DeepSeek base_url 为 https://api.deepseek.com；files 端点为 /files。"""
    return base_url.rstrip("/") + "/files"


async def _do_upload(
    base_url: str,
    api_key: str,
    data: bytes,
    media_type: str,
    expires_seconds: int,
) -> str | None:
    """POST /files 上传，返回 file_id；失败返回 None（调用方 fallback base64）。"""
    url = _files_url(base_url)
    form: dict[str, str] = {
        "purpose": "user_data",
        "expires_after[anchor]": "created_at",
        "expires_after[seconds]": str(expires_seconds),
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=_FILE_API_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers=headers,
                files={"file": ("image", data, media_type)},
                data=form,
            )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        file_id = payload.get("id")
        return str(file_id) if file_id else None
    except httpx.HTTPError:
        return None


async def get_file_id(
    base_url: str,
    api_key: str,
    base64_data: str,
    media_type: str,
    *,
    expires_seconds: int = _DEFAULT_EXPIRES_SECONDS,
) -> str | None:
    """按内容 hash 上传并缓存 file_id；同一图复用。失败返回 None（fallback base64）。"""
    try:
        data = base64.b64decode(base64_data)
    except (ValueError, TypeError):
        return None
    content_hash = _content_hash(data)
    async with _LOCK:
        cached = _CACHE.get(content_hash)
        if cached and cached["expires_at"] > time.time():
            return cached["file_id"]
    file_id = await _do_upload(base_url, api_key, data, media_type, expires_seconds)
    if file_id:
        async with _LOCK:
            _CACHE[content_hash] = {
                "file_id": file_id,
                "expires_at": time.time() + expires_seconds - _EXPIRES_SAFETY,
            }
    return file_id


def clear_cache() -> None:
    """清空缓存（测试/配置变更用）。"""
    _CACHE.clear()
