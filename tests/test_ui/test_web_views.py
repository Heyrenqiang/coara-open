"""web 会话视图存储（web_views）回归测试。

覆盖审查发现的缺陷：
- P1#2：tool_complete payload 必须带 turn_id，否则 diff 永远写不进视图存储
  （hydrate 改读视图后，F5 刷新 diff 会从聊天区消失）。
- 视图聚合：完整回合 + 孤儿回合（重启前进行中）的 build_messages 行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.ui.web_views import WebViewStore, resolve_web_view_path


@pytest.fixture()
def coara_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "coara_home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    return home


def _diff_lines() -> dict:
    return {
        "hunks": [
            {
                "path": "a.py",
                "added": 1,
                "removed": 0,
                "lines": [{"kind": "add", "text": "+x", "new_no": 1}],
            }
        ]
    }


def _ev(store: WebViewStore, path: Path, kind: str, sess: str, **payload: object) -> None:
    store.append_event(path, kind=kind, turn_id="t1", source="web", subject="root", session_id=sess, payload=payload)


def test_diff_frame_roundtrip_into_view(coara_home: Path, tmp_path: Path) -> None:
    """diff 帧经 append_event 落盘后，build_messages 能聚合成独立 diff 块行。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-1"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    store.append_event(path, kind="diff", turn_id="t1", source="web", subject="root", session_id=sess,
                       payload={"diff": _diff_lines()})
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    diff_rows = [m for m in messages if m.get("diff")]
    assert len(diff_rows) == 1
    assert diff_rows[0]["diff"]["hunks"][0]["path"] == "a.py"


def test_build_messages_since_seq_returns_delta_only(coara_home: Path, tmp_path: Path) -> None:
    """增量 hydrate：带游标只回游标之后的帧，游标保持全量末位——端侧只补后缀。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-1"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    _ev(store, path, "user_message", sess, content="第一问")
    _ev(store, path, "chunk", sess, text="第一答")
    store.flush(timeout=2.0)

    full, _t, cursor = WebViewStore.build_messages(path, limit=50)
    assert [m["text"] for m in full] == ["第一问", "第一答"]

    store.append_event(path, kind="turn_start", turn_id="t2", source="web", subject="root", session_id=sess)
    _ev(store, path, "user_message", sess, content="第二问")
    _ev(store, path, "chunk", sess, text="第二答")
    store.flush(timeout=2.0)
    store.close()

    delta, _t2, cursor2 = WebViewStore.build_messages(path, limit=50, since_seq=cursor)
    assert [m["text"] for m in delta] == ["第二问", "第二答"]
    assert cursor2 > cursor

    empty, _t3, cursor3 = WebViewStore.build_messages(path, limit=50, since_seq=cursor2)
    assert empty == []
    assert cursor3 == cursor2

def test_subagent_chunk_not_persisted_result_kept_as_marked_frame(coara_home: Path, tmp_path: Path) -> None:
    """子智能体过程帧（subagent_chunk）不入带；最终答复入带但只作折叠映射。

    落带不等于投影成消息：``subagent_result`` 帧必须留在带里（刷新后展开
    delegate 工具行仍能看到最终答复），但 build_messages 不得把它变成 assistant
    气泡——那是「刷新后凭空多一条回复」的根因。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)
    persist({"type": "subagent_chunk", "text": "子智能体的中间产出", "session_id": "s1", "subject": "root"})
    result_seq = persist(
        {
            "type": "subagent_result",
            "text": "子智能体的最终答复",
            "tool_call_id": "call-1",
            "session_id": "s1",
            "subject": "root",
            "turn_id": "t1",
        }
    )
    store.flush(timeout=2.0)
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="s1")
    frames = WebViewStore.iter_frames(path)

    assert [f["kind"] for f in frames] == ["subagent_result"]
    assert result_seq == frames[0]["view_seq"]
    assert frames[0]["payload"]["tool_call_id"] == "call-1"

    subagent_results: dict[str, str] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_out=subagent_results)
    assert messages == [], "子智能体答复不得投影成聊天消息（刷新后会复活成气泡）"
    assert subagent_results == {"call-1": "子智能体的最终答复"}
    store.close()


