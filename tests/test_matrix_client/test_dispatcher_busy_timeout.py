"""Tests for MatrixMessageDispatcher busy timeout (REMAINING_ISSUES #165)."""

from __future__ import annotations

import asyncio

import pytest

from src.matrix_client.sync_helpers import MatrixMessageDispatcher


@pytest.mark.asyncio
async def test_dispatcher_runs_serialised_normally() -> None:
    dispatcher = MatrixMessageDispatcher()
    order: list[str] = []

    async def job(name: str) -> None:
        order.append(name)

    dispatcher.schedule(job("a"), label="t-a")
    dispatcher.schedule(job("b"), label="t-b")
    await dispatcher.drain()
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_dispatcher_busy_timeout_invokes_on_busy_and_drops() -> None:
    dispatcher = MatrixMessageDispatcher()
    started = asyncio.Event()
    release = asyncio.Event()
    busy_calls: list[str] = []
    ran: list[str] = []

    async def hog() -> None:
        started.set()
        await release.wait()

    async def dropped() -> None:
        ran.append("dropped")

    async def on_busy() -> None:
        busy_calls.append("busy")

    dispatcher.schedule(hog(), label="hog")
    await started.wait()

    dispatcher.schedule(dropped(), label="doomed", on_busy=on_busy, busy_timeout=0.05)
    await asyncio.sleep(0.3)

    assert busy_calls == ["busy"]
    assert ran == []

    release.set()
    await dispatcher.drain()


@pytest.mark.asyncio
async def test_dispatcher_lock_released_after_handler_error() -> None:
    dispatcher = MatrixMessageDispatcher()
    ran: list[str] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> None:
        ran.append("good")

    dispatcher.schedule(bad(), label="bad")
    dispatcher.schedule(good(), label="good", busy_timeout=5.0)
    await dispatcher.drain()
    assert ran == ["good"]


@pytest.mark.asyncio
async def test_dispatcher_different_session_keys_run_concurrently() -> None:
    dispatcher = MatrixMessageDispatcher()
    a_started = asyncio.Event()
    b_ran = asyncio.Event()
    release_a = asyncio.Event()

    async def hog_a() -> None:
        a_started.set()
        await release_a.wait()

    async def job_b() -> None:
        b_ran.set()

    dispatcher.schedule(hog_a(), label="a", session_key="ws-a")
    await a_started.wait()
    dispatcher.schedule(job_b(), label="b", session_key="ws-b", busy_timeout=1.0)
    await asyncio.wait_for(b_ran.wait(), timeout=1.0)
    release_a.set()
    await dispatcher.drain()


@pytest.mark.asyncio
async def test_schedule_unlocked_runs_while_session_busy() -> None:
    dispatcher = MatrixMessageDispatcher()
    hog_started = asyncio.Event()
    unlocked_ran = asyncio.Event()
    release = asyncio.Event()

    async def hog() -> None:
        hog_started.set()
        await release.wait()

    async def unlocked() -> None:
        unlocked_ran.set()

    dispatcher.schedule(hog(), label="hog", session_key="ws-a")
    await hog_started.wait()
    dispatcher.schedule_unlocked(unlocked(), label="ws-cmd")
    await asyncio.wait_for(unlocked_ran.wait(), timeout=1.0)
    release.set()
    await dispatcher.drain()
