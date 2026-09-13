"""Tests for the workspace protocol (ws.md) mechanism.

Covers:
- protocol loading / env-seed injection (M1)
- conversation-detection whitelist for seed+protocol messages (M1)
- idle watcher expired scan, dedup, resting-workspace skip (M3)
- janitor renew race check and foreground trace (M3)
- startup catch-up scan (M3)
- manual /new maintenance pre-step (M4)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.coara.injections.environment_injector import (
    WS_PROTOCOL_PREFIX,
    build_environment_seed_messages,
    load_workspace_protocol,
)
from src.coara.root import RootCoara
from src.coara.workspace_state import workspace_session_has_conversation
from src.core.coara_home import workspace_id_for
from src.core.config import config_manager
from src.core.types import Message, MessageRole
from src.llm.registry import provider_registry
from tests.helpers import FakeProvider


@pytest.fixture(autouse=True)
def _restore_janitor_maintenance():
    """_root_for_maintenance 直接覆写 janitor_maintenance 模块属性；用例间恢复原实现。"""
    import src.coara.janitor_maintenance as jm

    original = jm.run_janitor_maintenance
    yield
    jm.run_janitor_maintenance = original


@pytest.fixture
async def root(tmp_path: Path):
    """Minimal RootCoara — no initialize(); idle watcher only."""
    from src.coara.workspace_protocol import reset_janitor_flights_for_tests

    reset_janitor_flights_for_tests()
    await config_manager.load()
    provider_registry.register("fake-wsproto", FakeProvider([]))
    try:
        r = RootCoara(workspace_dir=tmp_path, provider_name="fake-wsproto")
        yield r
    finally:
        await r.stop_idle_timeout_watcher()
        reset_janitor_flights_for_tests()


def _fake_coara(tmp_path: Path, *, conversation: bool = True):
    messages = [Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x")]
    if conversation:
        messages.append(Message(role=MessageRole.USER, content="帮我看看这个项目"))
    return SimpleNamespace(
        workspace_dir=str(tmp_path),
        has_active_turn=lambda: False,
        start_new_session=async_fake_start,
        persist_session_to_disk=lambda: None,
        message_history=messages,
        session_id="fake-sid",
        get_status=lambda: {"errors_log_path": ""},
    )


async def async_fake_start(*, interrupt_source: str = "new_session") -> str:
    return "fake-new-session-id"


# ---------------------------------------------------------------------------
# M1 protocol loading & env-seed injection
# ---------------------------------------------------------------------------


def test_load_workspace_protocol_missing(tmp_path: Path) -> None:
    assert load_workspace_protocol(tmp_path) == ""


def test_load_workspace_protocol_blank(tmp_path: Path) -> None:
    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("   \n", encoding="utf-8")
    assert load_workspace_protocol(tmp_path) == ""


def test_load_workspace_protocol_reads_file(tmp_path: Path) -> None:
    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n这是测试空间\n", encoding="utf-8")
    assert load_workspace_protocol(tmp_path) == "# 定位\n这是测试空间"


def test_load_workspace_protocol_truncates_oversize(tmp_path: Path) -> None:
    (tmp_path / ".coara").mkdir()
    big = "# big\n" + ("x" * (34 * 1024))
    (tmp_path / ".coara" / "ws.md").write_text(big, encoding="utf-8")
    loaded = load_workspace_protocol(tmp_path)
    assert "已截断" in loaded
    assert len(loaded.encode("utf-8")) < 34 * 1024


def test_env_seed_injects_protocol(tmp_path: Path) -> None:
    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n测试空间\n", encoding="utf-8")
    messages = build_environment_seed_messages(tmp_path)
    assert len(messages) >= 2
    ws = next(m for m in messages if WS_PROTOCOL_PREFIX in m.content)
    assert "测试空间" in ws.content
    env = next(m for m in messages if "环境上下文：" in m.content)
    assert WS_PROTOCOL_PREFIX not in env.content


def test_env_seed_placeholder_without_protocol(tmp_path: Path) -> None:
    messages = build_environment_seed_messages(tmp_path)
    ws = next(m for m in messages if WS_PROTOCOL_PREFIX in m.content)
    assert WS_PROTOCOL_PREFIX == "工作空间概况：\n"
    assert "生成中" in ws.content


def test_seed_with_protocol_not_counted_as_conversation(tmp_path: Path) -> None:
    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n测试空间\n", encoding="utf-8")
    seeds = build_environment_seed_messages(tmp_path)
    coara = SimpleNamespace(message_history=seeds)
    assert not workspace_session_has_conversation(coara)


# ---------------------------------------------------------------------------
# has_real_conversation_in_history
# ---------------------------------------------------------------------------


def _write_history(history_path: Path, messages: list[dict[str, Any]], *, workspace: Path | None = None) -> None:
    """事件溯源改造后：会话底稿为 session_events.jsonl（消息经 recorder 事件化）。

    ``history_path`` 是期望的事件文件路径；实际写入位置由
    ``resolve_session_log_path(workspace, coara_home)`` 决定（coara_home 缺省
    从环境解析）。测试统一显式传 workspace；coara_home 取
    ``history_path.parent.parent``（``<coara_home>/workspaces/<id>/`` 布局）或
    workspace 本身（扁平布局）。
    """
    from src.core.types import Message, MessageRole
    from src.session_log.recorder import SessionLogRecorder
    from src.session_log.store import resolve_session_log_path

    assert workspace is not None, "事件化 seed 需要显式 workspace 路径"
    # 布局推导：history_path 形如 <coara_home>/workspaces/<id>/session_events.jsonl
    # 或扁平 <dir>/session_events.jsonl（此时事件文件直接落在该目录）。
    candidate = history_path.parent.parent
    if resolve_session_log_path(workspace, coara_home=candidate) == history_path:
        coara_home = candidate
    elif resolve_session_log_path(workspace, coara_home=history_path.parent) == history_path:
        coara_home = history_path.parent
    else:
        # 直接落到期望路径（扁平布局）：手写 append
        from src.session_log.store import append_events
        from src.session_log.types import message_to_event

        history_path.parent.mkdir(parents=True, exist_ok=True)
        role_map_ = {"user": MessageRole.USER, "assistant": MessageRole.ASSISTANT, "tool": MessageRole.TOOL}
        msgs = [
            Message(role=role_map_.get(str(m.get("role")), MessageRole.USER), content=m.get("content", ""))
            for m in messages
        ]
        events = [message_to_event(message=msg, seq=i + 1, session_id="s1") for i, msg in enumerate(msgs)]
        events = [e for e in events if e is not None]
        append_events(history_path, events)
        assert history_path.is_file()
        return
    recorder = SessionLogRecorder(
        workspace_dir=workspace,
        session_id="s1",
        coara_home=coara_home,
    )
    role_map = {"user": MessageRole.USER, "assistant": MessageRole.ASSISTANT, "tool": MessageRole.TOOL}
    history = [
        Message(role=role_map.get(str(m.get("role")), MessageRole.USER), content=m.get("content", "")) for m in messages
    ]
    recorder.sync_history(history)
    assert history_path.is_file(), f"事件应写入 {history_path}"


def test_history_only_seed_not_conversation(tmp_path: Path) -> None:
    from src.coara.workspace_protocol import has_real_conversation_in_history

    hp = tmp_path / "session_events.jsonl"
    _write_history(hp, [{"role": "user", "content": "环境上下文：\n- 今天日期：xxx"}], workspace=tmp_path)
    assert not has_real_conversation_in_history(str(hp))


def test_history_with_user_text_is_conversation(tmp_path: Path) -> None:
    from src.coara.workspace_protocol import has_real_conversation_in_history

    hp = tmp_path / "session_events.jsonl"
    _write_history(
        hp,
        [
            {"role": "user", "content": "环境上下文：\n- 今天日期：xxx"},
            {"role": "user", "content": "帮我干活"},
        ],
        workspace=tmp_path,
    )
    # jsonl 跨会话共存：判定必须限定 session_id（生产调用方一律传 sid）
    assert has_real_conversation_in_history(str(hp), session_id="s1")


def test_history_missing_not_conversation(tmp_path: Path) -> None:
    from src.coara.workspace_protocol import has_real_conversation_in_history

    assert not has_real_conversation_in_history(str(tmp_path / "nope.json"))


@pytest.mark.asyncio
async def test_dispatch_janitor_awaits_execute() -> None:
    """机制化：dispatch 调 run_janitor_maintenance 起维护流程，返回语义化 task_id。

    对应旧「必须 await invocation.execute()」回归——新实现没有 delegate
    invocation；同意图改为断言：派发走内核维护流程（run_janitor_maintenance
    被调用、prompt 信封含概况路径）、task 被实际 await（done 回调后 flight 清出）。
    """
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    calls: list[dict[str, Any]] = []

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        calls.append(
            {
                "workspace_name": workspace_name,
                "workspace_dir": workspace_dir,
                "coara_home": coara_home,
                "prompt": prompt,
            }
        )

        async def _run() -> None:
            return None

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    task_id = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws1",
        workspace_dir="/tmp/ws1",
        coara_home="/tmp/home",
    )
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    # 信封只带动态参数（概况路径）：职责规则已随 janitor.md 注入，不再复述
    assert "概况文件" in prompt and "ws.md" in prompt
    assert "任务类型" not in prompt
    assert "触发" not in prompt
    # 语义化 task_id（不再是 sa-janitor-xxx）
    assert task_id is not None
    assert task_id.startswith("janitor-")
    assert not task_id.startswith("sa-")
    # task 被挂进 flight；完成后 done 回调把 flight 清出（dispatch 不等待干活）
    wid = workspace_id_for("/tmp/ws1")
    assert wp._janitor_flights[wid]["task_id"] == task_id
    for _ in range(50):
        await asyncio.sleep(0.02)
        if wid not in wp._janitor_flights:
            break
    assert wid not in wp._janitor_flights
    wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_dispatch_coalesces_when_already_running(monkeypatch, tmp_path: Path) -> None:
    from src.coara.workspace_protocol import (
        _janitor_flights,
        dispatch_janitor_background,
        reset_janitor_flights_for_tests,
    )
    from src.core.coara_home import workspace_id_for

    reset_janitor_flights_for_tests()
    monkeypatch.setattr(
        "src.coara.workspace_protocol._janitor_min_interval", lambda: 0.0
    )  # 冷却闸门不影响 dirty 补跑断言
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    builds = 0
    blocker = asyncio.Event()

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        nonlocal builds
        builds += 1

        async def _run() -> None:
            await blocker.wait()

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    tid1 = await dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
    )
    assert tid1 is not None and tid1.startswith("janitor-")
    assert builds == 1

    # 在飞（task 未 done）→ 合并：同一 task_id、标 dirty、不起第二轮
    tid2 = await dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
    )
    assert tid2 == tid1
    assert builds == 1
    wid = workspace_id_for(str(ws_dir))
    assert _janitor_flights[wid]["dirty"] is True

    # 收尾：放 task 完成，done 回调会 dirty 补跑一轮（再次调维护流程）
    blocker.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if builds >= 2:
            break
    assert builds == 2
    reset_janitor_flights_for_tests()


def test_janitor_hidden_from_llm_subagent_list() -> None:
    from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES, get_enabled_subagents, get_subagent

    assert "janitor" in SYSTEM_ONLY_SUBAGENT_TYPES
    names = {sa.name for sa in get_enabled_subagents(None)}
    assert "janitor" not in names
    assert "coaras" in names
    assert "explore" not in names
    # janitor 不是 agent：不在注册表，经 janitor_maintenance 直读 prompts 装配
    assert get_subagent("janitor") is None


def test_build_janitor_prompt_carries_kind(tmp_path: Path) -> None:
    """janitor 信封：动态参数 + janitor.md 同条 <系统提醒>。"""
    from src.coara.workspace_protocol import build_janitor_prompt

    p = build_janitor_prompt(workspace_dir=str(tmp_path), kind="code")
    assert "概况文件" in p
    assert "空间性质：code" in p
    assert p.count("<系统提醒>") == 1
    assert p.rstrip().endswith("</系统提醒>")
    assert "维护当前工作空间概况" in p or "工作空间概况" in p

    p2 = build_janitor_prompt(workspace_dir=str(tmp_path))
    assert "空间性质" not in p2
    assert p2.count("<系统提醒>") == 1


def test_workspace_kind_reads_registry() -> None:
    """_workspace_kind：读注册表 entry.kind；无管理器/无条目/异常一律空串。"""
    from src.coara.workspace_protocol import _workspace_kind
    from src.workspace.types import WorkspaceKind

    entry = SimpleNamespace(kind=WorkspaceKind.CODE)
    wm = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda _wid: entry))
    assert _workspace_kind(SimpleNamespace(workspace_manager=wm), "/tmp/ws") == "code"

    assert _workspace_kind(SimpleNamespace(workspace_manager=None), "/tmp/ws") == ""
    wm_none = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda _wid: None))
    assert _workspace_kind(SimpleNamespace(workspace_manager=wm_none), "/tmp/ws") == ""

    class _BoomRegistry:
        def get_by_id(self, _wid):
            raise RuntimeError("boom")

    wm_boom = SimpleNamespace(registry=_BoomRegistry())
    assert _workspace_kind(SimpleNamespace(workspace_manager=wm_boom), "/tmp/ws") == ""


@pytest.mark.asyncio
async def test_dispatch_janitor_prompt_carries_workspace_kind() -> None:
    """dispatch 从注册表读 kind 传入 janitor 信封：code 空间 janitor 据此维护目录模块。"""
    from src.coara import workspace_protocol as wp
    from src.workspace.types import WorkspaceKind

    wp.reset_janitor_flights_for_tests()
    prompts: list[str] = []

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        prompts.append(prompt)

        async def _run() -> None:
            return None

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    root.workspace_manager = SimpleNamespace(
        registry=SimpleNamespace(get_by_id=lambda _wid: SimpleNamespace(kind=WorkspaceKind.CODE))
    )
    await wp.dispatch_janitor_background(
        root,
        workspace_name="ws1",
        workspace_dir="/tmp/ws1",
        coara_home="/tmp/home",
    )
    assert prompts and "空间性质：code" in prompts[0]
    wp.reset_janitor_flights_for_tests()


def test_janitor_persona_inherits_target_static_prompt(tmp_path: Path) -> None:
    """前缀一致性回归：janitor persona 模板 = 目标空间会话静态提示词（逐字节）。

    janitor.md 只进本轮 <系统提醒> 信封，绝不进系统提示词。
    父会话静态提示词缺失时回退 janitor.yaml 原配置。
    """
    from src.coara import janitor_maintenance as jm

    cfg = jm._janitor_config()
    assert cfg is not None

    ws_dir = str(tmp_path / "ws")
    parent = SimpleNamespace(_static_prompt_cache={})
    sentinel = "STATIC-PROMPT-OF-TARGET-SESSION"

    def _build() -> str:
        parent._static_prompt_cache["static"] = sentinel
        return sentinel

    parent._build_system_prompt = _build
    root = SimpleNamespace(_sessions={workspace_id_for(ws_dir): SimpleNamespace(coara=parent)})

    template, yaml_config = jm._target_static_prompt(root, ws_dir, cfg)
    assert template == sentinel
    if yaml_config is not None:
        assert yaml_config.system_prompt == sentinel

    # 无活会话：回退原配置（不抛错）
    template2, _ = jm._target_static_prompt(SimpleNamespace(_sessions={}), ws_dir, cfg)
    assert template2 == cfg.system_prompt


def test_build_janitor_prompt_is_single_system_reminder(tmp_path: Path) -> None:
    """职责说明已并进 build_janitor_prompt，一条 <系统提醒>，无身份句。"""
    from src.coara.workspace_protocol import build_janitor_prompt

    p = build_janitor_prompt(workspace_dir=str(tmp_path))
    assert "你现在是 janitor" not in p
    assert p.count("<系统提醒>") == 1
    assert "概况文件" in p
    assert p.rstrip().endswith("</系统提醒>")


@pytest.mark.asyncio
async def test_dispatch_resolves_janitor_space_last_run(monkeypatch) -> None:
    """provider/model：机制化后不再在 dispatch 层传参，改由维护流程内部解析。

    run_janitor_maintenance → _resolve_janitor_llm → 空间 last-run（entry 绑定）；
    janitor 专属配置已退役，不再读取。本测试直接锁该解析函数的两条路径。
    """
    from src.coara import janitor_maintenance as jm

    monkeypatch.setattr(
        "src.coara.workspace_protocol._workspace_entry_llm",
        lambda root, ws_dir: ("deepseek", "d1"),
    )
    provider, model = jm._resolve_janitor_llm(object(), "/tmp/ws")
    assert (provider, model) == ("deepseek", "d1")

    monkeypatch.setattr(
        "src.coara.workspace_protocol._workspace_entry_llm",
        lambda root, ws_dir: (None, None),
    )
    provider, model = jm._resolve_janitor_llm(object(), "/tmp/ws")
    assert (provider, model) == (None, None)


@pytest.mark.asyncio
async def test_dispatch_janitor_swallows_metadata_errors() -> None:
    """装配失败（run_janitor_maintenance 返回 None）时 dispatch 返回 None 且不写 flight。

    旧语义（delegate metadata 读取失败被吞）随机制化消失——新实现没有 delegate
    invocation/metadata；同意图保留为「派发失败路径干净失败」。
    """
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()

    def _fail_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        return None

    root = _root_for_maintenance(_fail_run)
    assert (
        await wp.dispatch_janitor_background(
            root,
            workspace_name="ws1",
            workspace_dir="/tmp/ws1",
            coara_home="/tmp/home",
        )
        is None
    )
    assert wp._janitor_flights == {}
    wp.reset_janitor_flights_for_tests()


# ---------------------------------------------------------------------------
# M3 expired scan
# ---------------------------------------------------------------------------


async def _fake_dispatch(calls: list[dict[str, Any]], *a: Any, **kw: Any) -> str:
    calls.append(kw)
    return "task-1"


async def test_scan_expired_dispatches_and_dedups(root, tmp_path, monkeypatch) -> None:
    (tmp_path / ".coara").mkdir(exist_ok=True)
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n测试\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-1"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="ws1")
    epoch = time.time() - 10_000.0
    root._workspace_activity_at[ws_id] = epoch

    await root._janitor_scan_expired(7200.0)
    assert len(calls) == 1
    assert calls[0]["workspace_name"] == "ws1"
    assert root._janitor_pending[ws_id] == ("task-1", epoch)

    # Same epoch again -> no second dispatch.
    await root._janitor_scan_expired(7200.0)
    assert len(calls) == 1


async def test_scan_expired_already_maintained_arms_renew_without_redispatch(root, tmp_path, monkeypatch) -> None:
    """压缩路径已维护过的 epoch：空闲扫描只武装 /new，不再派一模一样的 janitor。"""
    (tmp_path / ".coara").mkdir(exist_ok=True)
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n测试\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-maintained"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="ws1")
    epoch = time.time() - 10_000.0
    root._workspace_activity_at[ws_id] = epoch
    root._janitor_activity_at[ws_id] = epoch

    await root._janitor_scan_expired(7200.0)
    assert calls == []
    assert root._janitor_pending[ws_id] == ("maintained", epoch)

    renewed: list[tuple[str, float]] = []

    async def _fake_renew(wid: str, ep: float) -> None:
        renewed.append((wid, ep))

    monkeypatch.setattr(root, "_janitor_maybe_renew", _fake_renew)
    await root._janitor_finalize_pending()
    assert renewed == [(ws_id, epoch)]
    assert ws_id not in root._janitor_pending


async def test_record_turn_activity_inherits_janitor_marker(root, tmp_path, monkeypatch) -> None:
    """turn_end 顺延已维护标记，避免压缩 janitor 后空闲再派同一段对话。"""
    from src.coara.workspace_protocol import load_janitor_activity, save_janitor_activity

    home = tmp_path / "home"
    home.mkdir()
    root.workspace_manager = SimpleNamespace(coara_home=home)
    ws_id = "ws-inherit"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="ws1")
    prev = time.time() - 100.0
    root._workspace_activity_at[ws_id] = prev
    root._janitor_activity_at[ws_id] = prev
    save_janitor_activity(str(tmp_path), str(home), prev)

    root.record_turn_activity(ws_id, push_status=False)
    new_at = root._workspace_activity_at[ws_id]
    assert new_at > prev
    assert root._janitor_activity_at[ws_id] == new_at
    assert load_janitor_activity(str(tmp_path), str(home)) == new_at


async def test_scan_expired_first_generation_when_no_ws_md(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-first"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="ws-first")
    root._workspace_activity_at[ws_id] = time.time() - 10_000.0

    await root._janitor_scan_expired(7200.0)
    assert len(calls) == 1


async def test_scan_expired_skips_resting_workspace(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-rest"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path, conversation=False), workspace_name="rest")
    root._workspace_activity_at[ws_id] = time.time() - 10_000.0

    await root._janitor_scan_expired(7200.0)
    assert calls == []


async def test_scan_expired_skips_not_stale(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-fresh"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="fresh")
    root._workspace_activity_at[ws_id] = time.time()

    await root._janitor_scan_expired(7200.0)
    assert calls == []


async def test_scan_expired_skips_pending(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    ws_id = "ws-pending"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="pending")
    root._workspace_activity_at[ws_id] = time.time() - 10_000.0
    root._janitor_pending[ws_id] = ("task-x", root._workspace_activity_at[ws_id])

    await root._janitor_scan_expired(7200.0)
    assert calls == []


# ---------------------------------------------------------------------------
# M3 renew race check
# ---------------------------------------------------------------------------


async def test_renew_skipped_when_epoch_changed(root, tmp_path) -> None:
    ws_id = "ws-race"
    started: list[bool] = []
    coara = _fake_coara(tmp_path)
    coara.start_new_session = async_fake_start
    original_start = coara.start_new_session

    async def record_start(*, interrupt_source: str = "new_session") -> str:
        started.append(True)
        return await original_start(interrupt_source=interrupt_source)

    coara.start_new_session = record_start
    root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="race")
    root._workspace_activity_at[ws_id] = 1000.0

    # Epoch changed (user resumed) -> no renew.
    root._workspace_activity_at[ws_id] = 9999.0
    await root._janitor_maybe_renew(ws_id, 1000.0)
    assert started == []


async def test_renew_happens_when_epoch_unchanged(root, tmp_path) -> None:
    ws_id = "ws-renew"
    started: list[bool] = []
    coara = _fake_coara(tmp_path)
    original_start = coara.start_new_session

    async def record_start(*, interrupt_source: str = "new_session") -> str:
        started.append(True)
        return await original_start(interrupt_source=interrupt_source)

    coara.start_new_session = record_start
    root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="renew")
    root._workspace_activity_at[ws_id] = 1000.0

    await root._janitor_maybe_renew(ws_id, 1000.0)
    assert started == [True]


async def test_renew_foreground_emits_trace(root, tmp_path) -> None:
    ws_id = "ws-fg"
    coara = _fake_coara(tmp_path)
    root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="fg")
    root._workspace_activity_at[ws_id] = 1000.0
    root._foreground_session_id = ws_id

    recorded: list[Any] = []
    root.set_trace_sink(lambda event: recorded.append(event))

    await root._janitor_maybe_renew(ws_id, 1000.0)
    assert any(e.event_type == "session_auto_new" for e in recorded)


async def test_renew_foreground_pushes_session_event(root, tmp_path) -> None:
    """Idle auto-renew of the FOREGROUND session must surface session_started so the
    Matrix subscription layer pushes new_session to the phone (janitor path goes
    through the same event, not a direct push_status_payload call)."""
    pushes: list[dict[str, Any]] = []
    from src.coara.mobile_sync import push_status_for_session_started

    def _fake_push(
        r: Any, *, force: bool = False, fallback_room_id: str = "", session_event: str | None = None
    ) -> None:
        pushes.append({"force": force, "session_event": session_event})

    import src.coara.mobile_sync as mobile_sync

    original_push = mobile_sync.push_status_payload
    mobile_sync.push_status_payload = _fake_push
    try:
        ws_id = "ws-fg-push"
        recorded: list[Any] = []
        root.set_trace_sink(lambda event: recorded.append(event))
        coara = _fake_coara(tmp_path)

        # 真实 coara.start_new_session 末尾 emit session_started trace；假 coara 补上
        # 这一步，订阅回调（与 Matrix bot._on_session_started 同一入口）才能收到事件。
        async def _start_with_trace(*, interrupt_source: str = "new_session") -> str:
            sid = await async_fake_start(interrupt_source=interrupt_source)
            root._emit_trace(
                "session_started",
                "New session started",
                payload={
                    "session_id": coara.session_id,
                    "interrupt_source": interrupt_source,
                    "workspace_id": ws_id,
                },
            )
            return sid

        coara.start_new_session = _start_with_trace
        root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="fg-push")
        root._workspace_activity_at[ws_id] = 1000.0
        root._foreground_session_id = ws_id

        await root._janitor_maybe_renew(ws_id, 1000.0)
        # 订阅层路径：session_started trace → push_status_for_session_started → push_status_payload
        for event in recorded:
            if event.event_type == "session_started":
                push_status_for_session_started(root, event, fallback_room_id="")
        assert pushes == [{"force": True, "session_event": "new_session"}]
    finally:
        mobile_sync.push_status_payload = original_push


async def test_renew_background_workspace_does_not_push(root, tmp_path, monkeypatch) -> None:
    """Non-foreground renewals renew silently (phone follows foreground only)."""
    pushes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.mobile_sync.push_status_payload",
        lambda r, *, force=False, fallback_room_id="", session_event=None: pushes.append(session_event),
    )
    ws_id = "ws-bg"
    coara = _fake_coara(tmp_path)
    root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="bg")
    root._workspace_activity_at[ws_id] = 1000.0
    root._foreground_session_id = "ws-other"

    await root._janitor_maybe_renew(ws_id, 1000.0)
    assert pushes == []


# ---------------------------------------------------------------------------
# M3 startup catch-up scan
# ---------------------------------------------------------------------------


def _fake_registry_entry(ws_id: str, name: str, ws_dir: Path):
    return SimpleNamespace(
        id=ws_id,
        name=name,
        status="active",
        resolved_path=lambda: ws_dir,
    )


class _FakeRegistry:
    def __init__(self, entries: list[Any]):
        self._entries = entries

    def list_active(self) -> list[Any]:
        return self._entries


class _FakeWorkspaceManager:
    def __init__(self, entries: list[Any], coara_home: Path):
        self.registry = _FakeRegistry(entries)
        self.coara_home = coara_home


async def test_startup_scan_dispatches_stale_with_history(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    home = tmp_path / "coara-home"
    ws_dir = tmp_path / "ws-a"
    ws_dir.mkdir()
    ws_id = "ws-a-id"
    root.workspace_manager = _FakeWorkspaceManager([_fake_registry_entry(ws_id, "ws-a", ws_dir)], home)

    from src.coara.workspace_protocol import session_history_path
    from src.coara.workspace_state import save_session_state

    (ws_dir / ".coara").mkdir(exist_ok=True)
    (ws_dir / ".coara" / "ws.md").write_text("# 定位\n测试\n", encoding="utf-8")
    save_session_state(ws_dir, "s1", coara_home=home, last_updated=time.time() - 10_000.0)
    _write_history(
        Path(session_history_path(str(ws_dir), str(home))),
        [
            {"role": "user", "content": "环境上下文：\n- 今天日期：x"},
            {"role": "user", "content": "干活"},
        ],
        workspace=ws_dir,
    )

    await root._janitor_startup_scan()
    assert len(calls) == 1
    assert calls[0]["workspace_name"] == "ws-a"
    # Catch-up must not enqueue renew.
    assert root._janitor_pending == {}

    # Scan runs once.
    await root._janitor_startup_scan()
    assert len(calls) == 1


async def test_startup_scan_dedupes_across_restart(root, tmp_path, monkeypatch) -> None:
    """磁盘已维护标记只在 janitor 完成时写入：派发后进程死亡 → 重启 catch-up 重派；
    完成落标记后 → 重启不再重复派发。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    home = tmp_path / "coara-home"
    ws_dir = tmp_path / "ws-r"
    ws_dir.mkdir()
    ws_id = "ws-r-id"
    root.workspace_manager = _FakeWorkspaceManager([_fake_registry_entry(ws_id, "ws-r", ws_dir)], home)

    from src.coara.workspace_protocol import janitor_activity_path, save_janitor_activity, session_history_path
    from src.coara.workspace_state import save_session_state

    (ws_dir / ".coara").mkdir(exist_ok=True)
    (ws_dir / ".coara" / "ws.md").write_text("# 定位\n测试\n", encoding="utf-8")
    stale_at = time.time() - 10_000.0
    save_session_state(ws_dir, "s1", coara_home=home, last_updated=stale_at)
    _write_history(
        Path(session_history_path(str(ws_dir), str(home))),
        [
            {"role": "user", "content": "环境上下文：\n- 今天日期：x"},
            {"role": "user", "content": "干活"},
        ],
        workspace=ws_dir,
    )

    await root._janitor_startup_scan()
    assert len(calls) == 1
    assert calls[0]["activity_epoch"] == stale_at
    # 派发时只记内存单飞标记，磁盘不落已维护标记
    assert not janitor_activity_path(str(ws_dir), str(home)).exists()

    # 模拟进程在维护中途死亡：内存标记与启动扫描标志清零
    root._janitor_activity_at.clear()
    root._janitor_startup_scan_done = False
    await root._janitor_startup_scan()
    assert len(calls) == 2  # epoch 未落标记 → catch-up 重派，不再永久漏维护

    # 模拟 janitor 完成：完成钩子落磁盘标记
    save_janitor_activity(str(ws_dir), str(home), stale_at)
    root._janitor_activity_at.clear()
    root._janitor_startup_scan_done = False
    await root._janitor_startup_scan()
    assert len(calls) == 2  # 已维护的 epoch 不再重复派发


