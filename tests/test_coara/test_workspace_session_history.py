"""会话事件溯源：事件日志为唯一事实源，persist 对账 + 恢复重放。"""

from __future__ import annotations

from pathlib import Path

from src.coara.workspace_state import (
    load_usage_snapshot_for_session,
    recover_session,
    replay_session_projection,
    save_session_state,
)
from src.context.window import LlmUsageSnapshot
from src.core.types import Message, MessageRole, ToolCall
from src.session_log.recorder import SessionLogRecorder


def _recorder(workspace: Path, coara_home: Path, session_id: str) -> SessionLogRecorder:
    return SessionLogRecorder(
        workspace_dir=workspace,
        session_id=session_id,
        coara_id="c1",
        coara_name="root",
        coara_home=coara_home,
    )


def test_persist_and_recover_preserves_tool_calls_and_continuations(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-tools-1"

    history = [
        Message(role=MessageRole.USER, content="调研信丰"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    name="skill",
                    arguments={"action": "activate", "name": "research"},
                )
            ],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            content="ok",
            tool_call_id="tc1",
            name="skill",
        ),
        Message(
            role=MessageRole.USER,
            content="<接续输入>世界杯结束了吗</接续输入>",
        ),
        Message(
            role=MessageRole.ASSISTANT,
            content="结束了。调研完成。",
        ),
    ]

    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history(history)
    save_session_state(workspace, session_id, coara_home=coara_home)

    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    sid, restored, _proj = recovered
    assert sid == session_id
    assert len(restored) == 5
    assert restored[0].content == "调研信丰"
    assert restored[1].tool_calls is not None
    assert restored[1].tool_calls[0].name == "skill"
    assert restored[1].tool_calls[0].arguments["name"] == "research"
    assert restored[2].role == MessageRole.TOOL_RESULT
    assert restored[2].tool_call_id == "tc1"
    assert "世界杯结束了吗" in str(restored[3].content)
    assert restored[4].content == "结束了。调研完成。"


def test_sync_history_idempotent_and_append_only(tmp_path: Path) -> None:
    """幂等：无差异零写入；追加只写增量。"""
    from src.session_log.store import count_events, resolve_session_log_path

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()

    recorder = _recorder(workspace, coara_home, "s1")
    base = [Message(role=MessageRole.USER, content="hi"), Message(role=MessageRole.ASSISTANT, content="yo")]
    recorder.sync_history(base)
    path = resolve_session_log_path(workspace, coara_home=coara_home)
    n1 = count_events(path)

    # 无差异：零写入
    recorder.sync_history(list(base))
    assert count_events(path) == n1

    # 追加：只写增量（2 条消息事件）
    recorder.sync_history([*base, Message(role=MessageRole.USER, content="again")])
    assert count_events(path) == n1 + 1


def test_sync_history_prefix_replacement_shadows(tmp_path: Path) -> None:
    """压缩/替换：前缀分叉 → shadow + 分叉后消息全部事件化重写。"""
    from src.session_log.project import project_session
    from src.session_log.store import read_events, resolve_session_log_path

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()

    recorder = _recorder(workspace, coara_home, "s2")
    old = [
        Message(role=MessageRole.USER, content="q1"),
        Message(role=MessageRole.ASSISTANT, content="a1"),
        Message(role=MessageRole.USER, content="q2"),
        Message(role=MessageRole.ASSISTANT, content="a2"),
    ]
    recorder.sync_history(old)

    # 压缩：前 3 条被摘要替换
    compressed = [
        Message(role=MessageRole.USER, content="<state_snapshot>摘要</state_snapshot>"),
        Message(role=MessageRole.ASSISTANT, content="a2"),
    ]
    recorder.sync_history(compressed)

    events = read_events(resolve_session_log_path(workspace, coara_home=coara_home), session_id="s2")
    kinds = [e["kind"] for e in events]
    assert "history/shadow" in kinds

    projection = project_session(events)
    assert [str(m.content) for m in projection.messages] == [
        "<state_snapshot>摘要</state_snapshot>",
        "a2",
    ]
    # 审计视图：旧消息仍可完整重建
    from src.session_log.project import derive_messages

    audit = derive_messages(events, skip_shadowed=False)
    contents = [str(m.content) for m in audit]
    assert "q1" in contents and "a1" in contents and "q2" in contents