def test_append_event_returns_assigned_view_seq(coara_home: Path, tmp_path: Path) -> None:
    """落带返回分配到的 view_seq：TurnStream 靠它把序号写进广播帧（端侧对账凭据）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-ret"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    first = store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                               session_id=sess, payload={"text": "一"})
    second = store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                                session_id=sess, payload={"text": "二"})
    assert (first, second) == (1, 2)
    store.flush(timeout=2.0)
    store.close()


def test_make_persist_returns_view_seq_none_for_unpersisted(coara_home: Path, tmp_path: Path) -> None:
    """make_persist 把 view_seq 交回调用方；子智能体过程帧不落带故返回 None。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)

    seq = persist({"type": "chunk", "text": "正文", "session_id": "s1", "subject": "root", "turn_id": "t1"})
    skipped = persist({"type": "subagent_chunk", "text": "子", "session_id": "s1", "subject": "root"})
    store.flush(timeout=2.0)
    store.close()

    assert seq == 1
    assert skipped is None


def test_legacy_synthetic_subagent_bubble_hidden(coara_home: Path, tmp_path: Path) -> None:
    """历史合成帧（[sa-…] 开头的 chunk）读端消隐：不再复活成 assistant 气泡。

    2026-09-11 前子智能体最终答复被合成成 turn_start + chunk 落带；合成源已删，
    既有带里的这些行只靠读端判据消隐（同回合的正常正文照常渲染）。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-legacy"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    _ev(store, path, "turn_start", sess)
    _ev(store, path, "chunk", sess, text="正常正文")
    _ev(store, path, "chunk", sess, text="[sa-coaras-e1ab31cb] 前端构建验证\n验证完成，构建通过。")
    _ev(store, path, "chunk", sess, text="后续正文")
    _ev(store, path, "turn_end", sess)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    assert [m["text"] for m in messages] == ["正常正文", "后续正文"]


def test_view_seq_continues_when_file_removed(coara_home: Path, tmp_path: Path) -> None:
    """文件被删（归档/清理）后继续追加：序号从历史最大 +1 续，不回退到 1。

    端上按 view_seq 做前缀比对，序号一旦重置就会与旧缓存撞号（去重/gap 全错）。
    sidecar 记高水位，是这条线的序号不重置的凭据。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-rm"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)

    store = WebViewStore()
    seqs = [
        store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                           session_id=sess, payload={"text": f"第{i}段"})
        for i in range(3)
    ]
    assert seqs == [1, 2, 3]
    store.close()  # close 强制落 sidecar 高水位

    path.unlink()  # 模拟归档/清空：文件没了，线还在

    store2 = WebViewStore()
    nxt = store2.append_event(path, kind="chunk", turn_id="t2", source="web", subject="root",
                              session_id=sess, payload={"text": "续写"})
    store2.flush(timeout=2.0)
    store2.close()

    assert nxt == 4, "文件缺失后续号必须从历史高水位继续"
    from src.ui.web_views import read_latest_view_seq

    assert read_latest_view_seq(path) == 4