async def test_startup_scan_skips_resting(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    home = tmp_path / "coara-home"
    ws_dir = tmp_path / "ws-b"
    ws_dir.mkdir()
    ws_id = "ws-b-id"
    root.workspace_manager = _FakeWorkspaceManager([_fake_registry_entry(ws_id, "ws-b", ws_dir)], home)

    from src.coara.workspace_protocol import session_history_path
    from src.coara.workspace_state import save_session_state

    save_session_state(ws_dir, "s1", coara_home=home, last_updated=time.time() - 10_000.0)
    _write_history(
        Path(session_history_path(str(ws_dir), str(home))),
        [{"role": "user", "content": "环境上下文：\n- 今天日期：x"}],
        workspace=ws_dir,
    )

    await root._janitor_startup_scan()
    assert calls == []


async def test_startup_scan_skips_fresh(root, tmp_path, monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        lambda *a, **kw: _fake_dispatch(calls, *a, **kw),
    )
    home = tmp_path / "coara-home"
    ws_dir = tmp_path / "ws-c"
    ws_dir.mkdir()
    ws_id = "ws-c-id"
    root.workspace_manager = _FakeWorkspaceManager([_fake_registry_entry(ws_id, "ws-c", ws_dir)], home)

    from src.coara.workspace_state import save_session_state

    save_session_state(ws_dir, "s1", coara_home=home, last_updated=time.time())

    await root._janitor_startup_scan()
    assert calls == []


# ---------------------------------------------------------------------------
# M4 manual /new maintenance pre-step
# ---------------------------------------------------------------------------


async def test_new_runs_janitor_when_conversation(root, tmp_path, monkeypatch) -> None:
    """/new dispatches janitor in background and does not wait for it."""
    calls: list[dict[str, Any]] = []
    started = time.monotonic()

    async def _slow_dispatch(*_a, **kw):
        calls.append(kw)
        await asyncio.sleep(0.3)  # would block /new if awaited as full janitor
        return "sa-janitor-new"

    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        _slow_dispatch,
    )
    ws_id = "ws-new"
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="ws-new")
    root._foreground_session_id = ws_id
    root.workspace_manager = _FakeWorkspaceManager([], tmp_path / "coara-home")
    root._workspace_activity_at[ws_id] = time.time()
    monkeypatch.setattr(root, "foreground_active_name", lambda: "ws-new")

    from src.coara.commands.registry import CommandArgs
    from src.coara.commands.session import handle_new

    result = await handle_new(root, CommandArgs(name="new", parts=["new"], raw="/new"))
    elapsed = time.monotonic() - started
    assert calls, "janitor should be dispatched"
    assert result.action == "new_session"
    # Dispatch itself is awaited (scheduling), but must not wait for long janitor work.
    # Our mock sleeps 0.3s inside dispatch; that is the dispatch await — OK.
    # Critical: handle_new returns after dispatch, not after a multi-minute job.
    assert elapsed < 2.0