def test_sync_history_truncation_shadows_tail(tmp_path: Path) -> None:
    """截断（content_policy 回滚）：尾部从投影消失 → shadow，前缀保留。"""
    from src.session_log.project import project_session
    from src.session_log.store import read_events, resolve_session_log_path

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()

    recorder = _recorder(workspace, coara_home, "s3")
    history = [
        Message(role=MessageRole.USER, content="keep1"),
        Message(role=MessageRole.ASSISTANT, content="keep2"),
        Message(role=MessageRole.TOOL_RESULT, content="strip-me", tool_call_id="t1", name="x"),
    ]
    recorder.sync_history(history)
    recorder.sync_history(history[:2])  # 截断尾部

    events = read_events(resolve_session_log_path(workspace, coara_home=coara_home), session_id="s3")
    projection = project_session(events)
    assert [str(m.content) for m in projection.messages] == ["keep1", "keep2"]


def test_usage_snapshot_via_session_meta(tmp_path: Path) -> None:
    """usage 快照走 session/meta 事件，重启后可恢复。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-usage-1"

    snap = LlmUsageSnapshot()
    snap.record_turn(
        usage={
            "input_tokens": 807,
            "output_tokens": 120,
            "cache_read_input_tokens": 175_872,
            "cache_creation_input_tokens": 0,
        },
        history_len=12,
        system_len=4209,
        tool_count=24,
    )
    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history([Message(role=MessageRole.USER, content="hi")])
    recorder.record_session_meta({"usage_snapshot": snap.to_dict()})
    save_session_state(workspace, session_id, coara_home=coara_home)

    loaded = load_usage_snapshot_for_session(session_id, workspace, coara_home=coara_home)
    assert loaded is not None
    assert loaded["history_len"] == 12
    assert loaded["usage"]["cache_read_input_tokens"] == 175_872

    restored = LlmUsageSnapshot()
    restored.restore(loaded)
    assert restored.has_reported_input
    assert restored.history_len == 12
    assert restored.tool_count == 24
    assert restored.cumulative_prompt_tokens == 807 + 175_872
    assert restored.cache_hit_ratio is not None

    # 无 meta 事件的会话：返回 None
    assert load_usage_snapshot_for_session("no-such", workspace, coara_home=coara_home) is None


def test_recover_restores_full_history_beyond_default_limit(tmp_path: Path) -> None:
    """恢复绝不裁剪：长历史全量回来。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-full-1"

    recorder = _recorder(workspace, coara_home, session_id)
    history: list[Message] = []
    for i in range(600):
        history.append(Message(role=MessageRole.USER, content=f"轮次 {i}"))
        history.append(Message(role=MessageRole.ASSISTANT, content=f"答复 {i}"))
    recorder.sync_history(history)
    save_session_state(workspace, session_id, coara_home=coara_home)

    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    sid, restored, _proj = recovered
    assert sid == session_id
    assert len(restored) == 1200
    assert restored[0].content == "轮次 0"
    assert restored[-1].content == "答复 599"


