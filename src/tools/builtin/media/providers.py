"""Provider logic for the media tool (Agnes).

Self-contained async implementation (httpx). API key comes from
``<COARA_HOME>/system/.env`` (or ``config/.env``), falling back to the
process environment / repo .env:

- ``AGNES_API_KEY`` — image (t2i/i2i) + video (t2v/i2v)
"""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from src.core.logger import logger

DEFAULT_AGNES_BASE = "https://api.agnes-ai.cn/v1"
DEFAULT_AGNES_IMAGE_MODEL = "agnes-image-2.1-flash"
DEFAULT_AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"

VIDEO_POLL_STATUSES_DONE = {"completed", "succeeded", "success", "done"}
VIDEO_POLL_STATUSES_FAIL = {"failed", "error", "cancelled", "canceled"}

_env_loaded = False


def _coara_home() -> Path | None:
    """Resolve coara home the standard way — never fall back to ``cwd/.env``."""
    from src.core.coara_home import resolve_bootstrap_coara_home, resolve_config_home

    try:
        from src.core.config import config_manager

        raw = getattr(config_manager, "_raw_config", None)
        if isinstance(raw, dict) and raw:
            return resolve_config_home(raw)
    except Exception:
        logger.debug("config coara_home 读取失败，回落 bootstrap 解析")
    return resolve_bootstrap_coara_home()


def load_env() -> None:
    """Load coara_home env files once (later files win within a home)."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    home = _coara_home()
    if home is None:
        return
    for rel in ("config/.env", "system/.env"):
        env_path = home / rel
        if env_path.is_file():
            load_dotenv(env_path, override=True)


def agnes_key() -> str:
    load_env()
    for name in ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ValueError("AGNES_API_KEY 未配置（写入 <coara_home>/system/.env）")


def _normalize_api_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if not base:
        return DEFAULT_AGNES_BASE
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _agnes_base_from_providers() -> str | None:
    home = _coara_home()
    if home is None:
        return None
    try:
        import yaml
    except ImportError:
        return None
    for rel in ("system/providers.yaml", "config/providers.yaml"):
        path = Path(home) / rel
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        providers = data.get("providers") if isinstance(data, dict) else None
        agnes = providers.get("agnes") if isinstance(providers, dict) else None
        if isinstance(agnes, dict):
            url = str(agnes.get("base_url") or "").strip()
            if url:
                return url
    return None


def resolve_agnes_base() -> str:
    load_env()
    env = os.environ.get("AGNES_BASE_URL", "").strip()
    if env:
        return _normalize_api_base(env)
    from_providers = _agnes_base_from_providers()
    if from_providers:
        return _normalize_api_base(from_providers)
    return DEFAULT_AGNES_BASE


def resolve_agnes_poll_base() -> str:
    base = resolve_agnes_base().rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


# ── HTTP helpers ─────────────────────────────────────────────────────────────


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    api_key: str,
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    resp = await client.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        if _is_video_queue_full(resp.status_code, resp.text):
            raise VideoQueueFullError(f"视频服务繁忙（HTTP {resp.status_code}），请稍后重试: {url}\n{resp.text[:500]}")
        if _is_quota_error(resp.status_code, resp.text):
            raise QuotaExceededError(f"额度/频率超限（HTTP {resp.status_code}）: {url}")
        raise ValueError(f"HTTP {resp.status_code} {url}\n{resp.text[:1500]}")
    return resp.json() if resp.text.strip() else {}


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    api_key: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    if resp.status_code >= 400:
        raise ValueError(f"HTTP {resp.status_code} {url}\n{resp.text[:1500]}")
    return resp.json() if resp.text.strip() else {}


class VideoDownloadError(Exception):
    """视频已在服务端生成（费用已扣）但下载/落盘失败。

    与「生成失败」区分：下载失败不该消耗 attempts 逼用户重提（重复扣费），
    而应仅重试下载阶段（video_id 已落盘，重跑直接进 poll 不再 submit）。
    """


async def download_file(client: httpx.AsyncClient, url: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with client.stream("GET", url, timeout=300.0) as resp:
            if resp.status_code >= 400:
                raise VideoDownloadError(f"HTTP {resp.status_code} 下载失败 {url}")
            with open(out, "wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
    except VideoDownloadError:
        raise
    except Exception as exc:  # noqa: BLE001 — 网络中断/磁盘满/落盘失败统一归类为下载失败
        raise VideoDownloadError(f"下载或落盘失败 {url}: {exc}") from exc
    try:
        size = out.stat().st_size
    except OSError as exc:
        raise VideoDownloadError(f"下载结果读取失败 {out}: {exc}") from exc
    if size <= 0:
        raise VideoDownloadError(f"下载结果为空文件: {out}")
    return size


def local_image_data_uri(path: str) -> str:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"参考图不存在: {p}")
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def resolve_images(items: list[str]) -> list[str]:
    resolved: list[str] = []
    for item in items:
        if item.startswith(("http://", "https://", "data:")):
            resolved.append(item)
        else:
            resolved.append(local_image_data_uri(item))
    return resolved


# ── response extraction ──────────────────────────────────────────────────────


def extract_image(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (url, b64) from OpenAI-style image payloads."""
    for key in ("images", "data"):
        items = payload.get(key)
        if isinstance(items, list) and items:
            first = items[0] or {}
            url = first.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url, None
            b64 = first.get("b64_json") or first.get("image")
            if isinstance(b64, str) and b64:
                return None, b64
    return None, None