async def test_new_skips_janitor_without_conversation(root, tmp_path, monkeypatch) -> None:
    ran: list[bool] = []

    async def _dispatch(*_a, **_kw):
        ran.append(True)
        return "sa-x"

    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        _dispatch,
    )
    ws_id = "ws-new-rest"
    root._sessions[ws_id] = SimpleNamespace(
        coara=_fake_coara(tmp_path, conversation=False), workspace_name="ws-new-rest"
    )
    root._foreground_session_id = ws_id
    root.workspace_manager = _FakeWorkspaceManager([], tmp_path / "coara-home")
    monkeypatch.setattr(root, "foreground_active_name", lambda: "ws-new-rest")

    from src.coara.commands.registry import CommandArgs
    from src.coara.commands.session import handle_new

    await handle_new(root, CommandArgs(name="new", parts=["new"], raw="/new"))
    assert ran == []


# ---------------------------------------------------------------------------
# M5 cooldown (defer, never drop)
# ---------------------------------------------------------------------------


# 机制化后 dispatch 不再经 delegate：root stub 只需挂 run_janitor_maintenance 桩。
# 原 _root_for_delegate/_delegate_stub 已删——delegate 桩没有活引用了。


def _root_for_maintenance(
    run_maintenance: Any,
    *,
    sessions: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """root stub：patch janitor_maintenance.run_janitor_maintenance（源模块属性）。

    workspace_protocol.dispatch_janitor_background 体内每次调用都
    ``from src.coara.janitor_maintenance import run_janitor_maintenance``——
    该 import 从 janitor_maintenance 源模块取属性（不查 workspace_protocol 全局），
    所以绑定点在 janitor_maintenance 模块上。
    """
    import src.coara.janitor_maintenance as jm

    jm.run_janitor_maintenance = run_maintenance

    return SimpleNamespace(
        workspace_manager=None,
        foreground_coara=None,
        records_store=None,
        _sessions=sessions or {},
    )


@pytest.mark.asyncio
async def test_cooldown_defers_second_dispatch(monkeypatch, tmp_path: Path) -> None:
    """Trigger inside the cooldown window is deferred, not dropped; one timer only."""
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    monkeypatch.setattr(wp, "_janitor_min_interval", lambda: 600.0)
    captured: list[dict[str, Any]] = []
    builds = {"n": 0}

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        builds["n"] += 1
        captured.append({"prompt": prompt})

        async def _run() -> None:
            return None

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    ws_dir = str(tmp_path / "ws")

    tid1 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=ws_dir,
        coara_home=str(tmp_path / "home"),
    )
    assert tid1 is not None and tid1.startswith("janitor-")
    assert builds["n"] == 1
    wid = workspace_id_for(ws_dir)
    # 等第一轮 task 完成、flight 清出（完成很快），后续才走冷却判定
    for _ in range(50):
        await asyncio.sleep(0.02)
        if wid not in wp._janitor_flights:
            break

    tid2 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=ws_dir,
        coara_home=str(tmp_path / "home"),
    )
    assert tid2 is None
    assert builds["n"] == 1  # 冷却窗内不立即派发

    assert wid in wp._janitor_deferred

    # Another trigger inside the window: timer kept, payload refreshed.
    handle = wp._janitor_deferred[wid]["handle"]
    await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=ws_dir,
        coara_home=str(tmp_path / "home"),
        activity_epoch=1234.0,
    )
    assert wp._janitor_deferred[wid]["handle"] is handle
    assert wp._janitor_deferred[wid]["payload"]["activity_epoch"] == 1234.0

    # Cooldown elapsed -> deferred fire dispatches with the latest payload.
    wp._janitor_last_dispatch[wid] = time.monotonic() - 601.0
    wp._fire_deferred_dispatch(wid)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if builds["n"] >= 2:
            break
    assert builds["n"] == 2
    assert wid not in wp._janitor_deferred
    assert "概况文件" in captured[-1]["prompt"]
    wp.reset_janitor_flights_for_tests()