def test_view_seq_continues_after_external_truncation(coara_home: Path, tmp_path: Path) -> None:
    """文件被外部截断（尾部读不到序号）时也续号——sidecar 高水位接住。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-trunc"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)

    store = WebViewStore()
    for i in range(5):
        _ev(store, path, "chunk", sess, text=str(i))
    store.close()

    path.write_text("", encoding="utf-8")  # 截断

    store2 = WebViewStore()
    nxt = store2.append_event(path, kind="chunk", turn_id="t9", source="web", subject="root",
                              session_id=sess, payload={"text": "after-truncate"})
    store2.flush(timeout=2.0)
    store2.close()

    assert nxt == 6


def test_reset_line_changes_epoch(coara_home: Path, tmp_path: Path) -> None:
    """重建线（唯一合法的重置入口）：世代 +1 → epoch 变化 → 端上判定「新线，全量重取」。"""
    from src.ui.web_views import view_line_epoch

    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-reset"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    _ev(store, path, "chunk", sess, text="一")
    _ev(store, path, "chunk", sess, text="二")

    assert store.line_generation(path) == 0
    assert view_line_epoch(ws, "root") == f"{ws}::root"

    generation = store.reset_line(path)

    assert generation == 1
    assert view_line_epoch(ws, "root", generation=generation) == f"{ws}::root#1"
    # 新线：序号从头开始（epoch 已变，端上不会拿旧前缀比对）
    assert store.append_event(path, kind="chunk", turn_id="t2", source="web", subject="root",
                              session_id=sess, payload={"text": "新线一"}) == 1
    store.flush(timeout=2.0)
    store.close()
    assert WebViewStore().line_generation(path) == 1


def test_meta_sidecar_is_not_a_frame_line(coara_home: Path, tmp_path: Path) -> None:
    """sidecar 与视图文件同名不同后缀：读帧只读 jsonl，不把元数据当帧。"""
    from src.ui.web_views import resolve_view_meta_path

    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-meta"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    _ev(store, path, "chunk", sess, text="正文")
    store.close()

    meta_path = resolve_view_meta_path(path)
    assert meta_path.name == "conversation.jsonl.meta.json"
    frames = WebViewStore.iter_frames(path)
    assert [f["kind"] for f in frames] == ["chunk"]
    messages, _total, latest = WebViewStore.build_messages(path, limit=50)
    assert [m["text"] for m in messages] == ["正文"]
    assert latest == 1


def test_subagent_tool_and_diff_frames_go_to_fold(coara_home: Path, tmp_path: Path) -> None:
    """带 parent_tool_call_id 的 tool/diff 不进 messages，按父 call_id 归集。

    主会话自己的 diff 行为不变（仍投影成 diff 行）；子智能体的工具行/改动只走
    subagent_diffs，端上渲染在那条 delegate 工具行的展开区。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-fold"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)
    base = {"session_id": sess, "subject": "root", "turn_id": "t1", "source": "web"}

    persist({**base, "type": "chunk", "text": "主会话正文"})
    # 主会话自己的 diff：照旧进主流，但带产生它的工具调用 id（端上精确挂位）
    persist({**base, "type": "diff", "diff_lines": _diff_lines(), "display_blocks": [{"kind": "diff"}],
             "tool_name": "edit", "tool_call_id": "c-main-1"})
    # 子智能体的工具行 + 改动：带父标识 → 折叠
    persist({**base, "type": "tool", "text": "✓ read(a.py)", "ok": True, "tool_name": "read",
             "tool_call_id": "c-sub-1", "duration_ms": 8.0, "parent_tool_call_id": "call-9"})
    persist({**base, "type": "diff", "diff_lines": _diff_lines(), "display_blocks": [{"kind": "diff"}],
             "tool_name": "edit", "tool_call_id": "c-sub-2", "parent_tool_call_id": "call-9"})
    store.flush(timeout=2.0)

    fold: dict[str, list[dict]] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_diffs_out=fold)

    assistant = [m for m in messages if m.get("role") == "assistant"]
    assert [m["text"] for m in assistant if m.get("text")] == ["主会话正文"]
    main_diff = [m for m in assistant if m.get("diff")]
    assert len(main_diff) == 1, "主会话 diff 照旧投影"
    assert main_diff[0]["tool_call_id"] == "c-main-1", "主流 diff 行也带工具调用 id"
    assert all(not m.get("tool") for m in assistant), "子智能体工具行不得进主会话正文流"

    assert list(fold) == ["call-9"]
    entries = fold["call-9"]
    assert [e["type"] for e in entries] == ["tool", "diff"]
    assert entries[0]["text"] == "✓ read(a.py)"
    assert entries[0]["tool_call_id"] == "c-sub-1"
    assert entries[0]["view_seq"] > 0
    assert entries[1]["display_blocks"] == [{"kind": "diff"}]
    assert entries[1]["diff_lines"] == _diff_lines()
    assert entries[1]["tool_name"] == "edit"
    assert entries[1]["tool_call_id"] == "c-sub-2", "折叠 diff 条目必须带产生它的工具调用 id"
    assert entries[0]["tool_call_id"] == "c-sub-1"
    store.close()


