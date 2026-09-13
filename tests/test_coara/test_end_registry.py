"""EndRegistry 输出帧路由（diff 与 chunk 统一走帧协议）。

内核输出（正文 chunk / 工具 diff）统一为输出帧，按 (source, session_id)
经 deliver() 投递到端通道。无通道静默跳过；会话槽位优先于全局槽位。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.end_registry import EndRegistry


def test_deliver_chunk_frame_to_sender() -> None:
    reg = EndRegistry()
    frames = []
    reg.register("web", lambda f: frames.append(f), "sess-1")
    reg.deliver("web", "sess-1", {"kind": "chunk", "text": "hi"})
    assert frames == [{"kind": "chunk", "text": "hi"}]


def test_deliver_diff_frame_to_sender() -> None:
    reg = EndRegistry()
    frames = []
    reg.register("matrix", lambda f: frames.append(f), "sess-1")
    reg.deliver("matrix", "sess-1", {"kind": "diff", "diff_lines": {"hunks": []}, "tool_name": "edit"})
    assert frames[0]["kind"] == "diff"
    assert frames[0]["diff_lines"] == {"hunks": []}


def test_deliver_no_sender_reports_miss() -> None:
    """无通道时不命中，value 为 None（判据是 hit，不是 value）。"""
    reg = EndRegistry()
    outcome = reg.deliver("web", "sess-1", {"kind": "chunk", "text": "x"})
    assert outcome.hit is False
    assert outcome.value is None


def test_deliver_global_fallback() -> None:
    reg = EndRegistry()
    frames = []
    reg.register("web", lambda f: frames.append(f))  # 全局槽位
    reg.deliver("web", "sess-9", {"kind": "chunk", "text": "y"})
    assert frames == [{"kind": "chunk", "text": "y"}]


def test_subagent_diff_routes_to_parent_session() -> None:
    """子智能体（非 user_facing）的 diff 必须按父会话 session_id 路由。

    端通道按父会话 session_id 注册；子智能体用自己 sa-xxx id 会查不到。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames = []
    reg.register("cli-attached", lambda f: frames.append(f), "parent-sess")

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-abc",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _subagent_parent=parent,
        _subagent_origin=("cli-attached", None),
        _delegate_parent_tool_call_id="delegate-call-1",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="",
        _cli_silent=False,
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="",
        _segments=SimpleNamespace(source="", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_diff(
        sub,
        {
            "display_blocks": [{"kind": "diff", "lines": ["+x"]}],
            "tool_name": "edit",
            "source": "cli-attached",
        },
    )
    assert len(frames) == 1
    assert frames[0]["kind"] == "diff"
    assert frames[0]["display_blocks"] == [{"kind": "diff", "lines": ["+x"]}]


def test_subagent_diff_source_falls_back_to_active_turn_source() -> None:
    """老子智能体无 subagent_origin 时回退回合段 source；路由键仍用父会话。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames = []
    reg.register("cli-attached", lambda f: frames.append(f), "parent-sess")

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-xyz",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _subagent_parent=parent,
        _subagent_origin=("cli-attached", None),
        _delegate_parent_tool_call_id="delegate-call-1",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="",
        _cli_silent=False,
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="cli-attached",
        _segments=SimpleNamespace(source="", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_diff(sub, {"display_blocks": [{"kind": "diff", "lines": ["-y"]}], "tool_name": "edit"})
    assert len(frames) == 1
    assert frames[0]["kind"] == "diff"


def test_deliver_session_id_falls_back_to_parent_for_subagent() -> None:
    """_route_session_id：非 user_facing（子智能体）回退父会话 session_id。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-1",
        identity=SimpleNamespace(user_facing=False),
        _subagent_parent=parent,
    )
    assert CoaraBase._route_session_id(sub) == "parent-sess"
    # 无父引用时保留自身 session_id
    sub_no_parent = SimpleNamespace(session_id="sa-coaras-2", identity=SimpleNamespace(user_facing=False))
    assert CoaraBase._route_session_id(sub_no_parent) == "sa-coaras-2"
    # user_facing（主会话）用自身 session_id
    main = SimpleNamespace(session_id="main-sess", identity=SimpleNamespace(user_facing=True))
    assert CoaraBase._route_session_id(main) == "main-sess"