# ---------------------------------------------------------------------------
# M7 completion-time activity marking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_janitor_completion_writes_activity_marker(tmp_path: Path) -> None:
    """已维护 epoch 标记只在 janitor 完成时落盘（完成钩子路径）"""
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    home = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    epoch = time.time() - 10_000.0

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        async def _run() -> None:
            await asyncio.sleep(0.05)

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    tid = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(home),
        activity_epoch=epoch,
    )
    assert tid is not None and tid.startswith("janitor-")
    # 派发时标记不落盘
    assert wp.load_janitor_activity(str(ws_dir), str(home)) is None
    # 任务完成 → done 回调（call_soon 调度）落标记
    for _ in range(50):
        await asyncio.sleep(0.02)
        if wp.load_janitor_activity(str(ws_dir), str(home)) is not None:
            break
    assert wp.load_janitor_activity(str(ws_dir), str(home)) == epoch
    wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_janitor_failure_leaves_activity_unmarked(tmp_path: Path) -> None:
    """janitor 失败/取消不写已维护标记：重启 catch-up 兜底重派"""
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    home = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    epoch = time.time() - 10_000.0

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        async def _run() -> None:
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)
    tid = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(home),
        activity_epoch=epoch,
    )
    assert tid is not None
    # task 失败 → done 回调不落盘、记失败表（退避补派闸门）
    wid = workspace_id_for(str(ws_dir))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if wid in wp._janitor_failures:
            break
    assert wp.load_janitor_activity(str(ws_dir), str(home)) is None
    assert wp._janitor_failures.get(wid, (None, None))[0] == epoch
    wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_coalesce_refreshes_activity_epoch(monkeypatch, tmp_path: Path) -> None:
    """在飞合并时用更新的 epoch 覆盖：完成时按新 epoch 落标记"""
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    blocker = asyncio.Event()

    def _fake_run(root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        async def _run() -> None:
            await blocker.wait()

        return asyncio.create_task(_run())

    root = _root_for_maintenance(_fake_run)

    tid1 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
        activity_epoch=1000.0,
    )
    assert tid1 is not None and tid1.startswith("janitor-")

    # 在飞合并（task 未 done）：同一 task_id，epoch 被新值覆盖
    tid2 = await wp.dispatch_janitor_background(
        root,
        workspace_name="ws",
        workspace_dir=str(ws_dir),
        coara_home=str(tmp_path / "home"),
        activity_epoch=2000.0,
    )
    assert tid2 == tid1
    wid = workspace_id_for(str(ws_dir))
    assert wp._janitor_flights[wid]["activity_epoch"] == 2000.0
    blocker.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if wid not in wp._janitor_flights:
            break
    wp.reset_janitor_flights_for_tests()


