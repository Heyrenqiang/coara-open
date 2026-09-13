"""Shell output wake routes to the launching WorkspaceSession, not the Root host id."""

from __future__ import annotations

from types import SimpleNamespace

from src.coara.root import RootCoara


def _root_with_sessions(
    sessions: dict[str, tuple[str, str]],
    *,
    foreground: str,
    root_coara_id: str = "root-host",
) -> RootCoara:
    """Build a RootCoara shell with {workspace_id: (session_id, coara_id)} peers."""
    root = RootCoara.__new__(RootCoara)
    root._foreground_session_id = foreground
    root._sessions = {
        ws_id: SimpleNamespace(
            coara=SimpleNamespace(
                session_id=session_id,
                identity=SimpleNamespace(coara_id=coara_id),
            )
        )
        for ws_id, (session_id, coara_id) in sessions.items()
    }
    root.identity = SimpleNamespace(coara_id=root_coara_id)
    return root


def _single(*, fg_session_id: str = "sid-fg", fg_coara_id: str = "coara-fg") -> RootCoara:
    return _root_with_sessions({"ws-1": (fg_session_id, fg_coara_id)}, foreground="ws-1")


def test_resolve_by_session_id():
    root = _single()
    target = root._resolve_session_coara_for_event({"session_id": "sid-fg", "coara_id": "coara-fg"})
    assert target is root.foreground_coara


def test_resolve_routes_to_non_foreground_session():
    """A wake for workspace A must land in A's session even when B is foreground."""
    root = _root_with_sessions(
        {"ws-a": ("sid-a", "coara-a"), "ws-b": ("sid-b", "coara-b")},
        foreground="ws-b",
    )
    target = root._resolve_session_coara_for_event({"session_id": "sid-a", "coara_id": "coara-a"})
    assert target is root._sessions["ws-a"].coara
    assert target is not root.foreground_coara


def test_resolve_by_coara_id_when_no_session_id():
    root = _single()
    assert root._resolve_session_coara_for_event({"coara_id": "coara-fg"}) is root.foreground_coara
    # Host Root id still accepted for residual host-stamped (legacy) events.
    assert root._resolve_session_coara_for_event({"coara_id": "root-host"}) is root.foreground_coara
    assert root._resolve_session_coara_for_event({"coara_id": "coara-other"}) is None


def test_resolve_unscoped_defaults_to_foreground():
    root = _single()
    assert root._resolve_session_coara_for_event({}) is root.foreground_coara
    assert root._resolve_session_coara_for_event({"coara_id": "bash-background-runner"}) is root.foreground_coara


def test_resolve_drops_stale_session_id():
    """A task from a previous process run has no live session — drop, don't misroute."""
    root = _single()
    assert root._resolve_session_coara_for_event({"session_id": "sid-gone", "coara_id": "coara-gone"}) is None


def test_resolve_routes_to_running_subagent(monkeypatch):
    """子智能体发起的后台任务，完成通知回到子智能体会话，不升级投主会话。"""
    from src.tools.builtin.delegate import delegate as delegate_mod

    root = _single()
    sub = SimpleNamespace(session_id="sa-x", identity=SimpleNamespace(coara_id="coara-sub-x"))
    monkeypatch.setattr(delegate_mod, "_RUNNING_SUBAGENTS", {"sa-x": sub})
    monkeypatch.setattr(delegate_mod, "_ACTIVE_SUBAGENTS", {})

    target = root._resolve_session_coara_for_event({"session_id": "sa-x", "coara_id": "coara-sub-x"})
    assert target is sub

    # 子智能体已收官（注册表已清）→ 落回 None，由调用方升级到前台
    monkeypatch.setattr(delegate_mod, "_RUNNING_SUBAGENTS", {})
    assert root._resolve_session_coara_for_event({"session_id": "sa-x", "coara_id": "coara-sub-x"}) is None


def test_resolve_does_not_require_root_coara_id_equality():
    """Regression: session-stamped wakes must not be compared only to Root id."""
    root = _single()
    # This is the post-peer-session payload shape from runtime/shell.py.
    target = root._resolve_session_coara_for_event({"session_id": "sid-fg", "coara_id": "coara-fg", "task_id": "t1"})
    assert target is root.foreground_coara
    # Old filter (payload.coara_id != root.identity.coara_id) would have dropped this.
    assert root.identity.coara_id != "coara-fg"