def test_session_id_mismatch_returns_empty_history(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()

    save_session_state(workspace, "current-sid", coara_home=coara_home)
    recorder = _recorder(workspace, coara_home, "other-sid")
    recorder.sync_history([Message(role=MessageRole.USER, content="old")])
    save_session_state(workspace, "current-sid", coara_home=coara_home)

    projection = replay_session_projection(workspace, "current-sid", coara_home=coara_home)
    assert projection.messages == []


def test_restart_notice_injected_once_on_recover(tmp_path: Path) -> None:
    """启动恢复写入的重启清算通知：恢复会话时注入一条注记且只注入一次。"""
    from src.coara.workspace_state import append_restart_notice

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-restart-1"

    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history([Message(role=MessageRole.USER, content="在吗")])
    save_session_state(workspace, session_id, coara_home=coara_home)
    append_restart_notice(workspace, ["后台任务 bash-xxx（视频生成）"], coara_home=coara_home)

    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    sid, history, _proj = recovered
    assert sid == session_id
    assert len(history) == 2
    assert "bash-xxx" in str(history[-1].content)
    assert "重启" in str(history[-1].content)

    recovered_again = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered_again is not None
    assert len(recovered_again[1]) == 1


def test_in_flight_marker_injects_interrupted_note_once(tmp_path: Path) -> None:
    """进程被杀残留 in-flight 标记：恢复时注入「上次回合未完成」注记且只注入一次。"""
    from src.coara.workspace_state import mark_turn_in_flight

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-inflight-1"

    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history([Message(role=MessageRole.USER, content="在吗")])
    save_session_state(workspace, session_id, coara_home=coara_home)
    mark_turn_in_flight(workspace, session_id, coara_home=coara_home, turn_id="t1")

    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    sid, history, _proj = recovered
    assert sid == session_id
    assert len(history) == 2
    assert history[-1].role == MessageRole.USER
    assert "上次回合" in str(history[-1].content)

    recovered_again = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered_again is not None
    assert len(recovered_again[1]) == 1


def test_in_flight_marker_mismatch_or_cleared_stays_silent(tmp_path: Path) -> None:
    """标记归属其他 session 或已正常清除时，恢复不注入注记且清掉陈旧标记。"""
    from src.coara.workspace_state import (
        _turn_in_flight_path,
        clear_turn_in_flight,
        mark_turn_in_flight,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-inflight-2"

    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history([Message(role=MessageRole.USER, content="hi")])
    save_session_state(workspace, session_id, coara_home=coara_home)

    mark_turn_in_flight(workspace, "old-sid", coara_home=coara_home)
    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    assert len(recovered[1]) == 1
    assert not _turn_in_flight_path(workspace, coara_home=coara_home).exists()

    mark_turn_in_flight(workspace, session_id, coara_home=coara_home)
    clear_turn_in_flight(workspace, coara_home=coara_home)
    recovered_clean = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered_clean is not None
    assert len(recovered_clean[1]) == 1


def test_provider_wire_blocks_roundtrip(tmp_path: Path) -> None:
    """anthropic thinking wire 块保真：事件化 → 投影 → 字段不丢。"""
    from src.session_log.project import project_session
    from src.session_log.store import read_events, resolve_session_log_path

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()

    wire = [{"type": "thinking", "thinking": "深想", "signature": "sig1"}]
    history = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="w1", name="read", arguments={"path": "/a"})],
            provider_wire_blocks=wire,
        ),
    ]
    recorder = _recorder(workspace, coara_home, "s-wire")
    recorder.sync_history(history)

    events = read_events(resolve_session_log_path(workspace, coara_home=coara_home), session_id="s-wire")
    projection = project_session(events)
    assert projection.messages[0].provider_wire_blocks == wire


def test_strip_then_persist_does_not_revive_switch_tail(
    tmp_path: Path,
) -> None:
    """Mid-turn switch strip 与事件对账组合：重启后恢复的是 strip 后历史。"""
    from src.coara.workspace_switch_history import strip_ws_switch_tail

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-strip-persist"

    history = [
        Message(role=MessageRole.USER, content="先做调研"),
        Message(role=MessageRole.ASSISTANT, content="好的，开始。"),
        Message(role=MessageRole.USER, content="切到 nx"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(
                    id="u1",
                    name="ws",
                    arguments={"action": "switch", "name": "nx"},
                )
            ],
        ),
    ]
    removed = strip_ws_switch_tail(history)
    assert removed == 2
    assert len(history) == 2

    recorder = _recorder(workspace, coara_home, session_id)
    recorder.sync_history(history)
    save_session_state(workspace, session_id, coara_home=coara_home)
    recovered = recover_session(workspace, coara_home=coara_home, label="ws")
    assert recovered is not None
    sid, restored, _proj = recovered
    assert sid == session_id
    assert len(restored) == 2
    assert restored[0].content == "先做调研"


def _workspace_entry(workspace: Path):
    from types import SimpleNamespace

    return SimpleNamespace(resolved_path=lambda: workspace, name="ws")


def _root_stub(coara_home: Path):
    from types import SimpleNamespace

    return SimpleNamespace(workspace_manager=SimpleNamespace(coara_home=coara_home))