# ---------------------------------------------------------------------------
# M6 overview splice into live session seed
# ---------------------------------------------------------------------------


def test_replace_overview_in_seed_swaps_only_overview(tmp_path: Path) -> None:
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX, replace_overview_in_seed
    from src.core.message_tags import system_info

    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n旧概况\n", encoding="utf-8")
    # Legacy combined seed (pre-split)
    seed = system_info(ENV_CONTEXT_PREFIX + "- 今天日期：x\n\n" + WS_PROTOCOL_PREFIX + "# 定位\n旧概况")
    assert "旧概况" in seed

    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n新概况\n", encoding="utf-8")
    new = replace_overview_in_seed(seed, tmp_path)
    assert new is not None
    assert "新概况" in new
    assert "旧概况" not in new
    # Env context part stays byte-identical (only the overview block changed).
    assert new.split(WS_PROTOCOL_PREFIX)[0] == seed.split(WS_PROTOCOL_PREFIX)[0]

    # Already current -> None (no rewrite, no persist, no trace).
    assert replace_overview_in_seed(new, tmp_path) is None
    # Not a seed -> None.
    assert replace_overview_in_seed("普通用户消息", tmp_path) is None


def test_replace_overview_in_seed_placeholder_to_content(tmp_path: Path) -> None:
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX, replace_overview_in_seed
    from src.core.message_tags import system_info

    seed = system_info(
        ENV_CONTEXT_PREFIX
        + "- 今天日期：x\n\n"
        + WS_PROTOCOL_PREFIX
        + "本空间工作空间概况生成中，可用工具自行探索了解本空间"
    )
    assert "生成中" in seed
    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n有了\n", encoding="utf-8")
    new = replace_overview_in_seed(seed, tmp_path)
    assert new is not None and "有了" in new and "生成中" not in new