def test_subagent_edit_diff_routes_to_parent_sender_real_path() -> None:
    """端到端（真实方法路径）：子智能体 edit 造成的 diff 按 subagent_origin 投父会话 sender。

    不 mock _route_session_id / _route_tool_diff：模拟 executor 构造的真实
    tool_complete payload（display_blocks + subagent_origin），验证 web 派发的
    子智能体 diff 到达 web 端 sender（用户要求：子智能体 edit diff 显示端与
    最终结果一致）。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.coara.end_registry import EndRegistry

    frames: list[dict] = []

    def web_sender(frame: dict) -> None:
        frames.append(frame)

    reg = EndRegistry()
    reg.register("web", web_sender, "parent-sess")

    root = SimpleNamespace(end_registry=reg)
    parent = SimpleNamespace(
        session_id="parent-sess",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
    )
    sub = SimpleNamespace(
        session_id="sa-coaras-edit",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _subagent_parent=parent,
        # delegate.py:913 登记的派发快照（web 端回合派发）
        _subagent_origin=("web", None),
        _delegate_parent_tool_call_id="delegate-call-1",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="",
        _cli_silent=False,
        _root_ref=root,
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        # 真实 CoaraBase 方法（逻辑已由 test_route_session_id_* 覆盖）
        _route_session_id=lambda: "parent-sess",
    )
    # executor._emit_tool_complete 段的真实 payload 形状（含 subagent_origin）
    CoaraBase._route_tool_diff(
        sub,
        {
            "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
            "diff_lines": {"hunks": []},
            "tool_name": "edit",
            "source": "cli-attached",  # 主会话回合来源；subagent_origin 优先
            "subagent_origin": "web",  # executor 从 _subagent_origin 快照写入
        },
    )
    assert len(frames) == 1
    assert frames[0]["kind"] == "diff"
    assert frames[0]["tool_name"] == "edit"
    assert frames[0]["display_blocks"] == [{"kind": "diff", "path": "a.py", "lines": ["+x"]}]


def test_subagent_diff_routes_by_origin_for_all_ends() -> None:
    """子智能体 edit diff 三端一视同仁：按派发来源投对应端 sender（与最终结果一致）。

    matrix 派发 → matrix sender；cli-attached 派发 → cli sender；web 派发 → web sender。
    同一 _route_tool_diff 路径，subagent_origin 决定落点。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.coara.end_registry import EndRegistry

    received: dict[str, list[dict]] = {}

    def _mk_sender(name: str):
        received[name] = []

        def sender(frame: dict) -> None:
            received[name].append(frame)

        return sender

    reg = EndRegistry()
    reg.register("web", _mk_sender("web"), "parent-sess")
    reg.register("cli-attached", _mk_sender("cli-attached"), "parent-sess")
    reg.register("matrix", _mk_sender("matrix"), "parent-sess")

    root = SimpleNamespace(end_registry=reg)
    for origin in ("web", "cli-attached", "matrix"):
        parent = SimpleNamespace(
            session_id="parent-sess",
            _segments=SimpleNamespace(source=origin, current=None, channel_id=""),
        )
        sub = SimpleNamespace(
            session_id=f"sa-{origin}",
            identity=SimpleNamespace(user_facing=False),
            _session_agent_kind="subagent",
            _subagent_parent=parent,
            _subagent_origin=(origin, None),
            _delegate_parent_tool_call_id="delegate-call-1",
            _delegate_parent_session_id="parent-sess",
            _delegate_parent_workspace_dir="",
            _cli_silent=False,
            _root_ref=root,
            _OUTPUT_FRAME_DIFF_ENABLED=True,
            _active_turn_source="",
            _segments=SimpleNamespace(source=origin, current=None, channel_id=""),
            _route_session_id=lambda: "parent-sess",
        )
        CoaraBase._route_tool_diff(
            sub,
            {
                "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
                "diff_lines": {"hunks": []},
                "tool_name": "edit",
                "source": "cli-attached",
                "subagent_origin": origin,
            },
        )

    assert set(received) == {"web", "cli-attached", "matrix"}
    for name, frames in received.items():
        assert len(frames) == 1, name
        assert frames[0]["kind"] == "diff"
        assert frames[0]["tool_name"] == "edit"


