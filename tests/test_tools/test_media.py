"""Tests for the media builtin tool.

``media`` —— 单一 provider（agnes）。生图走 ``providers.agnes_image``；视频
走 ``video_queue.VideoQueue``（入队）+ ``video_scheduler.VideoScheduler``（常驻调度）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.builtin.media import MediaTool, providers
from src.tools.builtin.media.video_queue import _MAX_TERMINAL_RECORDS, MAX_ATTEMPTS, VideoQueue, _runtime_root
from src.tools.builtin.media.video_scheduler import VideoScheduler, _CancelFlag


def _tool(workspace: Path) -> MediaTool:
    return MediaTool(workspace_root=workspace)


@pytest.fixture(autouse=True)
def _isolated_coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把全部磁盘状态隔离到本测试的 tmp_path，并重置 VideoScheduler 单例绑定。"""
    monkeypatch.setenv("COARA_HOME", str(tmp_path))
    sched = VideoScheduler()
    sched.stop()
    sched._queue = VideoQueue()
    sched._active_polls.clear()
    sched._cancel_flags.clear()
    sched._event_bus = None
    sched._next_run_at = 0.0


class _FakeEventBus:
    """Minimal event bus that records published TraceEvents."""

    def __init__(self) -> None:
        self.events: list = []

    def publish(self, event) -> None:  # noqa: ANN001 — event is a TraceEvent
        self.events.append(event)


# ── 参数校验 ─────────────────────────────────────────────────────────────────


def test_kind_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        _tool(tmp_path).create_invocation({"prompt": "a cat"})


def test_prompt_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="prompt"):
        _tool(tmp_path).create_invocation({"kind": "image"})


def test_video_rejects_siliconflow(tmp_path: Path) -> None:
    """视频仅支持 agnes——available_providers 不含 agnes 即拒绝（siliconflow 等已移除）。"""
    tool = MediaTool(workspace_root=tmp_path, available_providers={"siliconflow"})
    with pytest.raises(ValueError, match="agnes"):
        tool.create_invocation({"kind": "video", "prompt": "sea"})


def test_default_provider_per_kind(tmp_path: Path) -> None:
    image_inv = _tool(tmp_path).create_invocation({"kind": "image", "prompt": "x"})
    video_inv = _tool(tmp_path).create_invocation({"kind": "video", "prompt": "x"})
    assert image_inv.provider == "agnes"
    assert video_inv.provider == "agnes"


def test_video_has_no_wait_param(tmp_path: Path) -> None:
    """视频固定后台执行（不阻塞回合），不提供前台等待参数。"""
    tool = _tool(tmp_path)
    assert "wait" not in tool.parameters_schema["properties"]


def test_all_provider_rejected_for_video(tmp_path: Path) -> None:
    """provider 参数已失效、一律固定为 agnes；传 provider='all' 不再扇出。"""
    inv = _tool(tmp_path).create_invocation({"kind": "video", "prompt": "x", "provider": "all"})
    assert inv.provider == "agnes"


def test_relative_out_resolved_under_workspace(tmp_path: Path) -> None:
    inv = _tool(tmp_path).create_invocation({"kind": "image", "prompt": "x", "out": "pic/a.png"})
    assert inv.out is not None and inv.out.is_absolute()
    assert str(inv.out).startswith(str(tmp_path.resolve()))


def test_cancel_requires_task_id(tmp_path: Path) -> None:
    """action=cancel 缺 task_id 时构造期不报错，但执行时返回错误。"""
    inv = _tool(tmp_path).create_invocation({"action": "cancel"})
    assert inv.task_id == ""