@pytest.mark.asyncio
async def test_splice_session_overview_updates_history_and_persists(tmp_path: Path) -> None:
    from src.coara.workspace_protocol import _splice_session_overview

    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\nv1\n", encoding="utf-8")
    seeds = build_environment_seed_messages(tmp_path)
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\nv2\n", encoding="utf-8")

    persisted: list[bool] = []
    traces: list[str] = []
    coara = SimpleNamespace(
        message_history=[*seeds, Message(role=MessageRole.USER, content="开始干活")],
        persist_session_to_disk=lambda: persisted.append(True),
        _emit_trace=lambda event_type, *_a, **_kw: traces.append(event_type),
    )
    ok = await _splice_session_overview(coara, str(tmp_path))
    assert ok is True
    ws_msg = next(m for m in coara.message_history if WS_PROTOCOL_PREFIX in str(m.content))
    assert "v2" in ws_msg.content
    assert persisted == [True]
    assert traces == ["workspace_overview_refreshed"]

    # Second run: overview already current -> no-op.
    ok2 = await _splice_session_overview(coara, str(tmp_path))
    assert ok2 is False
    assert persisted == [True]


@pytest.mark.asyncio
async def test_splice_session_overview_skips_long_session(tmp_path: Path) -> None:
    from src.coara.workspace_protocol import OVERVIEW_REFRESH_MAX_MESSAGES, _splice_session_overview

    (tmp_path / ".coara").mkdir()
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\nv1\n", encoding="utf-8")
    seeds = build_environment_seed_messages(tmp_path)
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\nv2\n", encoding="utf-8")

    coara = SimpleNamespace(
        message_history=[*seeds]
        + [Message(role=MessageRole.USER, content=f"m{i}") for i in range(OVERVIEW_REFRESH_MAX_MESSAGES)],
        persist_session_to_disk=lambda: None,
        _emit_trace=lambda *a, **k: None,
    )
    assert await _splice_session_overview(coara, str(tmp_path)) is False
    ws_msg = next(m for m in coara.message_history if WS_PROTOCOL_PREFIX in str(m.content))
    assert "v1" in ws_msg.content