@pytest.mark.asyncio
async def test_subagent_diff_without_delegate_parent_is_not_delivered(tmp_path: Path) -> None:
    """真实运行验证：子智能体 CoaraBase 跑真实回合调用 edit 工具，diff 帧路由到父会话端 sender。

    不用桩对象——真实 CoaraBase + 真实 EditTool + 真实 process_message 回合循环，
    executor 在 tool_complete 里调 _route_tool_diff，验证 web 端（及同构的
    cli/matrix 端）实际会收到子智能体 edit 的 diff 帧。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.coara.end_registry import EndRegistry
    from src.core.types import CoaraPersona, ToolCall
    from src.llm.provider import LLMResponse
    from src.tools.builtin.file_io.edit import EditTool
    from tests.helpers import FakeProvider

    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")

    frames: list[dict] = []

    def web_sender(frame: dict) -> None:
        frames.append(frame)

    reg = EndRegistry()
    reg.register("web", web_sender, "parent-sess")
    root = SimpleNamespace(end_registry=reg)
    parent = SimpleNamespace(session_id="parent-sess", _root_ref=root)

    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-edit-1",
                        name="edit",
                        arguments={"path": str(target), "old_string": "value = 1", "new_string": "value = 2"},
                    )
                ],
            ),
            LLMResponse(content="改完了"),
        ]
    )
    subagent = CoaraBase(
        name="sa-edit-real",
        persona=CoaraPersona(name="coaras", role="subagent"),
        workspace_dir=tmp_path,
        provider=provider,
        user_facing=False,
    )
    subagent._session_agent_kind = "subagent"
    subagent._subagent_parent = parent
    subagent._subagent_origin = ("web", None)
    subagent._root_ref = root
    # 主流 source 判定保底：payload.subagent_origin 为空时回退回合段 source。
    subagent._active_turn_source = "web"
    # 非 delegate 建出的实例（无 delegate 父行标识）不盖父标识：其 diff 走主流
    # （user_facing=False 但无 _delegate_parent_tool_call_id → _route_subagent_tool_frame 早退）。
    subagent.register_tool(EditTool(read_state_store={}, workspace_root=tmp_path))

    async for _ in subagent.process_message("改一下 sample.py 的 value", source="cli-attached"):
        pass

    # 子智能体的 diff 与工具行同路：一律走折叠路径（必须带 delegate 父行标识）。
    # 非 delegate 建出的实例没有可折叠的父行 → 早退不投，绝不把子智能体的改动
    # 平铺进主会话正文流（「子智能体的任务/过程/结果/改动全部进折叠」是明确设计）。
    assert frames == [], f"无父标识的子智能体 diff 不应投递，实际收到 {len(frames)} 帧"


def test_deliver_specific_overrides_global() -> None:
    reg = EndRegistry()
    seen = []
    reg.register("web", lambda f: seen.append("global"), "")
    reg.register("web", lambda f: seen.append("specific"), "sess-1")
    reg.deliver("web", "sess-1", {"kind": "chunk", "text": "z"})
    assert seen == ["specific"]


def test_unregister_with_sender_guard() -> None:
    reg = EndRegistry()

    def s1(f):  # noqa: ANN001
        return None

    def s2(f):  # noqa: ANN001
        return None

    reg.register("web", s1, "sess-1")
    reg.unregister("web", s2, "sess-1")  # 非同一 sender：不删
    assert reg.sender_for("web", "sess-1") is s1
    reg.unregister("web", s1, "sess-1")
    assert reg.sender_for("web", "sess-1") is None


@pytest.mark.asyncio
async def test_chunk_falls_back_to_launch_end_when_seg_end_offline() -> None:
    """段归属端无通道时，chunk 回退投递到回合发起端——正文绝不静默丢弃。

    事故场景（2026-09-07）：background 唤醒回合被 web 跟话后段切到 web，
    但 ("web", session) 通道缺失 → chunk 全丢、页面像死掉。修复后应回退到
    launch end（background 通道）继续投递。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    background_frames: list[dict] = []
    reg.register("background", lambda f: background_frames.append(f), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="background",
        _segments=SimpleNamespace(source="", current=None, channel_id=""),
    )
    await CoaraBase._route_chunk_to_current_end(coara, "正文片段", "web")
    assert len(background_frames) == 1
    assert background_frames[0]["kind"] == "chunk"
    assert background_frames[0]["text"] == "正文片段"
    # 帧带归属（session_id）：端通道据此把帧投给它该去的回合流
    assert background_frames[0]["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_chunk_dropped_with_warning_only_when_no_end_at_all() -> None:
    """归属端与发起端都无通道时才丢弃，且 WARNING 留痕（不再 DEBUG 静默）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    coara = SimpleNamespace(
        session_id="sess-1",
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="background",
        _segments=SimpleNamespace(source="", current=None, channel_id=""),
    )
    # 无任何通道：不抛错、不投递（丢弃路径）
    await CoaraBase._route_chunk_to_current_end(coara, "正文片段", "web")
    assert reg.sender_for("web", "sess-1") is None
    assert reg.sender_for("background", "sess-1") is None


@pytest.mark.asyncio
async def test_chunk_falls_back_to_session_origin_for_background_awakened() -> None:
    """background 唤醒回合无自身通道、段归属也是 background：经 session_origin
    回投到最近一次真实用户输入端（子智能体结果注入 CLI 上屏的兜底链）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    cli_frames: list[dict] = []
    reg.register("cli-attached", lambda f: cli_frames.append(f), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="background",
        _segments=SimpleNamespace(source="background", current=None, channel_id=""),
        session_origin={"source": "cli-attached", "channel_id": ""},
    )
    await CoaraBase._route_chunk_to_current_end(coara, "子智能体结果", "background")
    assert len(cli_frames) == 1
    assert cli_frames[0]["kind"] == "chunk"
    assert cli_frames[0]["text"] == "子智能体结果"
    assert cli_frames[0]["session_id"] == "sess-1"


def test_tool_line_frame_routes_to_end_sender() -> None:
    """工具行帧（✓ tool(...)）按段 source 投到端通道，顶层带 text/ok/时长。

    web 端把它插进聊天流（正文段落之间）；标签由内核与 CLI 同源生成，
    端侧不再自行拼装。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    CoaraBase._route_tool_line(
        coara,
        {
            "tool_label": "read(D:\\ws\\a.py)",
            "tool_name": "read",
            "tool_call_id": "c1",
            "is_error": False,
            "duration_ms": 120.0,
            "source": "web",
        },
    )

    assert len(frames) == 1
    assert frames[0]["kind"] == "tool"
    assert frames[0]["text"] == "read(D:\\ws\\a.py)"
    assert frames[0]["is_error"] is False
    assert frames[0]["duration_ms"] == 120.0


def test_tool_line_routes_for_background_awakened_end() -> None:
    """background 唤醒回合有注册通道时，工具行与正文同路投递（手机端可见）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("background", lambda f: frames.append(f), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="background",
        _segments=SimpleNamespace(source="background", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    CoaraBase._route_tool_line(
        coara,
        {
            "tool_label": "read(a.py)",
            "tool_name": "read",
            "tool_call_id": "c-bg",
            "is_error": False,
            "duration_ms": 10.0,
            "source": "background",
        },
    )

    assert len(frames) == 1
    assert frames[0]["kind"] == "tool"
    assert frames[0]["text"] == "read(a.py)"


def test_tool_line_skipped_for_subagent_and_empty_label() -> None:
    """无父标识的子智能体工具行不进聊天流；无标签不投（防空行）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "parent-sess")

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-1",
        identity=SimpleNamespace(user_facing=False),
        _subagent_parent=parent,
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_line(sub, {"tool_label": "read(a.py)", "tool_name": "read", "source": "web"})

    main = SimpleNamespace(
        session_id="parent-sess",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_line(main, {"tool_label": "   ", "tool_name": "read", "source": "web"})

    assert frames == []


def test_subagent_tool_line_routes_to_delegate_fold() -> None:
    """delegate 子智能体的工具行带父标识只投 web（折叠在 delegate 工具行里）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "parent-sess")

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-fold",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _delegate_parent_tool_call_id="call-fold-1",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="D:\\ws",
        _subagent_origin=("web", None),
        _subagent_parent=parent,
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
    )
    CoaraBase._route_tool_line(
        sub,
        {"tool_label": "read(D:\\ws\\a.py)", "tool_name": "read", "tool_call_id": "c-sub-1",
         "duration_ms": 12.5, "source": "web", "subagent_origin": "web"},
    )

    assert len(frames) == 1
    frame = frames[0]
    assert frame["kind"] == "tool"
    assert frame["text"] == "read(D:\\ws\\a.py)"
    assert frame["parent_tool_call_id"] == "call-fold-1"
    assert frame["tool_call_id"] == "c-sub-1"
    assert frame["session_id"] == "parent-sess"


def test_subagent_tool_line_not_routed_for_non_web_origin() -> None:
    """CLI/手机派发的子智能体工具行不进 web 视图带（那条线上没有它的折叠行）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "parent-sess")

    sub = SimpleNamespace(
        session_id="sa-coaras-cli",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _delegate_parent_tool_call_id="call-cli-1",
        _delegate_parent_session_id="parent-sess",
        _subagent_origin=("cli-attached", "conn-1"),
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="cli-attached",
        _segments=SimpleNamespace(source="cli-attached", current=None, channel_id=""),
    )
    CoaraBase._route_tool_line(sub, {"tool_label": "read(a.py)", "tool_name": "read"})

    assert frames == []


def test_main_session_tool_line_has_no_parent_id() -> None:
    """主会话自己的工具行不带父标识（照旧进主流）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "sess-1")

    main = SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    CoaraBase._route_tool_line(main, {"tool_label": "read(a.py)", "tool_name": "read", "source": "web"})

    assert len(frames) == 1
    assert "parent_tool_call_id" not in frames[0]


