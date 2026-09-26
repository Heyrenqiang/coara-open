"""处置轨迹与纯规则过目（sweep）的回归测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.workspace.updates.store import WorkspaceUpdatesStore
from src.workspace.updates.types import WorkspaceUpdate


def _append(store: WorkspaceUpdatesStore, workspace: str, key: str, **kwargs) -> WorkspaceUpdate:
    msg = store.append(
        workspace=workspace,
        source_id="test",
        event_type="test.event",
        dedupe_key=key,
        text=f"text {key}",
        payload={},
        **kwargs,
    )
    assert msg is not None
    return msg


def test_disposition_defaults_pending(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k1")
    assert msg.disposition == "pending"
    assert msg.reviewed_by == ""
    assert msg.review_note == ""
    assert msg.expires_at is None
    loaded = store.get(msg.message_id)
    assert loaded is not None
    assert loaded.disposition == "pending"


def test_set_disposition_dismiss_marks_read_and_keeps_trail(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k2")
    updated = store.set_disposition(msg.message_id, "dismissed", reviewed_by="janitor", note="过期")
    assert updated is not None
    assert updated.disposition == "dismissed"
    assert updated.reviewed_by == "janitor"
    assert updated.reviewed_at is not None
    assert updated.review_note == "过期"
    # 勾掉后不再占红点
    assert updated.status == "read"
    assert store.unread_count("shop") == 0


def test_set_disposition_appends_review_history(tmp_path) -> None:
    """每次处置追加一条 review_history 轨迹（by/at/action/note），快捷字段=末条。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k2h")
    store.set_disposition(msg.message_id, "elevated", reviewed_by="janitor", note="需要用户拍板")
    store.set_disposition(msg.message_id, "resolved", reviewed_by="user", note="已处理完")

    loaded = store.get(msg.message_id)
    assert loaded is not None
    assert len(loaded.review_history) == 2
    first, second = loaded.review_history
    assert first.action == "elevated"
    assert first.by == "janitor"
    assert first.note == "需要用户拍板"
    assert second.action == "resolved"
    assert second.by == "user"
    # 快捷字段与末条一致
    assert loaded.reviewed_by == "user"
    assert loaded.review_note == "已处理完"