# ---------------------------------------------------------------------------
# M8 failure backoff retry (#334)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_janitor_failure_backoff_redispatch(root, tmp_path, monkeypatch) -> None:
    """失败后内存单飞标记不再永久堵死：退避窗口到期允许同 epoch 补派一次"""
    from src.coara import workspace_protocol as wp

    wp.reset_janitor_flights_for_tests()
    monkeypatch.setattr(wp, "_janitor_min_interval", lambda: 0.0)  # 退避与冷却是两道独立闸门
    (tmp_path / ".coara").mkdir(exist_ok=True)
    (tmp_path / ".coara" / "ws.md").write_text("# 定位\n测试\n", encoding="utf-8")
    ws_id = "ws-backoff"
    epoch = time.time() - 10_000.0
    root._sessions[ws_id] = SimpleNamespace(coara=_fake_coara(tmp_path), workspace_name="backoff")
    root._workspace_activity_at[ws_id] = epoch

    dispatch_count = {"n": 0}

    def _fake_run(_root, *, workspace_name, workspace_dir, coara_home, prompt, history_snapshot=None):
        dispatch_count["n"] += 1

        async def _run() -> None:
            await asyncio.sleep(0.02)
            raise RuntimeError("boom")

        return asyncio.create_task(_run())

    # 体内 import 从 janitor_maintenance 源模块取属性，绑定点在源模块
    monkeypatch.setattr("src.coara.janitor_maintenance.run_janitor_maintenance", _fake_run)
    try:
        await root._janitor_scan_expired(7200.0)
        assert dispatch_count["n"] == 1
        assert root._janitor_activity_at[ws_id] == epoch
        assert root._janitor_pending[ws_id][0].startswith("janitor-")
        assert not root._janitor_pending[ws_id][0].startswith("sa-")

        # 等失败完成钩子记账（done 回调经 call_soon 调度）
        wid = workspace_id_for(str(tmp_path))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if wid in wp._janitor_failures:
                break
        assert wp._janitor_failures.get(wid, (None, None))[0] == epoch
        # 磁盘已维护标记仍不落盘
        await root._janitor_finalize_pending()

        # 退避窗口内：同 epoch 不重派
        await root._janitor_scan_expired(7200.0)
        assert dispatch_count["n"] == 1

        # 退避窗口到期：消费名额补派一次，内存标记照常回填
        monkeypatch.setattr(wp, "JANITOR_RETRY_BACKOFF_SECONDS", 0.0)
        await root._janitor_scan_expired(7200.0)
        assert dispatch_count["n"] == 2
        assert root._janitor_activity_at[ws_id] == epoch
        assert root._janitor_pending[ws_id][1] == epoch
        assert root._janitor_pending[ws_id][0].startswith("janitor-")
        # task_id 后缀是 id(task)，前后两次 task 先后析构可能撞 id——不比较 id 相等性，
        # 用 flight 再次出现证明第二次派发真实发生
        wid2 = workspace_id_for(str(tmp_path))
        assert wid2 in wp._janitor_flights
    finally:
        wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_janitor_retry_quota_single_redispatch(tmp_path: Path, monkeypatch) -> None:
    """每 epoch 进程内最多补派一次：补派后再失败不再记录、不再放行"""
    from src.coara import workspace_protocol as wp
    from src.core.tool_base import ToolResult

    wp.reset_janitor_flights_for_tests()
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    epoch = 1234.0
    wid = workspace_id_for(str(ws_dir))
    payload = {"activity_epoch": epoch, "workspace_dir": str(ws_dir), "workspace_name": "ws"}

    async def _err() -> ToolResult:
        return ToolResult.error("boom")

    task1 = asyncio.create_task(_err())
    await task1
    wp._mark_activity_maintained(payload, task1)
    assert wid in wp._janitor_failures
    # 窗口内不补派；窗口到期消费名额；名额一次性
    assert wp.consume_janitor_retry(str(ws_dir), epoch) is False
    monkeypatch.setattr(wp, "JANITOR_RETRY_BACKOFF_SECONDS", 0.0)
    assert wp.consume_janitor_retry(str(ws_dir), epoch) is True
    assert wp.consume_janitor_retry(str(ws_dir), epoch) is False

    # 补派 run 再失败：不再记录失败表，防失败循环
    task2 = asyncio.create_task(_err())
    await task2
    wp._mark_activity_maintained(payload, task2)
    assert wid not in wp._janitor_failures
    assert wp.consume_janitor_retry(str(ws_dir), epoch) is False
    wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_janitor_success_not_recorded_as_failure(tmp_path: Path) -> None:
    """成功路径行为不变：磁盘标记照落、不记失败表、无补派名额"""
    from src.coara import workspace_protocol as wp
    from src.core.tool_base import ToolResult

    wp.reset_janitor_flights_for_tests()
    home = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    epoch = 1234.0
    payload = {
        "activity_epoch": epoch,
        "workspace_dir": str(ws_dir),
        "workspace_name": "ws",
        "coara_home": str(home),
    }

    async def _ok() -> ToolResult:
        return ToolResult.success(content="done")

    task = asyncio.create_task(_ok())
    await task
    wp._mark_activity_maintained(payload, task)
    assert wp.load_janitor_activity(str(ws_dir), str(home)) == epoch
    assert wp._janitor_failures == {}
    assert wp.consume_janitor_retry(str(ws_dir), epoch) is False
    wp.reset_janitor_flights_for_tests()