def test_subagent_chunk_not_routed_for_non_web_origin() -> None:
    """CLI/手机派发的子智能体正文不投 web（守卫同工具行；否则污染 web 视图带 + 刷 WARNING）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    async def _run() -> list[dict]:
        reg = EndRegistry()
        frames: list[dict] = []
        reg.register("web", lambda f: frames.append(f), "parent-sess")
        sub = SimpleNamespace(
            session_id="sa-coaras-cli-chunk",
            identity=SimpleNamespace(user_facing=False, coara_id="c-1"),
            _session_agent_kind="subagent",
            _delegate_parent_tool_call_id="call-cli-1",
            _delegate_parent_session_id="parent-sess",
            _delegate_parent_workspace_dir="D:\\ws",
            _subagent_origin=("cli-attached", "conn-1"),
            _root_ref=SimpleNamespace(end_registry=reg),
            _active_turn_source="cli-attached",
        )
        await CoaraBase._route_subagent_chunk(sub, "子智能体正文")
        return frames

    import asyncio

    assert asyncio.run(_run()) == []


@pytest.mark.asyncio
async def test_subagent_task_instruction_trace_marked_as_brief(tmp_path: Path) -> None:
    """实时路径：子智能体的 <任务指令> trace user_message 带 brief 标记 + 父 call_id。

    与落盘路径（delegate._persist_delegate_task_view）同源同字段——端上两条路都靠
    这两个键把指令折进 delegate 工具行，而不是新建 role=user 气泡。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.core.message_tags import task_instruction
    from src.core.types import CoaraPersona
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider

    provider = FakeProvider([LLMResponse(content="做完了")])
    sub = CoaraBase(
        name="sa-brief",
        persona=CoaraPersona(name="coaras", role="subagent"),
        workspace_dir=tmp_path,
        provider=provider,
        user_facing=False,
    )
    sub._session_agent_kind = "subagent"
    sub._delegate_parent_tool_call_id = "call-brief-9"
    sub._root_ref = SimpleNamespace(end_registry=EndRegistry())

    recorded: list[dict] = []
    original = sub._emit_trace

    def _spy(event_type, message, **kwargs):  # noqa: ANN001
        if event_type == "user_message":
            recorded.append(dict(kwargs.get("payload") or {}))
        return original(event_type, message, **kwargs)

    sub._emit_trace = _spy  # type: ignore[method-assign]
    async for _ in sub.process_message(task_instruction("查全量测试"), source="web"):
        pass

    assert recorded, "回合开始必须发 user_message trace"
    assert recorded[0]["delegate_brief"] is True
    assert recorded[0]["parent_tool_call_id"] == "call-brief-9"


