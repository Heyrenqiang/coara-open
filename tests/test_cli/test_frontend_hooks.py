"""CLI frontend hook wiring for attach diff rendering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.cli.display_controller import CliDisplayController
from src.cli.frontend_hooks import wire_cli_frontend_hooks
from src.coara.frontend import get_frontend
from src.coara.tool_output.types import DiffDisplayBlock


def test_wire_cli_frontend_hooks_registers_scrollback() -> None:
    wire_cli_frontend_hooks(console=SimpleNamespace())
    frontend = get_frontend()
    assert frontend.scrollback_write_renderable is not None
    assert frontend.scrollback_write is not None
    assert frontend.get_diff_colors() is not None
    assert frontend.display_width("abc") == 3


def test_flush_pending_fg_diffs_writes_renderable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach diff flush must paint via wired frontend hooks, not no-op defaults."""
    rendered: list[object] = []
    monkeypatch.setattr(
        "src.cli.scrollback.CliScrollback.write_renderable",
        lambda view: rendered.append(view),
    )
    monkeypatch.setattr("src.cli.scrollback.CliScrollback.write", lambda *args, **kwargs: None)
    wire_cli_frontend_hooks(console=SimpleNamespace())

    ctl = CliDisplayController(
        root=SimpleNamespace(
            foreground_coara=SimpleNamespace(
                session_id="sess-main",
                workspace_dir="D:/ws/a",
                identity=SimpleNamespace(coara_id="coara-main"),
            ),
            identity=SimpleNamespace(coara_id="root-1"),
            has_active_turn=lambda: False,
        ),
        console=SimpleNamespace(),
        subagent_spinner=SimpleNamespace(),
        background_spinner=SimpleNamespace(request_redraw=lambda **kwargs: None),
    )
    block = DiffDisplayBlock(path="demo.txt", old_text="a\n", new_text="b\n")
    ctl.queue_diff_frame({"display_blocks": [block.to_dict()]})
    ctl.flush_pending_fg_diffs()
    assert rendered, "expected diff panel renderable to be written to scrollback"