def test_frames_without_parent_id_still_project(coara_home: Path, tmp_path: Path) -> None:
    """老帧（没有 parent_tool_call_id）照原样投影——历史兼容，不改写旧数据。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-legacy-fold"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    _ev(store, path, "turn_start", sess)
    _ev(store, path, "tool", sess, text="✓ read(a.py)", ok=True, tool_name="read", tool_call_id="c-old")
    _ev(store, path, "diff", sess, diff=_diff_lines())
    _ev(store, path, "turn_end", sess)
    store.flush(timeout=2.0)

    fold: dict[str, list[dict]] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_diffs_out=fold)

    assert fold == {}
    kinds = [("tool" if m.get("tool") else "diff" if m.get("diff") else "text") for m in messages]
    assert kinds == ["tool", "diff"]
    store.close()


def test_delegate_brief_frame_goes_to_briefs_not_messages(coara_home: Path, tmp_path: Path) -> None:
    """delegate 任务指令不进 messages，归集进 subagent_briefs[父 call_id]。

    主会话真实用户消息不受影响（判据只认 brief 标记/老文本判据）。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-brief"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)
    base = {"session_id": sess, "subject": "root", "turn_id": "t1", "source": "web"}

    persist({**base, "type": "user_message", "content": "帮我看下构建"})
    persist(
        {
            **base,
            "type": "user_message",
            "content": "<任务指令>\n查全量测试\n</任务指令>",
            "delegate_task": True,
            "delegate_brief": True,
            "parent_tool_call_id": "call-brief-1",
        }
    )
    persist({**base, "type": "chunk", "text": "开始跑"})
    store.flush(timeout=2.0)

    briefs: dict[str, str] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_briefs_out=briefs)

    assert [m["text"] for m in messages if m.get("role") == "user"] == ["帮我看下构建"]
    assert [m["text"] for m in messages if m.get("text")] == ["帮我看下构建", "开始跑"]
    assert briefs == {"call-brief-1": "<任务指令>\n查全量测试\n</任务指令>"}
    store.close()


def test_injected_subagent_message_frame_not_rendered(coara_home: Path, tmp_path: Path) -> None:
    """内核注入信封（<子智能体消息>）落成的 user_message 帧不冒气泡、也不进折叠。

    写入路径现实存在：interact 的注入无端来源，回合结束后 leftover 按收尾端开
    回合（continuation_leftover 空 source 分支）→ web_server 落 user_message。
    读端兜底消隐，刷新后不会出现「用户手打了 <子智能体消息>」的假气泡。
    """
    from src.core.message_tags import subagent_message

    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-injected"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root", session_id=sess,
                       payload={"content": "真实提问"})
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root", session_id=sess,
                       payload={"content": subagent_message("里程碑报告", task_id="sa-coaras-1")})
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root", session_id=sess,
                       payload={"text": "收到"})
    store.flush(timeout=2.0)
    store.close()

    briefs: dict[str, str] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_briefs_out=briefs)

    assert [m["text"] for m in messages if m.get("text")] == ["真实提问", "收到"]
    assert briefs == {}