@pytest.mark.asyncio
async def test_plain_user_message_trace_not_marked_as_brief(tmp_path: Path) -> None:
    """主会话普通输入不带 brief 标记（判据必须排除普通用户消息）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.core.types import CoaraPersona
    from src.llm.provider import LLMResponse
    from tests.helpers import FakeProvider

    provider = FakeProvider([LLMResponse(content="好的")])
    coara = CoaraBase(
        name="main",
        persona=CoaraPersona(name="coara", role="assistant"),
        workspace_dir=tmp_path,
        provider=provider,
        user_facing=True,
    )
    coara._root_ref = SimpleNamespace(end_registry=EndRegistry())

    recorded: list[dict] = []
    original = coara._emit_trace

    def _spy(event_type, message, **kwargs):  # noqa: ANN001
        if event_type == "user_message":
            recorded.append(dict(kwargs.get("payload") or {}))
        return original(event_type, message, **kwargs)

    coara._emit_trace = _spy  # type: ignore[method-assign]
    async for _ in coara.process_message("帮我看下构建", source="web"):
        pass

    assert recorded
    assert "delegate_brief" not in recorded[0]
    assert "parent_tool_call_id" not in recorded[0]


def test_subagent_diff_channel_id_prefers_dispatch_channel() -> None:
    """子智能体 diff 的端内连接标识优先取派发快照（自身段 channel 恒空，多 attach 会投不准）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    conn_a_frames: list[dict] = []
    conn_b_frames: list[dict] = []
    sender_a = lambda f: conn_a_frames.append(f)  # noqa: E731
    sender_a._end_channel_id = "conn-a"  # type: ignore[attr-defined]
    sender_b = lambda f: conn_b_frames.append(f)  # noqa: E731
    sender_b._end_channel_id = "conn-b"  # type: ignore[attr-defined]
    reg.register("web", sender_a, "parent-sess")
    reg.register("web", sender_b, "parent-sess")

    parent = SimpleNamespace(session_id="parent-sess")
    sub = SimpleNamespace(
        session_id="sa-coaras-chan",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _delegate_parent_tool_call_id="call-chan",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="",
        _cli_silent=False,
        _subagent_origin=("web", "conn-b"),
        _subagent_parent=parent,
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_diff(
        sub,
        {
            "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
            "diff_lines": {"hunks": []},
            "tool_name": "edit",
            "source": "web",
            "subagent_origin": "web",
        },
    )

    assert conn_a_frames == []
    assert len(conn_b_frames) == 1


def test_diff_frame_carries_tool_call_id() -> None:
    """diff 帧带产生它的工具调用 id（与同工具 tool 行同值）：端上精确挂位。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "sess-1")

    main = SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    payload = {
        "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
        "diff_lines": {"hunks": []},
        "tool_name": "edit",
        "tool_call_id": "call_edit_1",
        "source": "web",
    }
    CoaraBase._route_tool_line(main, {**payload, "tool_label": "edit(a.py)"})
    CoaraBase._route_tool_diff(main, payload)

    assert [f["kind"] for f in frames] == ["tool", "diff"]
    assert frames[0]["tool_call_id"] == "call_edit_1"
    assert frames[1]["tool_call_id"] == "call_edit_1", "diff 必须与同工具 tool 行同 id"


def test_ptc_sub_call_tool_line_precedes_its_diff() -> None:
    """ptc 内部改文件的子调用：先投工具行、紧跟其 diff，两者 tool_call_id 同值。

    executor._emit_tool_complete 对 ptc 子调用（tool_call.id 带 ``code:`` 前缀）
    走同一对投递（先行后 diff，见 executor.py:451-457）；这里按同一 payload 形状
    钉死顺序与 id 一致——端上才能把 diff 挂在 ptc 行之后。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "sess-1")

    main = SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(user_facing=True),
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "sess-1",
    )
    payload = {
        "tool_name": "edit",
        "tool_call_id": "code:e4bf6bf665b8",
        "tool_label": "edit(D:\\ws\\src\\coara\\commands\\registry.py)",
        "is_error": False,
        "duration_ms": 3.0,
        "display_blocks": [{"kind": "diff", "path": "registry.py", "lines": ["+x"]}],
        "diff_lines": {"hunks": [{"path": "registry.py"}]},
        "source": "web",
    }
    CoaraBase._route_tool_line(main, payload)
    CoaraBase._route_tool_diff(main, payload)

    assert [f["kind"] for f in frames] == ["tool", "diff"]
    assert frames[0]["tool_call_id"] == frames[1]["tool_call_id"] == "code:e4bf6bf665b8"