def extract_video_id(payload: dict[str, Any]) -> str | None:
    for key in ("video_id", "videoId", "videoID"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("video_id", "videoId", "videoID", "id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_video_url(payload: dict[str, Any]) -> str | None:
    for container in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
        for key in ("video_url", "url", "output_url", "remixed_from_video_id"):
            value = container.get(key)
            if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
                return value.strip()
    return None


# ── providers ────────────────────────────────────────────────────────────────


class QuotaExceededError(ValueError):
    """Provider 额度/频率超限——可顺延到下一家，不算硬故障。"""


class VideoQueueFullError(ValueError):
    """Agnes 视频提交队列满——可稍后重试，不算硬故障。"""


def _is_quota_error(status_code: int, body_text: str) -> bool:
    if status_code == 429:
        return True
    text = body_text.lower()
    return any(k in text for k in ("quota", "rate limit", "usage limit", "limit exceeded", "daily allocation"))


def _is_video_queue_full(status_code: int, body_text: str) -> bool:
    """True for Agnes ``video_queue_full`` (typically HTTP 503)."""
    text = (body_text or "").lower()
    if "video_queue_full" in text or "queue is full" in text:
        return True
    return status_code == 503 and "queue" in text


async def agnes_image(
    client: httpx.AsyncClient,
    *,
    prompt: str,
    model: str,
    size: str,
    images: list[str] | None = None,
    timeout: float = 180.0,
) -> tuple[str | None, str | None]:
    """Returns (url, b64)."""
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "extra_body": {"response_format": "url"},
    }
    if images:
        body["extra_body"]["image"] = resolve_images(images)
    result = await _post_json(
        client,
        f"{resolve_agnes_base()}/images/generations",
        api_key=agnes_key(),
        body=body,
        timeout=timeout,
    )
    url, b64 = extract_image(result)
    if not url and not b64:
        raise ValueError(f"响应中没有图片: {str(result)[:1500]}")
    return url, b64


def _normalize_seconds(value: str | None) -> str:
    seconds = int(float(str(value).strip())) if value is not None and str(value).strip() else 5
    return str(max(4, min(12, seconds)))


def _video_mode_and_media(
    images: list[str] | None,
    *,
    mode: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map tool inputs to Agnes Video 2.5 ``mode`` + media fields."""
    resolved = resolve_images(images) if images else []
    if mode in ("text", "keyframe", "reference"):
        chosen = mode
    elif not resolved:
        chosen = "text"
    elif len(resolved) <= 2:
        chosen = "keyframe"
    else:
        chosen = "reference"

    media: dict[str, Any] = {}
    if chosen == "keyframe":
        if resolved:
            media["first_frame"] = resolved[0]
        if len(resolved) >= 2:
            media["last_frame"] = resolved[1]
    elif chosen == "reference" and resolved:
        media["images"] = resolved[:5]
    return chosen, media


async def agnes_create_video(
    client: httpx.AsyncClient,
    *,
    prompt: str,
    model: str,
    seconds: str | None = None,
    size: str = "720P",
    aspect_ratio: str = "16:9",
    mode: str | None = None,
    images: list[str] | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
    seed: int | None = None,
) -> str:
    """Create an Agnes Video 2.5 job with a single submit.

    Retry / queue-full handling is owned by the VideoScheduler (fixed cadence +
    re-queue-on-fail), so this performs one POST and lets any 503 / 429 /
    validation error bubble up for the scheduler to re-queue.
    """
    chosen_mode, media = _video_mode_and_media(images, mode=mode)
    if first_frame:
        chosen_mode = "keyframe"
        media["first_frame"] = (
            first_frame
            if first_frame.startswith(("http://", "https://", "data:"))
            else local_image_data_uri(first_frame)
        )
    if last_frame:
        chosen_mode = "keyframe"
        media["last_frame"] = (
            last_frame
            if last_frame.startswith(("http://", "https://", "data:"))
            else local_image_data_uri(last_frame)
        )
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "seconds": _normalize_seconds(seconds),
        "mode": chosen_mode,
        "size": size or "720P",
        "aspect_ratio": aspect_ratio or "16:9",
    }
    if seed is not None:
        body["seed"] = seed
    body.update(media)

    url = f"{resolve_agnes_base()}/videos"
    result = await _post_json(client, url, api_key=agnes_key(), body=body, timeout=60.0)
    video_id = extract_video_id(result)
    if not video_id:
        raise ValueError(f"创建视频任务失败（无 video_id）: {str(result)[:1500]}")
    return video_id


async def agnes_poll_video(
    client: httpx.AsyncClient,
    video_id: str,
    *,
    model: str | None = None,
    interval: float = 12.0,
    timeout: float = 600.0,
    signal: Any = None,
) -> dict[str, Any]:
    # Agnes 视频状态查询有服务端限流：2s 一次约 3 次即 429
    # ``video status query rate limit exceeded``。生成一条 4s 视频实测约需
    # 100s，故默认 12s 轮询，429 时做递增退避而非仅退 3s。
    deadline = time.monotonic() + timeout
    model_name = (model or DEFAULT_AGNES_VIDEO_MODEL).strip()
    url = f"{resolve_agnes_poll_base()}/agnesapi?video_id={video_id}&model_name={model_name}"
    backoff = 0
    while time.monotonic() < deadline:
        if signal is not None and getattr(signal, "aborted", False):
            raise InterruptedError("已取消")
        try:
            result = await _get_json(client, url, api_key=agnes_key())
        except ValueError as exc:
            if "HTTP 429" in str(exc):
                backoff += 1
                await _sleep(max(interval, 12.0) + backoff * 5.0, signal)
                continue
            raise
        backoff = 0
        status = str(result.get("status") or result.get("state") or "").lower()
        if status in VIDEO_POLL_STATUSES_DONE:
            return result
        if status in VIDEO_POLL_STATUSES_FAIL:
            raise ValueError(f"视频生成失败: {str(result)[:1000]}")
        await _sleep(interval, signal)
    raise ValueError(f"轮询超时（{timeout}s）video_id={video_id}")


async def _sleep(seconds: float, signal: Any) -> None:
    import asyncio

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if signal is not None and getattr(signal, "aborted", False):
            raise InterruptedError("已取消")
        await asyncio.sleep(min(0.5, end - time.monotonic()))
