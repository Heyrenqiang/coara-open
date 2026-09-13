"""实时帧与快照的可对账性：帧带 view_seq、无在飞回合时走 standby 流。

契约（docs/Web设计体系.md §0.1/§0.2）：屏幕内容只由「快照 + 实时帧」驱动，
两者都带 workspace_dir / session_id / view_seq，端侧按同一个 reducer 应用。
本模块钉死服务端侧的两个保证：

1. 落带回调分配到的 view_seq 写进广播帧（含重连回放帧）；
2. 没有在飞回合流时，帧不再「无序号直推」，而是经 standby 流落带 + 广播。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.ui.turn_stream import TurnStream
from src.ui.web_server import WebServer
from src.ui.web_views import WebViewStore, read_latest_view_seq, resolve_web_view_path


class _FakeRegistry:
    """广播注册表替身：记录发到活跃连接的帧。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def has_active(self) -> bool:
        return True

    def send_to_active_nowait(self, message: dict) -> None:
        self.sent.append(dict(message))


@pytest.fixture()
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_subagent_chunk_is_micro_batched_per_tool_call() -> None:
    """子智能体正文按 tool_call_id 分桶微批：一 token 一帧会打爆 WS 与重连 buffer。"""
    registry = _FakeRegistry()
    server = SimpleNamespace(registry=registry)
    stream = TurnStream("turn-1", "web", "root", server, session_id="sess-1")

    for piece in ("一", "二", "三"):
        stream.emit("subagent_chunk", text=piece, tool_call_id="call-a", coara_id="c1", subagent_id="sa1")
    for piece in ("甲", "乙"):
        stream.emit("subagent_chunk", text=piece, tool_call_id="call-b", coara_id="c2", subagent_id="sa2")
    await asyncio.sleep(0.05)  # 微批窗口

    assert [f["type"] for f in registry.sent] == ["subagent_chunk", "subagent_chunk"]
    assert [f["text"] for f in registry.sent] == ["一二三", "甲乙"]
    assert [f["tool_call_id"] for f in registry.sent] == ["call-a", "call-b"]
    assert registry.sent[0]["coara_id"] == "c1"
    assert [f["view_seq"] for f in stream.replay() if "view_seq" in f] == []


@pytest.mark.asyncio
async def test_turn_stream_frames_carry_view_seq() -> None:
    """落带即分配 view_seq：广播帧与 buffer（重连回放）都必须带。"""
    registry = _FakeRegistry()
    server = SimpleNamespace(registry=registry)
    persisted: list[int] = []

    def persist(frame: dict) -> int | None:
        persisted.append(len(persisted) + 1)
        return persisted[-1]

    stream = TurnStream("turn-1", "web", "root", server, session_id="sess-1", persist=persist)
    stream.emit("chunk", text="正文一")
    await asyncio.sleep(0.05)  # 微批窗口
    stream.emit("tool", text="✓ read(a.py)")

    assert persisted == [1, 2]
    assert [f.get("view_seq") for f in registry.sent] == [1, 2]
    assert [f.get("view_seq") for f in stream.replay()] == [1, 2]
    assert registry.sent[0]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_standby_stream_persists_and_broadcasts_with_view_seq(coara_home: Path, tmp_path: Path) -> None:
    """无在飞回合流的帧经 standby 流出去：落带一次、带 view_seq、带归属。"""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    registry = _FakeRegistry()
    server = WebServer.__new__(WebServer)
    server.workspace_dir = ws_dir  # type: ignore[attr-defined]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.registry = registry  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_store_workspace = ws_dir  # type: ignore[attr-defined]

    sender = server._connection_end_sender()
    frame = {
        "kind": "chunk",
        "text": "孤儿正文",
        "session_id": "sess-orphan",
        "workspace_dir": str(ws_dir),
        "turn_id": "turn-orphan",
    }
    assert sender(frame) is True
    await asyncio.sleep(0.05)  # 微批窗口
    server._view_store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-orphan")
    frames = WebViewStore.iter_frames(path)
    assert len(frames) == 1, "同一帧不得落两次带（历史双落带=重复 chunk）"
    assert frames[0]["view_seq"] == 1
    assert frames[0]["turn_id"] == "turn-orphan"
    assert frames[0]["payload"]["text"] == "孤儿正文"

    assert registry.sent and registry.sent[-1]["view_seq"] == 1
    assert registry.sent[-1]["session_id"] == "sess-orphan"
    assert registry.sent[-1]["turn_id"] == "turn-orphan"
    server._view_store.close()


