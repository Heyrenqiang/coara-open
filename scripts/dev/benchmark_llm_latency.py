#!/usr/bin/env python3
"""Benchmark LLM provider latency. Usage: scripts/dev/README.md"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv

from src.core.types import Message, MessageRole
from src.llm.anthropic import AnthropicProvider
from src.llm.openai import OpenAIProvider


@dataclass
class BenchResult:
    label: str
    ok: bool
    total_ms: float
    output_tokens: int = 0
    input_tokens: int = 0
    chars: int = 0
    error: str = ""


SHORT_USER = "用三句话介绍赣州信丰。"
LONG_USER = (
    "请写一份约 1500 字的中文说明，介绍 coara 多智能体运行时的工作方式，"
    "包括 Root、delegate 子智能体、workflow 引擎三层执行模型，分段加小标题。"
)


async def _bench_openai(label: str, provider: OpenAIProvider, **kwargs) -> BenchResult:
    t0 = time.perf_counter()
    try:
        resp = await provider.complete(
            messages=[Message(role=MessageRole.USER, content=LONG_USER if "long" in label else SHORT_USER)],
            system_prompt="你是简洁助手。",
            tools=None,
            **kwargs,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        content = resp.content or ""
        usage = resp.usage or {}
        return BenchResult(
            label=label,
            ok=True,
            total_ms=elapsed,
            output_tokens=int(usage.get("output_tokens", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            chars=len(content),
        )
    except Exception as exc:
        return BenchResult(label=label, ok=False, total_ms=(time.perf_counter() - t0) * 1000, error=str(exc))


async def _bench_anthropic(label: str, provider: AnthropicProvider, **kwargs) -> BenchResult:
    t0 = time.perf_counter()
    try:
        resp = await provider.complete(
            messages=[Message(role=MessageRole.USER, content=LONG_USER if "long" in label else SHORT_USER)],
            system_prompt="你是简洁助手。",
            tools=None,
            **kwargs,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        content = resp.content or ""
        usage = resp.usage or {}
        return BenchResult(
            label=label,
            ok=True,
            total_ms=elapsed,
            output_tokens=int(usage.get("output_tokens", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            chars=len(content),
        )
    except Exception as exc:
        return BenchResult(label=label, ok=False, total_ms=(time.perf_counter() - t0) * 1000, error=str(exc))


async def _bench_mimo_raw(label: str, base_url: str, api_key: str, body: dict) -> BenchResult:
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post(url, headers=headers, json=body)
            elapsed = (time.perf_counter() - t0) * 1000
            if r.status_code >= 400:
                return BenchResult(
                    label=label, ok=False, total_ms=elapsed, error=f"HTTP {r.status_code}: {r.text[:300]}"
                )
            data = r.json()
            content = data["choices"][0]["message"].get("content") or ""
            usage = data.get("usage") or {}
            return BenchResult(
                label=label,
                ok=True,
                total_ms=elapsed,
                output_tokens=int(usage.get("completion_tokens", 0)),
                input_tokens=int(usage.get("prompt_tokens", 0)),
                chars=len(content),
            )
    except Exception as exc:
        return BenchResult(label=label, ok=False, total_ms=(time.perf_counter() - t0) * 1000, error=str(exc))


def _print_row(r: BenchResult) -> None:
    if r.ok:
        tok_s = (r.output_tokens / (r.total_ms / 1000)) if r.total_ms > 0 and r.output_tokens else 0
        print(
            f"  {r.label:<42} {r.total_ms:8.0f}ms  out={r.output_tokens:5d}  in={r.input_tokens:5d}  "
            f"chars={r.chars:5d}  {tok_s:5.1f} tok/s"
        )
    else:
        print(f"  {r.label:<42} {r.total_ms:8.0f}ms  ERROR: {r.error[:120]}")


async def main() -> None:
    coara_home = os.environ.get("COARA_HOME", "D:/coara")
    load_dotenv(Path(coara_home) / "config" / ".env", override=False)

    mimo_key = os.environ.get("XIAOMIMIMO_API_KEY", "")
    minimax_key = os.environ.get("ANTHROPIC_API_KEY", "")
    mimo_base = "https://token-plan-cn.xiaomimimo.com/v1"
    minimax_base = "https://api.minimaxi.com/anthropic"

    if not mimo_key:
        print("XIAOMIMIMO_API_KEY missing")
    if not minimax_key:
        print("ANTHROPIC_API_KEY missing")

    print("=" * 96)
    print("LLM latency benchmark (same prompts, parameter variants)")
    print("=" * 96)

    results: list[BenchResult] = []

    if mimo_key:
        mimo = OpenAIProvider(
            name="xiaomi",
            api_key=mimo_key,
            base_url=mimo_base,
            default_model="mimo-v2.5-pro",
            default_max_tokens=8192,
        )
        print("\n[MiMo OpenAI SDK via coara OpenAIProvider]")
        for label, kwargs in [
            ("mimo short max_tokens=1024", {"model": "mimo-v2.5-pro", "max_tokens": 1024, "temperature": 0.7}),
            ("mimo short max_tokens=8192", {"model": "mimo-v2.5-pro", "max_tokens": 8192, "temperature": 0.7}),
            ("mimo short max_tokens=32768", {"model": "mimo-v2.5-pro", "max_tokens": 32768, "temperature": 0.7}),
            ("mimo long max_tokens=1024", {"model": "mimo-v2.5-pro", "max_tokens": 1024, "temperature": 0.7}),
            ("mimo long max_tokens=8192", {"model": "mimo-v2.5-pro", "max_tokens": 8192, "temperature": 0.7}),
            ("mimo long max_tokens=32768", {"model": "mimo-v2.5-pro", "max_tokens": 32768, "temperature": 0.7}),
        ]:
            r = await _bench_openai(label, mimo, **kwargs)
            results.append(r)
            _print_row(r)

        print("\n[MiMo raw HTTP — official max_completion_tokens vs max_tokens]")
        base_body = {
            "model": "mimo-v2.5-pro",
            "messages": [
                {"role": "system", "content": "你是简洁助手。"},
                {"role": "user", "content": LONG_USER},
            ],
            "temperature": 0.7,
        }
        for label, extra in [
            ("mimo raw max_completion_tokens=1024", {"max_completion_tokens": 1024}),
            ("mimo raw max_completion_tokens=8192", {"max_completion_tokens": 8192}),
            ("mimo raw max_tokens=8192", {"max_tokens": 8192}),
        ]:
            r = await _bench_mimo_raw(label, mimo_base, mimo_key, {**base_body, **extra})
            results.append(r)
            _print_row(r)

        print("\n[MiMo warm repeat — long 8192 x2]")
        for i in (1, 2):
            r = await _bench_openai(f"mimo long warm#{i} max_tokens=8192", mimo, model="mimo-v2.5-pro", max_tokens=8192)
            _print_row(r)

        await mimo.close()

    if minimax_key:
        mm = AnthropicProvider(
            name="minimax",
            api_key=minimax_key,
            base_url=minimax_base,
            default_model="MiniMax-M3",
            default_max_tokens=8192,
        )
        print("\n[MiniMax Anthropic SDK via coara AnthropicProvider]")
        for label, kwargs in [
            ("minimax short M3 max_tokens=4096", {"model": "MiniMax-M3", "max_tokens": 4096, "temperature": 0.7}),
            ("minimax short M3 max_tokens=32768", {"model": "MiniMax-M3", "max_tokens": 32768, "temperature": 0.7}),
            ("minimax long M3 max_tokens=4096", {"model": "MiniMax-M3", "max_tokens": 4096, "temperature": 0.7}),
            ("minimax long M3 max_tokens=8192", {"model": "MiniMax-M3", "max_tokens": 8192, "temperature": 0.7}),
        ]:
            r = await _bench_anthropic(label, mm, **kwargs)
            results.append(r)
            _print_row(r)

        print("\n[MiniMax service_tier=priority — short]")
        try:
            r = await _bench_anthropic(
                "minimax short priority max=4096",
                mm,
                model="MiniMax-M3",
                max_tokens=4096,
                temperature=0.7,
                extra_body={"service_tier": "priority"},
            )
            _print_row(r)
        except TypeError:
            r = await _bench_anthropic(
                "minimax short priority max=4096",
                mm,
                model="MiniMax-M3",
                max_tokens=4096,
                temperature=0.7,
                metadata={"service_tier": "priority"},
            )
            _print_row(r)

        await mm.close()

    ok = [r for r in results if r.ok]
    if ok:
        print("\n" + "=" * 96)
        print("Summary: coara default agent.main uses max_tokens=8192 (tune in providers.yaml).")
        fastest = min(ok, key=lambda x: x.total_ms)
        slowest = max(ok, key=lambda x: x.total_ms)
        print(f"  Fastest: {fastest.label} ({fastest.total_ms:.0f}ms, {fastest.output_tokens} out tokens)")
        print(f"  Slowest: {slowest.label} ({slowest.total_ms:.0f}ms, {slowest.output_tokens} out tokens)")


if __name__ == "__main__":
    asyncio.run(main())