def test_subagent_diff_frame_carries_parent_tool_call_id() -> None:
    """子智能体的 diff 帧带父标识（端上折进 delegate 工具行，不进正文流）。"""
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("web", lambda f: frames.append(f), "parent-sess")

    parent = SimpleNamespace(
        session_id="parent-sess",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
    )
    sub = SimpleNamespace(
        session_id="sa-coaras-diff",
        identity=SimpleNamespace(user_facing=False),
        _session_agent_kind="subagent",
        _delegate_parent_tool_call_id="call-fold-2",
        _delegate_parent_session_id="parent-sess",
        _delegate_parent_workspace_dir="",
        _cli_silent=False,
        _subagent_origin=("web", None),
        _subagent_parent=parent,
        _root_ref=SimpleNamespace(end_registry=reg),
        _OUTPUT_FRAME_DIFF_ENABLED=True,
        _active_turn_source="",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        _route_session_id=lambda: "parent-sess",
    )
    CoaraBase._route_tool_diff(
        sub,
        {
            "display_blocks": [{"kind": "diff", "path": "a.py", "lines": ["+x"]}],
            "diff_lines": {"hunks": []},
            "tool_name": "edit",
            "source": "web",
            "subagent_origin": "web",
        },
    )

    assert len(frames) == 1
    assert frames[0]["parent_tool_call_id"] == "call-fold-2"