async def test_restore_with_interrupt_note_does_not_duplicate_events(tmp_path: Path, monkeypatch) -> None:
    """D2 回归：恢复时被追加「上次回合未完成」注记（history 比投影多 1 条），
    recorder 游标仍须对齐投影本体——恢复后首次落盘只补记注记一条，
    不把整段历史当新事件重复写盘（续盘复制会让同一 tool_call 在下次恢复的
    投影里出现两份，provider 以 "No tool output found for tool call ..." 400 拒绝）。
    """
    from src.coara.injections.environment_injector import (
        ENV_CONTEXT_PREFIX,
        build_environment_seed_messages,
    )
    from src.coara.workspace_session import WorkspaceSession
    from src.coara.workspace_state import mark_turn_in_flight
    from src.session_log.project import project_session
    from src.session_log.store import count_events, read_events, resolve_session_log_path
    from tests.helpers import make_test_coara

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-cursor-1"

    # 历史含环境种子（走追加分支，不触发 seed 前置分叉）
    seed = build_environment_seed_messages(workspace)
    assert any(ENV_CONTEXT_PREFIX in str(m.content) for m in seed)
    history = [*seed, Message(role=MessageRole.USER, content="发布到目标机")]
    _recorder(workspace, coara_home, session_id).sync_history(history)
    save_session_state(workspace, session_id, coara_home=coara_home)
    mark_turn_in_flight(workspace, session_id, coara_home=coara_home, turn_id="t9")

    path = resolve_session_log_path(workspace, coara_home=coara_home)
    before = count_events(path)

    coara = make_test_coara(workspace)
    monkeypatch.setattr(coara, "_session_state_coara_home", lambda: coara_home)
    await WorkspaceSession._restore_from_disk(coara, _workspace_entry(workspace), _root_stub(coara_home))

    assert coara.session_id == session_id
    assert len(coara.message_history) == len(history) + 1
    assert "上次回合" in str(coara.message_history[-1].content)
    assert coara._session_log is not None

    # 恢复后首次落盘：游标对齐 → 纯追加，只多 1 条消息事件，无 shadow
    coara._session_log.sync_history(coara.message_history)
    assert count_events(path) == before + 1

    events = read_events(path, session_id=session_id)
    assert "history/shadow" not in [e["kind"] for e in events]
    contents = [str(m.content) for m in project_session(events).messages]
    assert contents.count("发布到目标机") == 1


async def test_restore_seed_prepend_rewrites_via_shadow_not_duplicate(tmp_path: Path, monkeypatch) -> None:
    """D2 回归：历史缺环境种子时恢复前置注入（前缀分叉），游标对齐投影后
    首次落盘走 history/shadow 截断重写；重放投影无重复消息（修复前游标丢失，
    整段历史被当新事件追加，投影里每条消息出现两份）。"""
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX
    from src.coara.workspace_session import WorkspaceSession
    from src.session_log.project import project_session
    from src.session_log.store import read_events, resolve_session_log_path
    from tests.helpers import make_test_coara

    workspace = tmp_path / "ws"
    workspace.mkdir()
    coara_home = tmp_path / "home"
    coara_home.mkdir()
    session_id = "sess-cursor-2"

    # 旧历史没有环境种子
    history = [
        Message(role=MessageRole.USER, content="在吗"),
        Message(role=MessageRole.ASSISTANT, content="在"),
    ]
    _recorder(workspace, coara_home, session_id).sync_history(history)
    save_session_state(workspace, session_id, coara_home=coara_home)

    coara = make_test_coara(workspace)
    monkeypatch.setattr(coara, "_session_state_coara_home", lambda: coara_home)
    await WorkspaceSession._restore_from_disk(coara, _workspace_entry(workspace), _root_stub(coara_home))

    # 种子被前置（可能多条前缀模块；环境上下文必有）
    assert any(ENV_CONTEXT_PREFIX in str(m.content) for m in coara.message_history)
    assert str(coara.message_history[-2].content) == "在吗"
    assert coara._session_log is not None

    coara._session_log.sync_history(coara.message_history)

    events = read_events(resolve_session_log_path(workspace, coara_home=coara_home), session_id=session_id)
    assert "history/shadow" in [e["kind"] for e in events]
    contents = [str(m.content) for m in project_session(events).messages]
    assert contents.count("在吗") == 1
    assert contents.count("在") == 1
