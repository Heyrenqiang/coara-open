"""janitor 维护机制不占有工作空间会话索引的回归测试。

历史 bug：janitor 补扫将会话索引 session_state 占为己有并把 last_updated
推新，启动补扫据此判定「会话有更新」反复触发 janitor——自己制造的更新
又触发自己，死循环。

注：daily 已升级为独立的 internal 系统工作空间主体（user_facing=True，
自己的 workspace_dir），它写自己的 session_state 是正确行为，不在本测试
范围——原「daily 子智能体不占有用户空间索引」的语义随 daily 空间化消失。
"""

from __future__ import annotations

from pathlib import Path

from src.coara.base import CoaraBase
from src.coara.workspace_state import load_session_state, save_session_state
from src.core.types import CoaraPersona, Message, MessageRole
from src.session_log.store import read_events, resolve_session_log_path
from tests.helpers import FakeProvider


def _maintenance_coara(ws: Path, persona_name: str) -> CoaraBase:
    return CoaraBase(
        name=f"sa-{persona_name}-t",
        persona=CoaraPersona(name=persona_name, role="maintainer"),
        workspace_dir=ws,
        provider=FakeProvider([]),
        user_facing=False,
        is_owner_context=True,
    )


def test_janitor_does_not_claim_session_index(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    save_session_state(ws, "main-session-id", coara_home=home)

    janitor = _maintenance_coara(ws, "janitor")
    janitor.message_history.append(Message(role=MessageRole.USER, content="收尾维护"))
    janitor.persist_session_to_disk()

    # 会话索引仍属主会话；janitor 不占有、不推新
    sid, _ = load_session_state(ws, coara_home=home)
    assert sid == "main-session-id"

    # 录像带同步照常（维护过程可审计）
    tape = resolve_session_log_path(ws, coara_home=home)
    events = read_events(tape, session_id=janitor.session_id)
    assert events


def test_daily_workspace_writes_own_session_index(tmp_path: Path, monkeypatch) -> None:
    """daily 空间化后是空间主体（user_facing=True）：写自己的 session_state 是正确行为。

    与原「daily 子智能体不占有用户空间索引」相对——空间化后 daily 有自己的
    workspace_dir（.internal/daily），它的会话索引归自己，不存在「顶掉主会话」。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COARA_HOME", str(home))
    daily_dir = tmp_path / ".internal" / "daily"
    daily_dir.mkdir(parents=True)

    daily = CoaraBase(
        name="daily",
        persona=CoaraPersona(name="daily", role="日常整理"),
        workspace_dir=daily_dir,
        provider=FakeProvider([]),
        user_facing=True,
        is_owner_context=True,
        session_agent_kind="daily",
    )
    daily.message_history.append(Message(role=MessageRole.USER, content="整理记录"))
    daily.persist_session_to_disk()

    sid, _ = load_session_state(daily_dir, coara_home=home)
    assert sid == daily.session_id