def test_legacy_brief_frames_still_folded(coara_home: Path, tmp_path: Path) -> None:
    """老带里的指令行（无新标记）也按 brief 处理：文本判据 + 历史 delegate_task 标记。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-brief-legacy"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"content": "普通提问"})
    # 老格式一：带 delegate_task 标记、无 brief 标记、无父 call_id
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id=sess,
                       payload={"content": "<任务指令>\n老指令 A\n</任务指令>", "delegate_task": True})
    # 老格式二：连 delegate_task 都没有，只有正文判据
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"content": "<任务指令>\n老指令 B\n</任务指令>"})
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"text": "回答"})
    store.flush(timeout=2.0)
    store.close()

    briefs: dict[str, str] = {}
    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, subagent_briefs_out=briefs)

    assert [m["text"] for m in messages if m.get("text")] == ["普通提问", "回答"]
    # 老帧没有父行标识：归到空 key（端上按需忽略），关键是不再冒气泡
    assert briefs == {"": "<任务指令>\n老指令 B\n</任务指令>"}


def test_fold_maps_respect_since_seq_and_caps(coara_home: Path, tmp_path: Path, monkeypatch) -> None:
    """折叠映射的边界：只回游标之后的帧；每个 call_id 与 call_id 数都封顶。"""
    import src.ui.web_views as web_views

    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-fold-cap"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    monkeypatch.setattr(web_views, "_FOLD_MAX_FRAMES_PER_CALL", 2)
    monkeypatch.setattr(web_views, "_FOLD_MAX_CALLS", 2)

    for idx in range(4):
        for call in ("call-a", "call-b", "call-c"):
            store.append_event(path, kind="tool", turn_id="t1", source="web", subject="root", session_id=sess,
                               payload={"text": f"✓ read({call}-{idx})", "tool_name": "read",
                                        "parent_tool_call_id": call})
    store.flush(timeout=2.0)
    store.close()

    fold: dict[str, list[dict]] = {}
    WebViewStore.build_messages(path, limit=50, subagent_diffs_out=fold)

    assert len(fold) == 2, "call_id 数按 _FOLD_MAX_CALLS 封顶（保留最近的）"
    assert set(fold) == {"call-b", "call-c"}
    assert [e["text"] for e in fold["call-c"]] == ["✓ read(call-c-2)", "✓ read(call-c-3)"]

    # 增量：since_seq 之后的帧才收（call-a/b/c 每个 idx 各一帧，共 12 帧）
    delta: dict[str, list[dict]] = {}
    WebViewStore.build_messages(path, limit=50, since_seq=10, subagent_diffs_out=delta)
    assert [e["text"] for e in delta["call-c"]] == ["✓ read(call-c-3)"]
    assert [e["text"] for e in delta["call-b"]] == ["✓ read(call-b-3)"]
    assert "call-a" not in delta
    empty: dict[str, list[dict]] = {}
    WebViewStore.build_messages(path, limit=50, since_seq=12, subagent_diffs_out=empty)
    assert empty == {}


def test_view_seq_continues_after_reopen(coara_home: Path, tmp_path: Path) -> None:
    """view_seq 重启（新建 store）后经尾部校准续号，不回退不撞号。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-3"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)

    store = WebViewStore()
    for i in range(5):
        _ev(store, path, "chunk", sess, text=str(i))
    store.flush(timeout=2.0)
    store.close()

    store2 = WebViewStore()
    _ev(store2, path, "chunk", sess, text="5")
    store2.flush(timeout=2.0)
    store2.close()

    _messages, _total, latest = WebViewStore.build_messages(path, limit=50)
    assert latest == 6


def test_command_result_chunk_keeps_command_card_flag(coara_home: Path, tmp_path: Path) -> None:
    """/compact 落带的 chunk 带 is_command_result，hydrate 仍渲染为命令卡。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-cmd"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    _ev(store, path, "turn_start", sess)
    _ev(store, path, "chunk", sess, text="已压缩：10 → 3", is_command_result=True)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    assert len(messages) == 1
    assert messages[0]["text"] == "已压缩：10 → 3"
    assert messages[0].get("is_command_result") is True


def test_multi_chunk_frames_stay_separate_bubbles(coara_home: Path, tmp_path: Path) -> None:
    """主会话刷新与实时一致：同一回合 a→工具→b 的多条 chunk 帧各自独立气泡。

    实时渲染（store chunk 分支）每条 chunk 帧一个气泡；build_messages 不得
    把它们合并成一个，否则刷新后气泡划分变化（「对话页变了」）。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-4"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    _ev(store, path, "turn_start", sess)
    _ev(store, path, "user_message", sess, content="改一下")
    _ev(store, path, "chunk", sess, text="先看第一段")
    _ev(store, path, "diff", sess, diff=_diff_lines())
    _ev(store, path, "chunk", sess, text="再看第二段")
    _ev(store, path, "turn_end", sess)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    assistant = [m for m in messages if m.get("role") == "assistant"]
    texts = [m.get("text") for m in assistant if m.get("text")]
    assert texts == ["先看第一段", "再看第二段"], texts
    diff_rows = [m for m in assistant if m.get("diff")]
    assert len(diff_rows) == 1
    # diff 夹在两段正文中间：顺序 = 先看第一段 → diff → 再看第二段（与实时一致）
    assert assistant[0]["text"] == "先看第一段"
    assert assistant[1].get("diff") is not None
    assert assistant[2]["text"] == "再看第二段"