# ── 图片生成（仅 agnes）─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agnes_image_auto_names_under_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_agnes_image(client, **kwargs):
        captured.update(kwargs)
        return "https://example.com/b.png", None

    async def fake_download(client, url, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 10)
        return 10

    monkeypatch.setattr(providers, "agnes_image", fake_agnes_image)
    monkeypatch.setattr(providers, "download_file", fake_download)

    inv = _tool(tmp_path).create_invocation(
        {"kind": "image", "prompt": "lake", "provider": "agnes", "image": ["ref.png"]}
    )
    result = await inv.execute()
    assert not result.is_error
    assert captured["images"] == ["ref.png"]
    saved = Path(result.metadata["saved"])
    assert saved.parent == (tmp_path.resolve() / "media")
    assert saved.name.startswith("image_agnes_") and saved.suffix == ".png"


@pytest.mark.asyncio
async def test_agnes_image_saves_b64_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    raw_bytes = b"png-bytes"

    async def fake_agnes_image(client, **kwargs):
        return None, base64.b64encode(raw_bytes).decode("ascii")

    monkeypatch.setattr(providers, "agnes_image", fake_agnes_image)

    out = tmp_path / "cat.png"
    inv = _tool(tmp_path).create_invocation({"kind": "image", "prompt": "cat", "out": str(out)})
    result = await inv.execute()
    assert not result.is_error
    assert out.read_bytes() == raw_bytes
    assert result.metadata["bytes"] == len(raw_bytes)
    assert result.metadata["provider"] == "agnes"


# ── 视频：入队（VideoQueue 方法，非 spawn）──────────────────────────────────


@pytest.mark.asyncio
async def test_video_enqueues_and_returns_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """视频 generate 同步入队后立即返回，不 spawn 后台子进程。"""
    captured: dict = {}

    def fake_enqueue(self, prompt, params, **kwargs):
        captured.update({"prompt": prompt, "params": params, "kwargs": kwargs})
        return "vq-test-1"

    monkeypatch.setattr(VideoQueue, "enqueue", fake_enqueue)
    monkeypatch.setattr(VideoScheduler, "start", lambda self: None)

    out = tmp_path / "fireflies.mp4"
    inv = _tool(tmp_path).create_invocation(
        {"kind": "video", "prompt": "fireflies", "out": str(out)}
    )
    result = await inv.execute()
    assert not result.is_error
    assert "已加入生成队列" in str(result.content)
    assert "task_id=vq-test-1" in str(result.content)
    assert result.metadata.get("background") is True
    assert result.metadata["task_id"] == "vq-test-1"
    assert result.metadata["saved"] is None
    assert captured["prompt"] == "fireflies"
    assert captured["params"]["model"] == providers.DEFAULT_AGNES_VIDEO_MODEL
    assert captured["kwargs"]["out"] == str(out)


@pytest.mark.asyncio
async def test_queue_status_reports_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(VideoScheduler, "start", lambda self: None)
    q = VideoQueue()
    tid = q.enqueue("a prompt", {"model": "m"}, out=str(tmp_path / "a.mp4"), description="视频生成: a")
    inv = _tool(tmp_path).create_invocation({"action": "status"})
    result = await inv.execute()
    assert not result.is_error
    assert "媒体生成队列" in str(result.content)
    assert tid in str(result.content)
    assert "queued" in str(result.content)


@pytest.mark.asyncio
async def test_queue_cancel_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(VideoScheduler, "start", lambda self: None)
    q = VideoQueue()
    tid = q.enqueue("a", {"model": "m"}, out=str(tmp_path / "a.mp4"))

    inv = _tool(tmp_path).create_invocation({"action": "cancel", "task_id": tid})
    result = await inv.execute()
    assert not result.is_error
    assert "已取消媒体任务" in str(result.content)

    # 已终态（cancelled）再取消 → 失败
    result2 = await _tool(tmp_path).create_invocation({"action": "cancel", "task_id": tid}).execute()
    assert result2.is_error
    assert "已终态" in str(result2.content) or "未找到可取消" in str(result2.content)

    # 缺 task_id → 失败
    result3 = await _tool(tmp_path).create_invocation({"action": "cancel"}).execute()
    assert result3.is_error
    assert "task_id" in str(result3.content)


# ── VideoQueue 单元测试（tmp_path 隔离）─────────────────────────────────────