def test_set_disposition_normalizes_note(tmp_path) -> None:
    """note 规范化：去控制字符、截断到 NOTE_MAX_CHARS。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k2n")
    long_note = "x" * 500
    updated = store.set_disposition(msg.message_id, "resolved", reviewed_by="user", note=f"a\u0000b\n{long_note}")
    assert updated is not None
    assert "\u0000" not in updated.review_note
    assert len(updated.review_note) <= 200
    assert updated.review_history[-1].note == updated.review_note


def test_set_salience_records_editor(tmp_path) -> None:
    """store.set_salience：改优先级并记录修改者。"""
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k2s", salience="normal")
    updated = store.set_salience(msg.message_id, "high", by="janitor")
    assert updated is not None
    assert updated.salience == "high"
    assert updated.salience_by == "janitor"
    assert updated.salience_at is not None
    # 非法 salience 拒绝
    assert store.set_salience(msg.message_id, "urgent", by="janitor") is None
    assert store.get(msg.message_id).salience == "high"  # type: ignore[union-attr]


def test_board_groups_pending_and_reviewed(tmp_path) -> None:
    """过目单分组：pending 待处理；elevated/resolved/dismissed 已处置；归档不出现。"""
    store = WorkspaceUpdatesStore(tmp_path)
    pending = _append(store, "shop", "k20")
    elevated = _append(store, "shop", "k21")
    resolved = _append(store, "shop", "k22")
    dismissed = _append(store, "shop", "k23")
    archived = _append(store, "shop", "k24")
    store.set_disposition(elevated.message_id, "elevated", reviewed_by="janitor", note="呈阅")
    store.set_disposition(resolved.message_id, "resolved", reviewed_by="user", note="处理")
    store.set_disposition(dismissed.message_id, "dismissed", reviewed_by="janitor", note="噪音")
    store.archive(archived.message_id)

    pending_group, reviewed_group = store.board("shop")
    assert {m.message_id for m in pending_group} == {pending.message_id}
    assert {m.message_id for m in reviewed_group} == {
        elevated.message_id,
        resolved.message_id,
        dismissed.message_id,
    }
    assert archived.message_id not in {m.message_id for m in pending_group + reviewed_group}


def test_set_disposition_rejects_unknown_value(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(store, "shop", "k3")
    assert store.set_disposition(msg.message_id, "bogus", reviewed_by="janitor") is None
    assert store.get(msg.message_id).disposition == "pending"  # type: ignore[union-attr]


def test_pending_view_excludes_dismissed_and_resolved_includes_elevated(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    high = _append(store, "shop", "k4", salience="high")
    dismissed = _append(store, "shop", "k5", salience="high")
    resolved = _append(store, "shop", "k6", salience="high")
    elevated = _append(store, "shop", "k7", salience="normal")
    store.set_disposition(dismissed.message_id, "dismissed", reviewed_by="janitor")
    store.set_disposition(resolved.message_id, "resolved", reviewed_by="user")
    store.set_disposition(elevated.message_id, "elevated", reviewed_by="janitor", note="需要用户拍板")

    ids = {m.message_id for m in store.pending()}
    assert high.message_id in ids
    assert elevated.message_id in ids  # 呈阅的上浮，无论显著性
    assert dismissed.message_id not in ids
    assert resolved.message_id not in ids


def test_sweep_dismisses_expired(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    expired = _append(
        store,
        "shop",
        "k8",
        expires_at=(datetime.now() - timedelta(seconds=1)).isoformat(),
    )
    fresh = _append(
        store,
        "shop",
        "k9",
        expires_at=(datetime.now() + timedelta(hours=1)).isoformat(),
    )
    swept = store.sweep()
    swept_ids = {m.message_id for m in swept}
    assert expired.message_id in swept_ids
    assert fresh.message_id not in swept_ids
    loaded = store.get(expired.message_id)
    assert loaded is not None
    assert loaded.disposition == "dismissed"
    assert loaded.reviewed_by == "janitor"
    assert "保质期" in loaded.review_note


def test_sweep_dismisses_stale_low_salience(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    stale_low = _append(store, "shop", "k10", salience="low")
    stale_low.created_at = (datetime.now() - timedelta(days=8)).isoformat()
    store._save(stale_low)
    fresh_low = _append(store, "shop", "k11", salience="low")
    stale_normal = _append(store, "shop", "k12", salience="normal")
    stale_normal.created_at = (datetime.now() - timedelta(days=30)).isoformat()
    store._save(stale_normal)

    swept_ids = {m.message_id for m in store.sweep()}
    assert stale_low.message_id in swept_ids
    assert fresh_low.message_id not in swept_ids
    # normal 显著再老也不动——只有 low 适用超龄规则
    assert stale_normal.message_id not in swept_ids


def test_sweep_never_touches_non_pending(tmp_path) -> None:
    store = WorkspaceUpdatesStore(tmp_path)
    msg = _append(
        store,
        "shop",
        "k13",
        salience="low",
        expires_at=(datetime.now() - timedelta(seconds=1)).isoformat(),
    )
    store.set_disposition(msg.message_id, "elevated", reviewed_by="janitor")
    # elevated 的即使过期也不能被规则勾掉——大臣已呈阅
    assert store.sweep() == []
    loaded = store.get(msg.message_id)
    assert loaded is not None
    assert loaded.disposition == "elevated"


def test_sweep_mixed_naive_and_aware_timestamps_no_typeerror(tmp_path) -> None:
    """created/expires 历史上有 naive（now_iso）与 aware 两种口径，sweep 不得抛 TypeError 中断。"""
    from datetime import UTC

    store = WorkspaceUpdatesStore(tmp_path)
    aware_expired = _append(
        store,
        "shop",
        "k14",
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    aware_fresh = _append(
        store,
        "shop",
        "k15",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    naive_low = _append(store, "shop", "k16", salience="low")
    naive_low.created_at = (datetime.now() - timedelta(days=8)).isoformat()
    store._save(naive_low)
    aware_low = _append(store, "shop", "k17", salience="low")
    aware_low.created_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store._save(aware_low)

    swept_ids = {m.message_id for m in store.sweep()}
    assert aware_expired.message_id in swept_ids
    assert naive_low.message_id in swept_ids
    assert aware_low.message_id in swept_ids
    assert aware_fresh.message_id not in swept_ids