def test_connection_scope_sender_survives_unregister() -> None:
    """连接级兜底通道（全局槽）不参与回合收尾注销，否则该端此后整条连接再无兜底。"""
    reg = EndRegistry()
    conn_frames: list[dict] = []
    conn = lambda f: conn_frames.append(f)  # noqa: E731
    conn._end_connection_scope = True  # type: ignore[attr-defined]
    reg.register("web", conn, "")

    turn_frames: list[dict] = []
    turn = lambda f: turn_frames.append(f)  # noqa: E731
    reg.register("web", turn, "sess-1")

    reg.unregister("web", turn, "sess-1")  # 回合收尾：删自己的精确槽
    reg.unregister("web", conn, "")  # 想删全局槽：必须被拦

    assert reg.sender_for("web", "sess-1") is conn
    reg.deliver("web", "sess-2", {"kind": "chunk", "text": "x"})
    assert conn_frames == [{"kind": "chunk", "text": "x"}]
    assert turn_frames == []


def test_per_session_sender_unregister_still_works() -> None:
    """保护只针对连接级通道：普通回合 sender 仍可按身份正常注销。"""
    reg = EndRegistry()
    turn = lambda f: None  # noqa: E731
    reg.register("web", turn, "sess-1")
    reg.unregister("web", turn, "sess-1")
    assert reg.sender_for("web", "sess-1") is None