@pytest.mark.asyncio
async def test_delegate_tool_for_workspace_ensures_target_session(tmp_path: Path) -> None:
    """janitor 的 delegate 永远取自目标空间自己的会话（未加载则按需创建），不借前台。

    回归：前台 v8 时给 nx 派 janitor 用了前台 delegate，janitor 继承 v8
    全部对话历史（串味进 nx 录像带与快照）。
    """
    from src.coara import workspace_protocol as wp

    nx_delegate = object()
    ensured: list[str] = []
    coara = SimpleNamespace(_tool_manager=SimpleNamespace(tools={"delegate": nx_delegate}))
    entry = SimpleNamespace(id="nx-id")
    wm = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda _wid: entry))

    async def _ensure(e):
        ensured.append(e.id)
        return SimpleNamespace(coara=coara)

    root = SimpleNamespace(workspace_manager=wm, ensure_workspace_session=_ensure)
    nx_dir = tmp_path / "nx"
    nx_dir.mkdir()

    # 会话未加载也按需创建：父会话永远是目标空间会话
    assert await wp._delegate_tool_for_workspace(root, str(nx_dir)) is nx_delegate
    assert ensured == ["nx-id"]

    # 空间未注册：返回 None 由调用方放弃，不回退前台
    wm_empty = SimpleNamespace(registry=SimpleNamespace(get_by_id=lambda _wid: None))
    root_empty = SimpleNamespace(workspace_manager=wm_empty, ensure_workspace_session=_ensure)
    assert await wp._delegate_tool_for_workspace(root_empty, str(nx_dir)) is None


def test_load_target_history_prefers_snapshot() -> None:
    """压缩派发的 janitor 必须优先用压缩前完整历史快照，而不是压缩后的活会话。"""
    from src.coara.janitor_maintenance import _load_target_history

    snapshot = [Message(role=MessageRole.USER, content="压缩前完整对话历史")]
    # 即使活会话已压缩成摘要，快照优先
    root = SimpleNamespace(_sessions={})
    history = _load_target_history(root, "/tmp/ws", "/tmp/home", history_snapshot=snapshot)

    assert len(history) == 1
    assert "压缩前完整对话历史" in history[0].content
    # 快照是深拷贝：janitor 改动历史不影响派发时持有的快照
    history.append(Message(role=MessageRole.USER, content="janitor 追加"))
    assert len(snapshot) == 1


def test_load_target_history_falls_back_without_snapshot() -> None:
    """无快照（常规 janitor 触发）时仍走活会话/磁盘恢复逻辑。"""
    from src.coara.janitor_maintenance import _load_target_history

    root = SimpleNamespace(_sessions={})
    history = _load_target_history(root, "/tmp/ws", "/tmp/home", history_snapshot=None)
    assert history == []