@pytest.mark.asyncio
async def test_chunk_routed_through_real_sender_lands_once(coara_home: Path, tmp_path: Path) -> None:
    """端到端：真实 EndRegistry + 真实 TurnStream 落带路径下，正文帧只落一次带。

    修复前：web sender 同步 void（返回 None）被判成「投递失败」→ 兜底 sink 再落
    一次带 → 同一 turn 同文本相邻两条 chunk（视图带里 seq=0 的 sink 帧 + seq=N 的
    TurnStream 帧）。修复后每帧恰好一条，index 与 view_seq 一一对应。
    """
    from src.coara.base import CoaraBase
    from src.coara.end_registry import EndRegistry

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    registry = _FakeRegistry()
    server = WebServer.__new__(WebServer)
    server.workspace_dir = ws_dir  # type: ignore[attr-defined]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.registry = registry  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_store_workspace = ws_dir  # type: ignore[attr-defined]

    stream = TurnStream(
        "turn-1",
        "web",
        "root",
        server,
        session_id="sess-1",
        workspace_dir=str(ws_dir),
        persist=server._view_store.make_persist(ws_dir, coara_home=coara_home),
    )
    end_registry = EndRegistry()
    end_registry.register("web", server._web_end_sender(stream), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _root_ref=SimpleNamespace(end_registry=end_registry),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        session_origin={},
    )
    for text in ("第一段", "第二段"):
        await CoaraBase._route_chunk_to_current_end(coara, text, "web")
        await asyncio.sleep(0.05)  # 微批窗口：两段各自成帧（否则会被合并成一帧）
    server._view_store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    frames = WebViewStore.iter_frames(path)
    texts = [(f.get("payload") or {}).get("text") for f in frames]
    assert texts == ["第一段", "第二段"], f"重复帧未消除：{texts}"
    assert [f["view_seq"] for f in frames] == [1, 2]
    assert [f.get("view_seq") for f in registry.sent] == [1, 2]
    server._view_store.close()


@pytest.mark.asyncio
async def test_session_messages_snapshot_carries_epoch_and_cursor(coara_home: Path, tmp_path: Path) -> None:
    """快照顶层带 workspace_dir / session_id / epoch / latest_seq / subagent_results。"""
    import json

    from src.ui.handlers.session import SessionHandlers

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id="sess-1")
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id="sess-1", payload={"content": "问"})
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id="sess-1", payload={"text": "答"})
    store.append_event(path, kind="subagent_result", turn_id="t1", source="web", subject="root",
                       session_id="sess-1", payload={"text": "子智能体答复", "tool_call_id": "call-9"})
    store.append_event(path, kind="tool", turn_id="t1", source="web", subject="root", session_id="sess-1",
                       payload={"text": "✓ read(a.py)", "ok": True, "tool_name": "read",
                                "tool_call_id": "c-sub-1", "parent_tool_call_id": "call-9"})
    store.flush(timeout=2.0)
    store.close()

    handlers = SessionHandlers.__new__(SessionHandlers)
    handlers._check_token = lambda request: None  # type: ignore[method-assign]
    handlers.workspace_dir = ws_dir  # type: ignore[attr-defined]
    handlers.coara_home = coara_home  # type: ignore[attr-defined]
    handlers.trace_store = SimpleNamespace(workspace_dir=ws_dir)  # type: ignore[attr-defined]
    handlers._resolve_view_subject_session = lambda source: ("root", "sess-1")  # type: ignore[method-assign]
    handlers._current_runtime = lambda: {"running": False}  # type: ignore[method-assign]

    response = await handlers._session_messages_impl(SimpleNamespace(query={}), "web")
    data = json.loads(response.body.decode("utf-8"))

    assert data["session_id"] == "sess-1"
    assert data["workspace_dir"] == str(ws_dir)
    assert data["epoch"] == f"{ws_dir}::root"
    # 游标同源：latest_seq 只覆盖实际返回切片的末帧——被折叠的 subagent 帧不进
    # messages，就不算游标（否则端侧把被裁帧当成「已送达」，屏幕出现永久空洞）。
    assert data["latest_seq"] == data["messages"][-1].get("seq")
    # 消息流里只有用户行与正文行（子智能体答复不进气泡）
    assert [m.get("text") for m in data["messages"] if m.get("text")] == ["问", "答"]
    assert data["subagent_results"] == {"call-9": "子智能体答复"}
    assert [e["type"] for e in data["subagent_diffs"]["call-9"]] == ["tool"]
    assert data["subagent_diffs"]["call-9"][0]["text"] == "✓ read(a.py)"
    assert data["subagent_diffs"]["call-9"][0]["tool_call_id"] == "c-sub-1"