def test_mid_turn_followup_users_interleaved_by_seq(coara_home: Path, tmp_path: Path) -> None:
    """同回合跟话：用户行按 view_seq 插在前后段助手产出之间，不得覆盖成单条。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-followup"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    # user_message 可先于 turn_start（新 emit 序）
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"content": "先生成图"})
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"text": "好的，开始"})
    store.append_event(path, kind="files", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"files": [{"file_id": "f1"}], "caption": ""})
    store.append_event(path, kind="user_message", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"content": "再来一张"})
    store.append_event(path, kind="chunk", turn_id="t1", source="web", subject="root",
                       session_id=sess, payload={"text": "第二张好了"})
    store.append_event(path, kind="turn_end", turn_id="t1", source="web", subject="root", session_id=sess)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    roles = [m["role"] for m in messages]
    texts = [m.get("text") for m in messages]
    assert roles == ["user", "assistant", "assistant", "user", "assistant"]
    assert texts[0] == "先生成图"
    assert texts[1] == "好的，开始"
    assert messages[2].get("files")
    assert texts[3] == "再来一张"
    assert texts[4] == "第二张好了"


def test_user_row_carries_client_msg_id_for_hydrate_pairing(coara_home: Path, tmp_path: Path) -> None:
    """端上标识随用户行回落：刷新后的 hydrate 按它配对实时气泡而不是比文本。
    老帧（无标识）不带该键，端上退回兜底判据。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-cid"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    _ev(store, path, "user_message", sess, content="带标识的话", client_msg_id="cid-1")
    _ev(store, path, "turn_start", sess)
    _ev(store, path, "chunk", sess, text="收到")
    _ev(store, path, "user_message", sess, content="老帧没有标识")
    _ev(store, path, "turn_end", sess)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    users = [m for m in messages if m["role"] == "user"]
    assert len(users) == 2
    assert users[0]["client_msg_id"] == "cid-1"
    assert "client_msg_id" not in users[1]


def test_merge_chunks_keeps_module_bubbles(coara_home: Path, tmp_path: Path) -> None:
    """模块会话（merge_chunks=True）保持合并：与其实时追加同一气泡一致。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-5"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()

    _ev(store, path, "turn_start", sess)
    _ev(store, path, "user_message", sess, content="构建一下")
    _ev(store, path, "chunk", sess, text="a 段")
    _ev(store, path, "chunk", sess, text="b 段")
    _ev(store, path, "turn_end", sess)
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50, merge_chunks=True)
    assistant = [m for m in messages if m.get("role") == "assistant" and m.get("text")]
    assert len(assistant) == 1
    assert assistant[0]["text"] == "a 段b 段"


def test_root_view_path_is_workspace_line_not_per_session(coara_home: Path, tmp_path: Path) -> None:
    """空间一条线：root 主对话视图文件按空间寻址（conversation.jsonl），
    不同 session_id 解析到同一文件——/new 不产生新文件。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    p1 = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="sess-A")
    p2 = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="sess-B")
    assert p1 == p2
    assert p1.name == "conversation.jsonl"
    # 模块会话仍按 section（草案）分文件
    m1 = resolve_web_view_path(ws, coara_home=coara_home, subject="config", session_id="s1")
    m2 = resolve_web_view_path(ws, coara_home=coara_home, subject="config", session_id="s2")
    assert m1 != m2