@pytest.mark.asyncio
async def test_chunk_route_miss_no_second_landing(tmp_path: Path) -> None:
    """端通道全不可达时只丢弃 + WARNING：路由层不再有第二落点（双落带根因已删）。

    用真实视图存储 + 真实落带回调验证：帧归属的会话没有通道时，带里零帧——
    「路由层兜底补落一次带」的路径已不存在（落带唯一入口是端通道内的 TurnStream）。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase
    from src.ui.web_views import WebViewStore, resolve_web_view_path

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    store = WebViewStore()
    persist = store.make_persist(ws_dir, coara_home=None)

    def _web_sender(frame: dict) -> None:
        # 只服务别的会话（sess-other）：本帧归属 sess-1，查不到通道
        persist(
            {
                "type": "chunk",
                "text": str(frame.get("text") or ""),
                "session_id": "sess-other",
                "subject": "root",
                "turn_id": "turn-1",
            }
        )

    reg = EndRegistry()
    reg.register("web", _web_sender, "sess-other")
    coara = SimpleNamespace(
        session_id="sess-1",
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _active_turn_source="web",
        _segments=SimpleNamespace(source="web", current=None, channel_id=""),
        session_origin={},
    )

    await CoaraBase._route_chunk_to_current_end(coara, "正文段落", "web")
    store.flush(timeout=2.0)

    path = resolve_web_view_path(ws_dir, coara_home=None, subject="root", session_id="sess-other")
    assert WebViewStore.iter_frames(path) == [], "路由层未命中时不得补落一次带"
    store.close()


@pytest.mark.asyncio
async def test_chunk_route_hit_is_not_judged_by_sender_return_value() -> None:
    """命中判据与 sender 返回值分离：同步 void sender 命中后不再兜底重投。

    事故根因（2026-09-11）：sender 命中但返回 None 时被判成「投递失败」，于是
    依次向兜底端重投——web 端表现就是「TurnStream 落一次带 + 兜底再落一次带」
    的重复帧；attach 端表现是每帧一次虚假 WARNING。
    """
    from types import SimpleNamespace

    from src.coara.base import CoaraBase

    reg = EndRegistry()
    hit_frames: list[dict] = []
    fallback_frames: list[dict] = []
    reg.register("cli-attached", lambda f: hit_frames.append(f), "sess-1")  # void sender
    reg.register("background", lambda f: fallback_frames.append(f), "sess-1")

    coara = SimpleNamespace(
        session_id="sess-1",
        _root_ref=SimpleNamespace(end_registry=reg),
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _active_turn_source="cli-attached",
        _segments=SimpleNamespace(source="cli-attached", current=None, channel_id=""),
        session_origin={"source": "background", "channel_id": ""},
    )

    await CoaraBase._route_chunk_to_current_end(coara, "正文段落", "cli-attached")

    assert len(hit_frames) == 1
    assert fallback_frames == [], "命中即可，禁止再向兜底端重投（会双显）"


def test_deliver_hit_is_independent_of_sender_return_value() -> None:
    """deliver 的 hit 与 sender 返回值无关：同步 void sender 也是命中。"""
    reg = EndRegistry()
    frames: list[dict] = []
    reg.register("cli-attached", lambda f: frames.append(f), "sess-1")

    outcome = reg.deliver("cli-attached", "sess-1", {"kind": "chunk", "text": "x"})

    assert outcome.hit is True
    assert outcome.value is None  # sender 没返回值 ≠ 没命中
    assert frames == [{"kind": "chunk", "text": "x"}]
    # sender 无返回值时 value 同样是 None——它是「sender 说了什么」，不是命中判据
    assert reg.deliver("cli-attached", "sess-1", {"kind": "chunk", "text": "y"}).value is None
    assert len(frames) == 2


def test_deliver_reports_miss() -> None:
    reg = EndRegistry()
    outcome = reg.deliver("web", "sess-1", {"kind": "chunk", "text": "x"})
    assert outcome.hit is False
    assert outcome.value is None