@pytest.mark.asyncio
async def test_subagent_result_persisted_when_no_end_channel(coara_home: Path, tmp_path: Path) -> None:
    """端无通道时子智能体最终答复仍落带（折叠内容刷新后可回放），且落在 web 源回合。"""
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(_web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-7"  # type: ignore[attr-defined]

    tool._persist_subagent_result_view(SimpleNamespace(identity=SimpleNamespace(coara_id="c-1")), "子智能体答复")

    store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    frames = WebViewStore.iter_frames(path)
    assert len(frames) == 1
    assert frames[0]["kind"] == "subagent_result"
    assert frames[0]["turn_id"] == "turn-1"
    assert frames[0]["source"] == "web"
    assert frames[0]["view_seq"] == 1
    assert frames[0]["payload"] == {"text": "子智能体答复", "tool_call_id": "call-7", "coara_id": "c-1"}
    store.close()


@pytest.mark.asyncio
async def test_subagent_result_not_double_persisted_when_end_channel_hit(coara_home: Path, tmp_path: Path) -> None:
    """命中 web 通道时只由 TurnStream 落一份带——兜底落盘不得再补第二份。"""
    from src.coara.end_registry import EndRegistry
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    registry = EndRegistry()
    routed: list[dict] = []
    # 模拟 web 端 sender：命中并记录，返回 True（命中判据在 deliver，不在返回值）
    registry.register("web", lambda frame: routed.append(frame) or True, "sess-1")
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(end_registry=registry, _web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-7"  # type: ignore[attr-defined]

    tool._route_subagent_result(
        SimpleNamespace(identity=SimpleNamespace(coara_id="c-1"), _subagent_origin=("web", None)), "子智能体答复"
    )
    store.flush(timeout=2.0)

    assert [f["kind"] for f in routed] == ["subagent_result"]
    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    assert WebViewStore.iter_frames(path) == [], "命中通道时兜底落盘必须闭口"
    store.close()


@pytest.mark.asyncio
async def test_subagent_result_not_delivered_for_non_web_origin(coara_home: Path, tmp_path: Path) -> None:
    """CLI/手机派发的子智能体结果不投 web、也不落 web 视图带（守卫先于投递）。"""
    from src.coara.end_registry import EndRegistry
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    registry = EndRegistry()
    routed: list[dict] = []
    registry.register("web", lambda frame: routed.append(frame) or True, "sess-1")
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="cli-attached",
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(end_registry=registry, _web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-7"  # type: ignore[attr-defined]

    tool._route_subagent_result(
        SimpleNamespace(identity=SimpleNamespace(coara_id="c-1"), _subagent_origin=("cli-attached", "conn-1")),
        "子智能体答复",
    )
    store.flush(timeout=2.0)

    assert routed == []
    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    assert WebViewStore.iter_frames(path) == []
    store.close()


@pytest.mark.asyncio
async def test_persist_delegate_task_view_marks_brief(coara_home: Path, tmp_path: Path) -> None:
    """落盘路径：delegate 任务指令帧带 delegate_brief + 父 call_id（与实时 trace 同字段）。"""
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web"),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(_web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-brief-1"  # type: ignore[attr-defined]
    tool.background = False  # type: ignore[attr-defined]

    tool._persist_delegate_task_view("<任务指令>\n查全量测试\n</任务指令>")
    store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    frames = WebViewStore.iter_frames(path)
    assert len(frames) == 1
    assert frames[0]["kind"] == "user_message"
    assert frames[0]["payload"]["delegate_brief"] is True
    assert frames[0]["payload"]["parent_tool_call_id"] == "call-brief-1"

    briefs: dict[str, str] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_briefs_out=briefs)
    assert messages == [], "指令不得作为正文消息出现"
    assert briefs == {"call-brief-1": "<任务指令>\n查全量测试\n</任务指令>"}
    store.close()


@pytest.mark.asyncio
async def test_workspace_switch_response_carries_target_snapshot(coara_home: Path, tmp_path: Path) -> None:
    """切空间一次往返：响应直接带目标空间的权威快照（端上一次提交上屏）。"""
    import json

    from src.ui.handlers.session import _DEFAULT_HISTORY_LIMIT

    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    # 目标空间（beta）的线上已有内容
    store = WebViewStore()
    path_b = resolve_web_view_path(ws_b, coara_home=coara_home, subject="root", session_id="sid-b")
    store.append_event(path_b, kind="turn_start", turn_id="t1", source="web", subject="root", session_id="sid-b")
    store.append_event(path_b, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id="sid-b", payload={"content": "旧问题"})
    store.append_event(path_b, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id="sid-b", payload={"text": "旧回答"})
    store.append_event(path_b, kind="subagent_result", turn_id="t1", source="web", subject="root",
                       session_id="sid-b", payload={"text": "折叠答复", "tool_call_id": "c-1"})
    store.append_event(path_b, kind="diff", turn_id="t1", source="web", subject="root", session_id="sid-b",
                       payload={"diff": {"hunks": []}, "display_blocks": [{"kind": "diff"}],
                                "tool_name": "edit", "parent_tool_call_id": "c-1"})
    store.flush(timeout=2.0)

    view = SimpleNamespace(workspace_dir=ws_b, session_id="sid-b")
    requested: list[str] = []

    async def _set_web_view(name: str) -> bool:
        requested.append(name)
        return True

    server = WebServer.__new__(WebServer)
    server._check_token = lambda request: None  # type: ignore[method-assign]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server._view_store = store  # type: ignore[attr-defined]
    server.registry = SimpleNamespace(has_active=lambda: False)  # type: ignore[attr-defined]
    server._current_runtime = lambda: {"running": False, "session_id": "sid-b"}  # type: ignore[method-assign]
    server._view_coara = lambda: view  # type: ignore[method-assign]
    server.trace_store = SimpleNamespace(workspace_dir=ws_b)  # type: ignore[attr-defined]
    server._refresh_trace_store = lambda: None  # type: ignore[method-assign]
    server.root = SimpleNamespace(  # type: ignore[attr-defined]
        set_web_view_workspace=_set_web_view,
        resolve_web_view_coara=lambda: view,
    )
    server.workspace_dir = ws_a  # type: ignore[attr-defined]

    request = SimpleNamespace(json=AsyncMock(return_value={"name": "beta"}))
    response = await server._handle_workspace_switch(request)
    data = json.loads(response.body.decode("utf-8"))

    assert requested == ["beta"]
    assert server.workspace_dir == ws_b, "切换后本端视图目录要指向目标空间"
    snapshot = data["snapshot"]
    assert snapshot["workspace_dir"] == str(ws_b)
    assert snapshot["session_id"] == "sid-b"
    assert snapshot["epoch"] == f"{ws_b}::root"
    # 游标同源（同 test_session_messages_snapshot_carries_epoch_and_cursor）：末帧即游标
    assert snapshot["latest_seq"] == snapshot["messages"][-1].get("seq")
    assert snapshot["subagent_results"] == {"c-1": "折叠答复"}
    assert snapshot["subagent_diffs"]["c-1"][0]["type"] == "diff"
    assert snapshot["subagent_diffs"]["c-1"][0]["display_blocks"] == [{"kind": "diff"}]
    assert snapshot["subagent_diffs"]["c-1"][0]["diff_lines"] == {"hunks": []}
    assert [m.get("text") for m in snapshot["messages"] if m.get("text")] == ["旧问题", "旧回答"]
    assert snapshot["runtime"] == {"running": False, "session_id": "sid-b"}
    assert _DEFAULT_HISTORY_LIMIT == 100
    store.close()


@pytest.mark.asyncio
async def test_session_messages_readonly_workspace_prefetch(coara_home: Path, tmp_path: Path) -> None:
    """/api/session/messages?workspace_dir=… 只读那条线：不切空间、不动状态、可增量。"""
    import json

    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    store = WebViewStore()
    path_a = resolve_web_view_path(ws_a, coara_home=coara_home, subject="root", session_id="sid-a")
    for text in ("A一", "A二"):
        store.append_event(path_a, kind="chunk", turn_id="t1", source="web", subject="root",
                           session_id="sid-a", payload={"text": text})
    store.flush(timeout=2.0)

    switched: list[str] = []
    server = WebServer.__new__(WebServer)
    server._check_token = lambda request: None  # type: ignore[method-assign]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server._view_store = store  # type: ignore[attr-defined]
    server._current_runtime = lambda: {"running": False, "session_id": "sid-b"}  # type: ignore[method-assign]
    server._view_coara = lambda: SimpleNamespace(workspace_dir=ws_b, session_id="sid-b")  # type: ignore[method-assign]
    server.trace_store = SimpleNamespace(workspace_dir=ws_b)  # type: ignore[attr-defined]
    server.workspace_dir = ws_b  # type: ignore[attr-defined]
    server.root = SimpleNamespace(  # type: ignore[attr-defined]
        set_web_view_workspace=AsyncMock(side_effect=lambda name: switched.append(name) or True),
        resolve_workspace_coara=lambda workspace_id: (_ for _ in ()).throw(RuntimeError("not cached")),
    )

    request = SimpleNamespace(query={"workspace_dir": str(ws_a), "limit": "50"})
    response = await server._session_messages_impl(request, "web")
    data = json.loads(response.body.decode("utf-8"))

    assert data["workspace_dir"] == str(ws_a)
    assert data["epoch"] == f"{ws_a}::root"
    assert data["latest_seq"] == 2
    assert [m["text"] for m in data["messages"]] == ["A一", "A二"]
    # 只读：没切空间、本端视图目录没动、会话 id 查不到就给空串
    assert switched == []
    assert server.workspace_dir == ws_b
    assert data["session_id"] == ""

    # 增量：after_view_seq 对指定空间同样生效
    request_delta = SimpleNamespace(query={"workspace_dir": str(ws_a), "after_view_seq": "1"})
    delta = json.loads((await server._session_messages_impl(request_delta, "web")).body.decode("utf-8"))
    assert [m["text"] for m in delta["messages"]] == ["A二"]
    assert delta["latest_seq"] == 2

    # 非绝对路径拒绝（不把相对路径解释成 cwd 下的空间）
    request_bad = SimpleNamespace(query={"workspace_dir": "relative/ws"})
    bad = await server._session_messages_impl(request_bad, "web")
    assert bad.status == 400
    store.close()


@pytest.mark.asyncio
async def test_readonly_prefetch_writes_nothing_to_other_space(coara_home: Path, tmp_path: Path) -> None:
    """只读预取绝不落盘：读他空间的线（含 epoch 世代）不创建 sidecar、不写缓存。"""
    import json as _json

    from src.ui.web_views import resolve_view_meta_path

    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    path_a = resolve_web_view_path(ws_a, coara_home=coara_home, subject="root", session_id="sid-a")
    # 手写一帧（不经 store）：模拟「这条线是别的进程/更早写的」，此刻没有 sidecar
    path_a.write_text(
        _json.dumps(
            {
                "view_seq": 7,
                "seq": 1,
                "turn_id": "t1",
                "ts": 1.0,
                "source": "web",
                "subject": "root",
                "session_id": "sid-a",
                "kind": "chunk",
                "payload": {"text": "他空间正文"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert not resolve_view_meta_path(path_a).exists()

    store = WebViewStore()
    server = WebServer.__new__(WebServer)
    server._check_token = lambda request: None  # type: ignore[method-assign]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server._view_store = store  # type: ignore[attr-defined]
    server._current_runtime = lambda: None  # type: ignore[method-assign]
    server._view_coara = lambda: SimpleNamespace(workspace_dir=ws_b, session_id="sid-b")  # type: ignore[method-assign]
    server.trace_store = SimpleNamespace(workspace_dir=ws_b)  # type: ignore[attr-defined]
    server.workspace_dir = ws_b  # type: ignore[attr-defined]
    server.root = SimpleNamespace()  # type: ignore[attr-defined]

    data = _json.loads(
        (
            await server._session_messages_impl(SimpleNamespace(query={"workspace_dir": str(ws_a)}), "web")
        ).body.decode("utf-8")
    )
    store.flush(timeout=2.0)
    store.close()

    assert data["latest_seq"] == 7 and data["epoch"] == f"{ws_a}::root"
    assert not resolve_view_meta_path(path_a).exists(), "只读预取不得往他空间目录写 sidecar"
    assert read_latest_view_seq(path_a) == 7, "读不得改动他空间的线"


@pytest.mark.asyncio
async def test_session_messages_default_path_unchanged(coara_home: Path, tmp_path: Path) -> None:
    """不传 workspace_dir 时行为不变：读当前视图空间那条线。"""
    import json

    ws = tmp_path / "ws"
    ws.mkdir()
    store = WebViewStore()
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="sid")
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id="sid", payload={"text": "当前空间"})
    store.flush(timeout=2.0)

    server = WebServer.__new__(WebServer)
    server._check_token = lambda request: None  # type: ignore[method-assign]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server._view_store = store  # type: ignore[attr-defined]
    server._current_runtime = lambda: None  # type: ignore[method-assign]
    server._view_coara = lambda: SimpleNamespace(workspace_dir=ws, session_id="sid")  # type: ignore[method-assign]
    server.trace_store = SimpleNamespace(workspace_dir=ws)  # type: ignore[attr-defined]
    server.workspace_dir = ws  # type: ignore[attr-defined]

    data = json.loads((await server._session_messages_impl(SimpleNamespace(query={}), "web")).body.decode("utf-8"))

    assert data["workspace_dir"] == str(ws)
    assert data["session_id"] == "sid"
    assert data["latest_seq"] == 1
    assert [m["text"] for m in data["messages"]] == ["当前空间"]
    store.close()


@pytest.mark.asyncio
async def test_error_frame_carries_workspace_belonging() -> None:
    """错误帧带当前 runtime 的 session_id / workspace_dir（端侧边界守卫不丢提示）。"""
    server = WebServer.__new__(WebServer)
    server._current_runtime = lambda: {"session_id": "sess-1", "workspace_dir": "D:\\ws\\a"}  # type: ignore[method-assign]

    frame = server._error_frame("Empty message")

    assert frame == {
        "type": "error",
        "message": "Empty message",
        "session_id": "sess-1",
        "workspace_dir": "D:\\ws\\a",
    }
    # 模块错误保留 subject 与附加字段
    module_frame = server._error_frame("模块主体创建失败", turn_id="", subject="flow")
    assert module_frame["subject"] == "flow"
    assert module_frame["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_subagent_diff_end_to_end_lands_in_fold(coara_home: Path, tmp_path: Path) -> None:
    """端到端：delegate 子智能体的 diff 经真实路由 + 真实落带后进 subagent_diffs。"""
    from src.coara.base import CoaraBase
    from src.coara.end_registry import EndRegistry

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    registry = _FakeRegistry()
    server = WebServer.__new__(WebServer)
    server.workspace_dir = ws_dir  # type: ignore[attr-defined]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.registry = registry  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_store_workspace = ws_dir  # type: ignore[attr-defined]

    stream = TurnStream(
        "turn-1",
        "web",
        "root",
        server,
        session_id="sess-1",
        workspace_dir=str(ws_dir),
        persist=server._view_store.make_persist(ws_dir, coara_home=coara_home),
    )
    end_registry = EndRegistry()
    end_registry.register("web", server._web_end_sender(stream), "sess-1")

    sub = SimpleNamespace(
        session_id="sa-coaras-fold",
        identity=SimpleNamespace(user_facing=False, coara_id="c-sub"),
        workspace_dir=ws_dir,
        _session_agent_kind="subagent",
        _delegate_parent_tool_call_id="call-9",
        _delegate_parent_session_id="sess-1",
        _subagent_origin=("web", None),
        _root_ref=SimpleNamespace(end_registry=end_registry),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    tool_payload = {
        "tool_name": "edit",
        "tool_call_id": "c-sub-9",
        "tool_label": "edit(a.py)",
        "is_error": False,
        "duration_ms": 4.0,
        "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
        "diff_lines": {"hunks": [{"path": "a.py"}]},
        "source": "web",
        "subagent_origin": "web",
    }
    # executor 的真实调用顺序：先工具行、紧跟其 diff（同一 tool_call_id）
    CoaraBase._route_tool_line(sub, tool_payload)
    CoaraBase._route_tool_diff(sub, tool_payload)
    server._view_store.flush(timeout=2.0)

    assert registry.sent and registry.sent[-1].get("parent_tool_call_id") == "call-9"
    assert registry.sent[-2]["tool_call_id"] == registry.sent[-1]["tool_call_id"] == "c-sub-9"
    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    fold: dict[str, list[dict]] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_diffs_out=fold)
    assert messages == [], "子智能体 diff 不得进主会话消息流"
    assert [e["type"] for e in fold["call-9"]] == ["tool", "diff"]
    assert fold["call-9"][0]["tool_call_id"] == fold["call-9"][1]["tool_call_id"] == "c-sub-9", (
        "折叠条目里的 tool 行与 diff 必须同 id（端上据此把 diff 挂在工具行之后）"
    )
    assert fold["call-9"][1]["diff_lines"] == {"hunks": [{"path": "a.py"}]}
    server._view_store.close()


@pytest.mark.asyncio
async def test_resume_row_fold_has_brief_diffs_and_result(coara_home: Path, tmp_path: Path) -> None:
    """resume 行的折叠区与 spawn 同口径：brief + diffs + result 一个不少。

    同一子智能体两次 resume → 两条行各自带同一份原始指令（互不覆盖）。
    """
    import json

    from src.coara.end_registry import EndRegistry
    from src.core.types import Message, MessageRole
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    registry = EndRegistry()
    server = WebServer.__new__(WebServer)
    server.workspace_dir = ws_dir  # type: ignore[attr-defined]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server._view_store = store  # type: ignore[attr-defined]
    server._view_store_workspace = ws_dir  # type: ignore[attr-defined]
    server.registry = SimpleNamespace(has_active=lambda: False)  # type: ignore[attr-defined]
    server._current_runtime = lambda: None  # type: ignore[method-assign]
    server._view_coara = lambda: SimpleNamespace(session_id="sess-1", workspace_dir=ws_dir)  # type: ignore[method-assign]
    server.trace_store = SimpleNamespace(workspace_dir=ws_dir)  # type: ignore[attr-defined]
    server.root = SimpleNamespace()  # type: ignore[attr-defined]

    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web"),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(end_registry=registry, _web_server=web_server),
    )
    instruction = "<任务指令>\n查全量测试\n</任务指令>"
    subagent = SimpleNamespace(
        identity=SimpleNamespace(coara_id="c-sub"),
        message_history=[Message(role=MessageRole.USER, content=instruction)],
        # `_build_subagent` 为本次 resume 写入的归属（下面是它的真实产物形状）
        _session_agent_kind="subagent",
        _delegate_parent_session_id="sess-1",
        _delegate_parent_workspace_dir=str(ws_dir),
        _subagent_origin=("web", None),
        _root_ref=SimpleNamespace(end_registry=registry),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
    )
    subagent._route_session_id = lambda: "sess-1"  # type: ignore[method-assign]

    # web 端通道先就位（真实 TurnStream + 真实落带），否则帧会被当作无通道丢弃
    registry.register(
        "web",
        server._web_end_sender(
            TurnStream(
                "turn-1",
                "web",
                "root",
                server,
                session_id="sess-1",
                workspace_dir=str(ws_dir),
                persist=store.make_persist(ws_dir, coara_home=coara_home),
            )
        ),
        "sess-1",
    )
    tool_payload = {
        "tool_name": "edit",
        "tool_call_id": "c-sub-1",
        "tool_label": "edit(a.py)",
        "is_error": False,
        "duration_ms": 4.0,
        "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
        "diff_lines": {"hunks": [{"path": "a.py"}]},
        "source": "web",
    }

    for call_id in ("call-resume-1", "call-resume-2"):
        tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
        tool._parent = parent  # type: ignore[attr-defined]
        tool.tool_call_id = call_id  # type: ignore[attr-defined]
        tool.background = False  # type: ignore[attr-defined]
        subagent._delegate_parent_tool_call_id = call_id
        # 1) brief（本次 resume 行带同一份原始指令）
        tool._persist_resume_brief_view(subagent, "sa-coaras-x")
        # 2) 过程帧：工具行 + diff（父标识＝本次 resume call id）
        from src.coara.base import CoaraBase

        CoaraBase._route_tool_line(subagent, tool_payload)
        CoaraBase._route_tool_diff(subagent, tool_payload)
        # 3) 最终结果（同一 resume 行）
        tool._route_subagent_result(subagent, f"{call_id} 的结果")
    store.flush(timeout=2.0)

    snapshot = await server._load_view_snapshot("web", limit=100)
    for call_id in ("call-resume-1", "call-resume-2"):
        assert snapshot["subagent_briefs"][call_id] == instruction, f"{call_id} 行必须带同一份原始指令"
        assert [e["type"] for e in snapshot["subagent_diffs"][call_id]] == ["tool", "diff"]
        assert snapshot["subagent_diffs"][call_id][0]["tool_call_id"] == "c-sub-1"
        assert snapshot["subagent_results"][call_id] == f"{call_id} 的结果"
    # 指令不进消息流（不冒出 role=user 气泡）
    assert instruction not in [m.get("text") for m in snapshot["messages"]]
    assert snapshot["epoch"] == f"{ws_dir}::root"
    json.dumps(snapshot)  # 形状可序列化（端侧按 JSON 消费）
    store.close()


def test_build_subagent_registers_dispatch_origin(tmp_path: Path) -> None:
    """spawn / resume / 自动续跑都经 _build_subagent：派发端归属在这里统一登记。

    漏设的后果（生产带实测）：resume 的工具行 / chunk / 最终结果被 origin 守卫
    （非 web 派发不投）整条丢掉——折叠区只剩 diff。
    """
    from src.coara.builtin_agents import get_subagent
    from src.llm.registry import provider_registry
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation
    from tests.helpers import FakeProvider

    # CoaraBase 构造要解析 provider profile；隔离环境下注册一个假 provider
    provider_registry.register("test-origin-provider", FakeProvider([]))

    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=tmp_path,
        workspace_manager=None,
        delegate_depth=0,
        audit_session_id="audit-1",
        provider_name="p",
        model_name="m",
        _segments=SimpleNamespace(source="web"),
        session_origin={"channel_id": "conn-1"},
        _root_ref=None,
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-resume-1"  # type: ignore[attr-defined]
    tool.background = False  # type: ignore[attr-defined]
    tool.subagent_type = "coaras"  # type: ignore[attr-defined]
    tool.action = "resume"  # type: ignore[attr-defined]
    tool.system_dispatch = False  # type: ignore[attr-defined]

    subagent = tool._build_subagent(
        "sa-coaras-origin",
        get_subagent("coaras"),
        tmp_path,
        provider_name="test-origin-provider",
        model="m",
    )

    assert subagent._delegate_parent_tool_call_id == "call-resume-1", "本次调用的 call id（resume 同理）"
    assert subagent._subagent_origin == ("web", "conn-1"), "派发端快照：origin 守卫据此放行/拦截"
    assert subagent._subagent_parent is parent
    assert subagent._session_agent_kind == "subagent"


def test_original_task_instruction_from_history(tmp_path: Path) -> None:
    """resume 的 brief 内容回溯自断点历史；取不到返回空串（调用方跳过，不造空帧）。"""
    from src.core.types import Message, MessageRole
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)

    with_instruction = SimpleNamespace(
        message_history=[
            Message(role=MessageRole.SYSTEM, content="sys"),
            Message(role=MessageRole.USER, content="<任务指令>\n原始任务\n</任务指令>"),
            Message(role=MessageRole.USER, content="<主会话消息>补充</主会话消息>"),
        ]
    )
    assert tool._original_task_instruction(with_instruction) == "<任务指令>\n原始任务\n</任务指令>"
    assert tool._original_task_instruction(SimpleNamespace(message_history=[])) == ""
    assert tool._original_task_instruction(SimpleNamespace()) == ""


@pytest.mark.asyncio
async def test_persist_resume_brief_skips_when_no_instruction(coara_home: Path, tmp_path: Path) -> None:
    """历史里没有 <任务指令> 时跳过落盘并记 warning（不造空帧）。"""
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    web_server = SimpleNamespace(_view_store=store, coara_home=coara_home)
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web"),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(_web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-resume-1"  # type: ignore[attr-defined]
    tool.background = False  # type: ignore[attr-defined]

    tool._persist_resume_brief_view(SimpleNamespace(message_history=[]), "sa-coaras-x")
    store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="sess-1")
    assert WebViewStore.iter_frames(path) == [], "取不到指令时不得造空帧"
    store.close()


def test_resume_brief_live_frame_matches_spawn_shape(tmp_path: Path) -> None:
    """resume brief 同时发实时帧：delegate_brief + 父行 id + 指令全文（与 spawn 同形状）。"""
    from src.core.types import Message, MessageRole
    from src.tools.builtin.delegate.delegate import DelegateToolInvocation

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    web_server = SimpleNamespace(_view_store=None, coara_home=None)
    parent = SimpleNamespace(
        session_id="sess-1",
        workspace_dir=ws_dir,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web"),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _root_ref=SimpleNamespace(_web_server=web_server),
    )
    tool = DelegateToolInvocation.__new__(DelegateToolInvocation)
    tool._parent = parent  # type: ignore[attr-defined]
    tool.tool_call_id = "call-resume-7"  # type: ignore[attr-defined]
    tool.background = False  # type: ignore[attr-defined]

    instruction = "<任务指令>\n原始任务\n</任务指令>"
    emitted: list[tuple[str, str, dict]] = []
    subagent = SimpleNamespace(
        identity=SimpleNamespace(coara_id="c-sub"),
        message_history=[Message(role=MessageRole.USER, content=instruction)],
        _subagent_origin=("web", None),
    )
    subagent._emit_trace = lambda event_type, message, **kwargs: emitted.append(  # type: ignore[method-assign]
        (event_type, message, dict(kwargs.get("payload") or {}))
    )

    tool._persist_resume_brief_view(subagent, "sa-coaras-x")

    assert len(emitted) == 1, "view_store 缺失也要发实时帧（只落盘会让刷新前后不一致）"
    event_type, _message, payload = emitted[0]
    assert event_type == "user_message"
    assert payload["content"] == instruction
    assert payload["delegate_brief"] is True
    assert payload["parent_tool_call_id"] == "call-resume-7"
    assert payload["source"] == "web"


@pytest.mark.asyncio
async def test_send_error_writes_frame_with_belonging() -> None:
    """_send_error 实际发出去的 error 帧已带归属（端上不再因缺字段丢弃）。"""
    import json

    class _WS:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_str(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

    server = WebServer.__new__(WebServer)
    server._current_runtime = lambda: {"session_id": "sess-1", "workspace_dir": "D:\\ws\\a"}  # type: ignore[method-assign]
    ws = _WS()

    await server._send_error(ws, "Web 端请用界面操作代替 /ws（该命令在此不可用）。")

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["session_id"] == "sess-1"
    assert ws.sent[0]["workspace_dir"] == "D:\\ws\\a"


@pytest.mark.asyncio
async def test_standby_stream_reused_per_session_and_workspace(coara_home: Path, tmp_path: Path) -> None:
    """standby 流按 (session, workspace) 复用：同一空间的多段正文序号连续。"""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    registry = _FakeRegistry()
    server = WebServer.__new__(WebServer)
    server.workspace_dir = ws_dir  # type: ignore[attr-defined]
    server.coara_home = coara_home  # type: ignore[attr-defined]
    server.registry = registry  # type: ignore[attr-defined]
    server._view_store = WebViewStore()  # type: ignore[attr-defined]
    server._view_store_workspace = ws_dir  # type: ignore[attr-defined]

    sender = server._connection_end_sender()
    for text in ("一", "二"):
        sender({"kind": "chunk", "text": text, "session_id": "s1", "workspace_dir": str(ws_dir), "turn_id": "t1"})
        await asyncio.sleep(0.05)
    server._view_store.flush(timeout=2.0)

    assert len(server._standby_streams) == 1
    path = resolve_web_view_path(ws_dir, coara_home=coara_home, subject="root", session_id="s1")
    frames = WebViewStore.iter_frames(path)
    assert [f["view_seq"] for f in frames] == [1, 2]
    assert [f["payload"]["text"] for f in frames] == ["一", "二"]
    assert [f["view_seq"] for f in registry.sent] == [1, 2]
    server._view_store.close()