def test_workspace_line_continuous_across_new_session(coara_home: Path, tmp_path: Path) -> None:
    """空间一条线：/new 是线上一个 divider 帧；同空间两个会话段的消息 + 分隔线
    按时间连续聚合，刷新回放即完整空间线（永久连续）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="sess-A")
    store = WebViewStore()

    # 会话段 A
    store.append_event(path, kind="turn_start", turn_id="tA", source="web", subject="root", session_id="sess-A")
    store.append_event(path, kind="user_message", turn_id="tA", source="web", subject="root",
                       session_id="sess-A", payload={"content": "第一段问题"})
    store.append_event(path, kind="chunk", turn_id="tA", source="web", subject="root",
                       session_id="sess-A", payload={"text": "第一段回答"})
    store.append_event(path, kind="turn_end", turn_id="tA", source="web", subject="root", session_id="sess-A")
    # /new 分隔标记（无 turn_id）
    store.append_event(path, kind="divider", turn_id="", source="web", subject="root",
                       session_id="sess-A", payload={"label": "新会话"})
    # 会话段 B（/new 后）
    store.append_event(path, kind="turn_start", turn_id="tB", source="web", subject="root", session_id="sess-B")
    store.append_event(path, kind="user_message", turn_id="tB", source="web", subject="root",
                       session_id="sess-B", payload={"content": "第二段问题"})
    store.append_event(path, kind="chunk", turn_id="tB", source="web", subject="root",
                       session_id="sess-B", payload={"text": "第二段回答"})
    store.append_event(path, kind="turn_end", turn_id="tB", source="web", subject="root", session_id="sess-B")
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    seq_view = [(m.get("role"), m.get("text"), m.get("divider")) for m in messages]
    assert seq_view == [
        ("user", "第一段问题", None),
        ("assistant", "第一段回答", None),
        ("assistant", "", "新会话"),
        ("user", "第二段问题", None),
        ("assistant", "第二段回答", None),
    ]


def test_make_persist_diff_maps_to_payload_diff(coara_home: Path, tmp_path: Path) -> None:
    """TurnStream emit 展开传的 diff 帧经 make_persist 落盘统一为 payload={"diff": ...}。

    广播帧顶层带 display_blocks/diff_lines（前端/CLI 读），落盘键由 persist
    映射为 diff（build_messages 唯一读取键）——两者分离，刷新与实时都可见。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-6"
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)

    # TurnStream._record 后的帧形状（emit("diff", display_blocks=..., diff_lines=...) 展开）
    persist(
        {
            "type": "diff",
            "display_blocks": [{"kind": "diff", "lines": ["+x"]}],
            "diff_lines": _diff_lines(),
            "tool_name": "edit",
            "turn_id": "t1",
            "source": "web",
            "subject": "root",
            "session_id": sess,
            "seq": 1,
        }
    )
    store.flush(timeout=2.0)
    store.close()

    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    frames = WebViewStore.iter_frames(path)
    diff_frames = [f for f in frames if f.get("kind") == "diff"]
    assert len(diff_frames) == 1
    assert diff_frames[0]["payload"]["diff"] == _diff_lines()
    assert "display_blocks" not in diff_frames[0]["payload"]


def test_turn_stream_diff_broadcast_frame_top_level_fields() -> None:
    """diff 帧广播形状契约：emit 展开传，广播帧顶层带 display_blocks/diff_lines。

    三端消费端（web store case "diff" 读 diff_lines、CLI queue_diff_frame 读
    display_blocks）都只读顶层——曾因 sender 用 emit("diff", payload={...})
    把字段包进 payload 键导致三端读不到、diff 全丢（测试桩未走真实 emit 路径
    未暴露）。此测试锁定广播帧形状。
    """
    from types import SimpleNamespace

    from src.ui.turn_stream import TurnStream

    broadcast: list[dict] = []

    class MockServer:
        registry = SimpleNamespace(
            has_active=lambda: True,
            send_to_active_nowait=lambda frame: broadcast.append(frame),
        )
        attach_registry = SimpleNamespace(send_to_nowait=lambda channel, frame: None)

    stream = TurnStream("turn-1", "web", "root", MockServer())  # type: ignore[arg-type]
    stream.emit("diff", display_blocks=[{"kind": "diff", "lines": ["+x"]}], diff_lines=_diff_lines(), tool_name="edit")
    stream._flush_pending()

    assert len(broadcast) == 1
    frame = broadcast[0]
    assert frame["type"] == "diff"
    assert frame["display_blocks"] == [{"kind": "diff", "lines": ["+x"]}]
    assert frame["diff_lines"] == _diff_lines()
    # 不允许 payload 包裹：消费端读顶层，包裹必丢
    assert "payload" not in frame or "diff_lines" not in frame.get("payload", {})


