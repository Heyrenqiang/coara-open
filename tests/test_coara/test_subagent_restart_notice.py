"""子智能体启动恢复：写重启清算通知，系统自恢复角色（janitor/daily）除外。"""

from __future__ import annotations

from pathlib import Path

from src.coara.subagent_store import SubagentRecord, SubagentStore
from src.coara.workspace_state import consume_restart_notice


def _record(agent_id: str, subagent_type: str, status: str) -> SubagentRecord:
    now = "2026-08-19T00:00:00"
    return SubagentRecord(
        agent_id=agent_id,
        subagent_type=subagent_type,
        description="调研报告",
        message_history=[],
        status=status,
        created_at=now,
        updated_at=now,
        session_id="s1",
        child_coara_id="c-" + agent_id,
    )


def test_subagent_recovery_writes_restart_notice(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    store = SubagentStore(ws, coara_home=home)
    store.save(_record("sa-coaras-abc12345", "coaras", "running_background"))
    store.save(_record("sa-janitor-def67890", "janitor", "running_background"))
    store.save(_record("sa-coaras-done00000", "coaras", "completed"))

    assert store.recover_stale_running() == 2

    notice = consume_restart_notice(ws, coara_home=home)
    assert notice is not None
    items = [str(it) for it in notice["items"]]
    assert any("sa-coaras-abc12345" in it and "调研报告" in it for it in items)
    # janitor/daily 会自行重跑，不进注记
    assert not any("janitor" in it for it in items)
    assert consume_restart_notice(ws, coara_home=home) is None


def test_subagent_recovery_no_notice_when_nothing_recovered(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    store = SubagentStore(ws, coara_home=home)
    store.save(_record("sa-coaras-done00000", "coaras", "completed"))

    assert store.recover_stale_running() == 0
    assert consume_restart_notice(ws, coara_home=home) is None
