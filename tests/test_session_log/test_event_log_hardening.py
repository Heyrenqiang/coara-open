"""事件溯源转正后的行为守护测试（对齐旧行为硬约束）。

覆盖三类曾出现/可能出现的偏差：
- ``has_real_conversation_in_history`` jsonl 分支必须按 session_id 过滤
  （/new 后旧会话事件不得污染新会话判定，对齐旧单会话快照语义）
- ``append_with_seq`` 写失败必须返回 False（可观测），且 seq 缓存回退
- recorder ``sync_history`` 写失败不推进投影游标（下次重对账，不静默丢增量）
"""

from __future__ import annotations

from pathlib import Path


def _seed_events(path: Path, events: list[dict]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def test_jsonl_branch_filters_by_session_id(tmp_path: Path) -> None:
    """jsonl 含旧会话真实对话时，新 session_id 的判定必须为 False。"""
    from src.coara.workspace_protocol import has_real_conversation_in_history

    events_file = tmp_path / "session_events.jsonl"
    _seed_events(
        events_file,
        [
            {
                "seq": 1,
                "kind": "user/message",
                "session_id": "old-sid",
                "payload": {"content": "帮我干活"},
            },
            {
                "seq": 2,
                "kind": "assistant/message",
                "session_id": "old-sid",
                "payload": {"content": "好的"},
            },
        ],
    )

    # 旧会话视角：有真实对话
    assert has_real_conversation_in_history(str(events_file), session_id="old-sid")
    # 新会话视角（/new 后）：旧事件不得计入
    assert not has_real_conversation_in_history(str(events_file), session_id="new-sid")


def test_jsonl_branch_env_seed_only_not_conversation(tmp_path: Path) -> None:
    """当前会话只有 env seed 事件时应判 False（与旧快照语义一致）。"""
    from src.coara.workspace_protocol import has_real_conversation_in_history

    events_file = tmp_path / "session_events.jsonl"
    _seed_events(
        events_file,
        [
            {
                "seq": 1,
                "kind": "user/message",
                "session_id": "s1",
                "payload": {"content": "环境上下文：\n- 今天日期：xxx"},
            },
            # 其它会话的噪声（tool result）也不得计入
            {
                "seq": 2,
                "kind": "tool/result",
                "session_id": "other",
                "payload": {"content": "x"},
            },
        ],
    )
    assert not has_real_conversation_in_history(str(events_file), session_id="s1")


def test_append_with_seq_reports_failure_and_rolls_back_cache(tmp_path: Path) -> None:
    """写失败返回 False；seq 缓存回退，恢复后不重复分配已尝试的 seq。"""
    from src.session_log import store

    path = tmp_path / "session_events.jsonl"
    assert store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message"}])

    def _deny(path: Path):
        original = Path.open

        def _blocked(self, *args, **kwargs):
            if self == path and args and args[0] == "a":
                raise OSError("disk full")
            return original(self, *args, **kwargs)

        return _blocked

    import json

    try:
        Path.open = _deny(path)  # type: ignore[method-assign]
        ok = store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message"}])
    finally:
        Path.open = original_open  # type: ignore[assignment]

    assert ok is False
    assert store._seq_cache.get(path) is None
    # 文件内容未被污染，且恢复后分配点从磁盘重扫（seq 不重复）
    ok2 = store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message"}])
    assert ok2
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [e["seq"] for e in lines] == [1, 2]


original_open = Path.open


def test_sync_history_keeps_cursor_on_write_failure(tmp_path: Path) -> None:
    """sync 写失败不推进 _synced_keys：同批差异下次重对账（不静默丢增量）。"""
    from src.core.types import Message, MessageRole
    from src.session_log.recorder import SessionLogRecorder

    recorder = SessionLogRecorder(
        workspace_dir=tmp_path,
        session_id="s1",
        coara_home=tmp_path,
    )
    history = [
        Message(role=MessageRole.USER, content="你好"),
        Message(role=MessageRole.ASSISTANT, content="在"),
    ]
    recorder.sync_history(history)
    # 正常写入后游标已推进
    assert recorder._synced_keys  # noqa: SLF001

    # 构造追加失败：把 jsonl 所在目录的文件写权限改为拒绝（Windows 上用只读属性不可靠，
    # 直接 monkeypatch store.append_with_seq 返回 False 更聚焦游标语义）
    import src.session_log.recorder as recorder_mod

    original = recorder_mod.append_with_seq

    def _fail(path, factory):  # noqa: ANN001
        return False

    history3 = [*history, Message(role=MessageRole.USER, content="继续")]
    recorder_mod.append_with_seq = _fail  # type: ignore[assignment]
    try:
        recorder.sync_history(history3)
    finally:
        recorder_mod.append_with_seq = original  # type: ignore[assignment]

    # 游标未推进到 history3：下次（真实）sync 应补记差异
    recorder.sync_history(history3)
    assert len(recorder._synced_keys) == 3  # noqa: SLF001


def test_append_isolates_crash_half_line(tmp_path: Path) -> None:
    """崩溃半行残骸不得粘连吞掉后续事件（断电/强杀场景的录像带自保红线）。

    进程在上次 write 中途被杀会留下无换行半行；追加前必须补换行隔离残骸，
    否则新事件粘在残骸尾部整行解析失败被静默跳过——事件丢失 + seq 幽灵缺口。
    """
    from src.session_log import store

    path = tmp_path / "session_events.jsonl"
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message", "payload": {"content": "a"}}])
    # 模拟崩溃：写入一个无换行的半截 JSON 残骸
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 999, "kind": "user/messa')
    # 崩溃后首次追加：残骸须被换行隔离，新事件完整可投影
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message", "payload": {"content": "good"}}])
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message", "payload": {"content": "good3"}}])

    seqs = [e.get("seq") for e in store.iter_events(path)]
    assert seqs == [1, 2, 3]  # 残骸(999)被隔离跳过 后续事件完整 seq 连续无缺口


def test_interleaved_recorders_keep_seq_globally_unique(tmp_path: Path) -> None:
    """多 recorder 交错写同一带后 seq 严格单调唯一（seq 唯一性契约红线）。

    真实带曾出现重复 seq（4778–4781 各 2 份）：主会话与子智能体的 recorder
    交错写时，一方的 append_events 未推进另一方已建立的 seq 缓存。现有
    ``_advance_seq_cache`` 是堵此洞的补丁——本测试固化为契约，防回归。
    """
    from src.session_log import store

    path = tmp_path / "session_events.jsonl"
    # 主会话 recorder：append_with_seq 建立缓存
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "turn/start", "session_id": "main"}])
    # 子智能体等另一写者：append_events 直通（带自己的 seq）——必须推进主缓存
    store.append_events(path, [{"seq": 2, "kind": "turn/end", "session_id": "sub"}])
    store.append_events(path, [{"seq": 3, "kind": "turn/end", "session_id": "sub"}])
    # 主会话再分配：必须从全局最大 seq 续 而非缓存旧值
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "user/message", "session_id": "main"}])
    store.append_with_seq(path, lambda seq: [{"seq": seq, "kind": "assistant/message", "session_id": "main"}])

    seqs = [int(e.get("seq") or 0) for e in store.iter_events(path)]
    assert len(seqs) == len(set(seqs)), f"seq 重复: {seqs}"
    assert seqs == sorted(seqs), f"seq 非单调: {seqs}"
    assert seqs == [1, 2, 3, 4, 5]