def test_tool_line_frame_lands_between_chunk_rows(coara_home: Path, tmp_path: Path) -> None:
    """工具行帧落盘后按 view_seq 插在正文段落之间（刷新回放与实时同构）。

    回合时序 a 段正文 → 工具行 → b 段正文：回放必须是「正文、工具行、正文」，
    而不是正文连成一片、工具行堆在末尾。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sess = "sess-tool"
    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id=sess)
    store = WebViewStore()
    store.append_event(path, kind="turn_start", turn_id="t1", source="web", subject="root", session_id=sess)
    _ev(store, path, "chunk", sess, text="先看一下代码")
    _ev(store, path, "tool", sess, text="read(src/a.py)", ok=True, tool_name="read", duration_ms=120.0)
    _ev(store, path, "tool", sess, text="edit(src/a.py)", ok=False, tool_name="edit", duration_ms=8.0)
    _ev(store, path, "chunk", sess, text="改完了")
    store.flush(timeout=2.0)
    store.close()

    messages, _total, _latest = WebViewStore.build_messages(path, limit=50)
    shape = [
        ("text", m.get("text")) if not m.get("tool") else ("tool", m["tool"]["label"])
        for m in messages
        if m.get("text") or m.get("tool")
    ]
    assert shape == [
        ("text", "先看一下代码"),
        ("tool", "read(src/a.py)"),
        ("tool", "edit(src/a.py)"),
        ("text", "改完了"),
    ]
    tools = [m["tool"] for m in messages if m.get("tool")]
    assert tools[0]["ok"] is True
    assert tools[1]["ok"] is False
    assert tools[0]["duration_ms"] == 120.0


def test_tool_line_broadcast_and_persist_shape(coara_home: Path, tmp_path: Path) -> None:
    """工具行帧形状契约：广播帧顶层带 text/ok（前端 case "tool" 直读），
    落盘经 make_persist 保留同名字段（build_messages 唯一读取键）。"""
    from types import SimpleNamespace

    from src.ui.turn_stream import TurnStream

    broadcast: list[dict] = []

    class MockServer:
        registry = SimpleNamespace(
            has_active=lambda: True,
            send_to_active_nowait=lambda frame: broadcast.append(frame),
        )
        attach_registry = SimpleNamespace(send_to_nowait=lambda channel, frame: None)

    stream = TurnStream("turn-1", "web", "root", MockServer())  # type: ignore[arg-type]
    stream.emit("tool", text="read(src/a.py)", ok=True, tool_name="read", tool_call_id="c1", duration_ms=120.0)
    stream._flush_pending()

    assert len(broadcast) == 1
    frame = broadcast[0]
    assert frame["type"] == "tool"
    assert frame["text"] == "read(src/a.py)"
    assert frame["ok"] is True
    assert frame["tool_call_id"] == "c1"
    assert "payload" not in frame

    ws = tmp_path / "ws"
    ws.mkdir()
    store = WebViewStore()
    persist = store.make_persist(ws, coara_home=coara_home)
    persist({**frame, "subject": "root", "session_id": "sess-b"})
    store.flush(timeout=2.0)
    store.close()

    path = resolve_web_view_path(ws, coara_home=coara_home, subject="root", session_id="sess-b")
    frames = [f for f in WebViewStore.iter_frames(path) if f.get("kind") == "tool"]
    assert len(frames) == 1
    payload = frames[0]["payload"]
    assert payload["text"] == "read(src/a.py)"
    assert payload["ok"] is True
    assert payload["tool_name"] == "read"