def test_video_queue_enqueue_pop_next_fifo(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    t1 = q.enqueue("a")
    t2 = q.enqueue("b")
    popped = q.pop_next()
    assert popped is not None and popped.task_id == t1
    assert popped.status == "running"
    assert q.pop_next().task_id == t2
    assert q.pop_next() is None


def test_video_queue_requeue_tail(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    t1 = q.enqueue("a")
    t2 = q.enqueue("b")
    assert q.pop_next().task_id == t1
    q.requeue_tail(t1, "err")
    cur = q.get(t1)
    assert cur is not None
    assert cur.status == "queued"
    assert cur.attempts == 1
    assert cur.error == "err"
    # 重排到队尾后，先出队的是更早入队的 t2
    assert q.pop_next().task_id == t2
    assert q.pop_next().task_id == t1


def test_video_queue_cancel(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    tid = q.enqueue("a")
    assert q.cancel(tid) is True
    cur = q.get(tid)
    assert cur is not None and cur.status == "cancelled"
    assert q.cancel(tid) is False  # 已终态
    assert q.cancel("vq-nope") is False  # 未知任务
    assert q.has_pending() is False


def test_video_queue_mark_done_and_failed(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    t1 = q.enqueue("a")
    q.pop_next()
    q.mark_done(t1, {"out": str(tmp_path / "a.mp4"), "bytes": 123})
    cur = q.get(t1)
    assert cur is not None and cur.status == "done"
    assert cur.result == {"out": str(tmp_path / "a.mp4"), "bytes": 123}

    t2 = q.enqueue("b")
    q.pop_next()
    q.mark_failed(t2, "boom")
    cur2 = q.get(t2)
    assert cur2 is not None and cur2.status == "failed"
    assert cur2.error == "boom"


def test_video_queue_status_lists_tasks(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    t1 = q.enqueue("first", {"model": "m"}, out=str(tmp_path / "a.mp4"), description="视频生成: first")
    t2 = q.enqueue("second", {"model": "m"})
    rows = q.status()
    assert len(rows) == 2
    by_id = {r["task_id"]: r for r in rows}
    assert by_id[t1]["status"] == "queued"
    assert by_id[t1]["attempts"] == 0
    assert by_id[t1]["max_attempts"] == MAX_ATTEMPTS
    assert by_id[t1]["out"] == str(tmp_path / "a.mp4")
    assert by_id[t1]["description"] == "视频生成: first"
    assert by_id[t2]["status"] == "queued"
    assert "error" in by_id[t1]


def test_video_queue_reset_running(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    t1 = q.enqueue("a")
    q.pop_next()
    assert q.get(t1).status == "running"
    assert q.reset_running() == 1
    assert q.get(t1).status == "queued"
    assert q.reset_running() == 0


def test_video_queue_has_pending(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    assert q.has_pending() is False
    q.enqueue("a")
    assert q.has_pending() is True
    q.pop_next()  # running
    assert q.has_pending() is False


# ── VideoScheduler 单元测试 ─────────────────────────────────────────────────


def _scheduler_with_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[VideoScheduler, VideoQueue]:
    q = VideoQueue(base_dir=tmp_path)
    scheduler = VideoScheduler()
    monkeypatch.setattr(scheduler, "_queue", q)
    return scheduler, q


@pytest.mark.asyncio
async def test_scheduler_on_fail_requeues_below_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    bus = _FakeEventBus()
    monkeypatch.setattr(scheduler, "_event_bus", bus)
    tid = q.enqueue("a", {"model": "m"})
    task = q.pop_next()
    assert task is not None and task.attempts == 0
    await scheduler._on_fail(task, "submit error")
    cur = q.get(tid)
    assert cur is not None and cur.status == "queued"
    assert cur.attempts == 1
    assert cur.error == "submit error"
    assert bus.events == []  # requeue 不通知


@pytest.mark.asyncio
async def test_scheduler_on_fail_marks_failed_at_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    bus = _FakeEventBus()
    monkeypatch.setattr(scheduler, "_event_bus", bus)
    tid = q.enqueue("a", {"model": "m"})
    q.requeue_tail(tid, "e1")
    q.requeue_tail(tid, "e2")
    task = q.pop_next()
    assert task is not None and task.attempts == MAX_ATTEMPTS - 1
    await scheduler._on_fail(task, "final error")
    cur = q.get(tid)
    assert cur is not None and cur.status == "failed"
    assert cur.error == "final error"
    assert len(bus.events) == 1
    payload = bus.events[0].payload
    assert payload["status"] == "failed"
    assert payload["has_error"] is True
    assert payload["error"] == "final error"


@pytest.mark.asyncio
async def test_scheduler_on_fail_cancel_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务已被取消：_on_fail 不覆盖终态、不通知。"""
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    bus = _FakeEventBus()
    monkeypatch.setattr(scheduler, "_event_bus", bus)
    tid = q.enqueue("a", {"model": "m"})
    task = q.pop_next()
    q.cancel(tid)
    await scheduler._on_fail(task, "err")
    cur = q.get(tid)
    assert cur is not None and cur.status == "cancelled"
    assert bus.events == []


@pytest.mark.asyncio
async def test_scheduler_poll_until_done_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    bus = _FakeEventBus()
    monkeypatch.setattr(scheduler, "_event_bus", bus)
    out = tmp_path / "o.mp4"
    tid = q.enqueue("a", {"model": "m"}, out=str(out))
    task = q.pop_next()

    async def fake_poll(client, video_id, **kwargs):
        return {"status": "completed", "video_url": "https://example.com/o.mp4"}

    async def fake_download(client, url, out_path):
        out_path.write_bytes(b"mp4")
        return 3

    monkeypatch.setattr(providers, "agnes_poll_video", fake_poll)
    monkeypatch.setattr(providers, "download_file", fake_download)
    await scheduler._poll_until_done(task, "vid-1", _CancelFlag())
    cur = q.get(tid)
    assert cur is not None and cur.status == "done"
    assert cur.result == {"out": str(out), "bytes": 3}
    assert len(bus.events) == 1
    assert bus.events[0].payload["status"] == "completed"


@pytest.mark.asyncio
async def test_scheduler_poll_until_done_cancel_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成完成但任务已被取消：不覆盖为 done、不推送完成事件。"""
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    bus = _FakeEventBus()
    monkeypatch.setattr(scheduler, "_event_bus", bus)
    out = tmp_path / "o.mp4"
    tid = q.enqueue("a", {"model": "m"}, out=str(out))
    task = q.pop_next()
    q.cancel(tid)

    async def fake_poll(client, video_id, **kwargs):
        return {"status": "completed", "video_url": "https://example.com/o.mp4"}

    async def fake_download(client, url, out_path):
        out_path.write_bytes(b"mp4")
        return 3

    monkeypatch.setattr(providers, "agnes_poll_video", fake_poll)
    monkeypatch.setattr(providers, "download_file", fake_download)
    await scheduler._poll_until_done(task, "vid-1", _CancelFlag())
    cur = q.get(tid)
    assert cur is not None and cur.status == "cancelled"
    assert bus.events == []


@pytest.mark.asyncio
async def test_scheduler_cancel_aborts_in_flight_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel(task_id) must abort the running poll signal, not only mark JSON."""
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    tid = q.enqueue("a", {"model": "m"}, out=str(tmp_path / "a.mp4"))
    task = q.pop_next()
    assert task is not None
    flag = _CancelFlag()
    scheduler._cancel_flags[tid] = flag
    saw_signal: dict[str, object] = {}

    async def fake_poll(client, video_id, **kwargs):
        saw_signal["signal"] = kwargs.get("signal")
        import asyncio

        for _ in range(40):
            sig = kwargs.get("signal")
            if sig is not None and getattr(sig, "aborted", False):
                raise InterruptedError("已取消")
            await asyncio.sleep(0.01)
        raise AssertionError("poll was not aborted")

    monkeypatch.setattr(providers, "agnes_poll_video", fake_poll)
    poll = asyncio.create_task(scheduler._poll_until_done(task, "vid-x", flag))
    scheduler._active_polls[tid] = poll
    await asyncio.sleep(0.02)
    assert scheduler.cancel(tid) is True
    assert flag.aborted is True
    await poll
    cur = q.get(tid)
    assert cur is not None and cur.status == "cancelled"


@pytest.mark.asyncio
async def test_scheduler_tick_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a poll is recorded as active, _tick_once must not pop another job."""
    scheduler, q = _scheduler_with_queue(tmp_path, monkeypatch)
    q.enqueue("a", {"model": "m"})
    q.enqueue("b", {"model": "m"})
    # Pretend a poll is still running.
    hang = asyncio.get_running_loop().create_future()
    scheduler._active_polls["vq-busy"] = hang  # type: ignore[assignment]
    started = await scheduler._tick_once()
    assert started is False
    assert q.get(q.status()[0]["task_id"]).status == "queued"  # nothing popped
    hang.cancel()
    scheduler._active_polls.clear()


def test_runtime_root_prefers_config_coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue must follow config coara_home, not only process env."""
    home = tmp_path / "configured-home"
    home.mkdir()
    monkeypatch.delenv("COARA_HOME", raising=False)

    class _CM:
        _raw_config = {"coara_home": str(home)}

    monkeypatch.setattr("src.core.config.config_manager", _CM())
    assert _runtime_root() == (home.resolve() / "runtime")


def test_video_queue_prunes_old_terminals(tmp_path: Path) -> None:
    q = VideoQueue(base_dir=tmp_path)
    ids: list[str] = []
    for i in range(_MAX_TERMINAL_RECORDS + 5):
        tid = q.enqueue(f"p{i}")
        ids.append(tid)
        q.pop_next()
        q.mark_done(tid, {"out": f"{i}.mp4", "bytes": 1})
    remaining = list((tmp_path / "video_queue").glob("*.json"))
    assert len(remaining) == _MAX_TERMINAL_RECORDS


# ── providers：单次提交 / 队列满识别 / gate ────────────────────────────────


def test_is_video_queue_full_detects_agnes_body() -> None:
    body = '{"code":"video_queue_full","message":"video queue is full, please retry later"}'
    assert providers._is_video_queue_full(503, body)
    assert not providers._is_video_queue_full(500, "internal error")
    assert not providers._is_video_queue_full(503, "service unavailable")


@pytest.mark.asyncio
async def test_agnes_create_video_single_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """agnes_create_video 已改为单次提交，不重试。"""
    calls = {"n": 0}

    async def fake_post(client, url, **kwargs):
        calls["n"] += 1
        return {"video_id": "vid-ok"}

    monkeypatch.setattr(providers, "_post_json", fake_post)
    monkeypatch.setattr(providers, "agnes_key", lambda: "k")
    monkeypatch.setattr(providers, "resolve_agnes_base", lambda: "https://example.test/v1")

    class _Client:
        pass

    video_id = await providers.agnes_create_video(
        _Client(),  # type: ignore[arg-type]
        prompt="x",
        model="agnes-video-2.5-flash",
        seconds="5",
    )
    assert video_id == "vid-ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_agnes_create_video_submit_failure_propagates(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """单次提交失败直接抛出（交由 scheduler 重排），这里只测 submit 不再内部重试。"""
    async def boom(client, url, **kwargs):
        raise providers.VideoQueueFullError("full")

    monkeypatch.setattr(providers, "_post_json", boom)
    monkeypatch.setattr(providers, "agnes_key", lambda: "k")
    monkeypatch.setattr(providers, "resolve_agnes_base", lambda: "https://example.test/v1")

    class _Client:
        pass

    with pytest.raises(providers.VideoQueueFullError):
        await providers.agnes_create_video(_Client(), prompt="x", model="m")
