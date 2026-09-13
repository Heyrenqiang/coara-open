"""CLI rendering layer for slash commands.

This module is a thin adapter between the UI-agnostic command service layer
(:mod:`src.coara.commands`) and the terminal. It:

1. Calls :func:`execute_command` to run the command logic.
2. Renders the returned :class:`CommandResult` with rich markup / colors.

All command *logic* lives in :mod:`src.coara.commands`. This file only does
rendering and terminal-specific interaction.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from src.coara.commands import CommandResult, execute_command

console = Console()


async def handle_chat_command(
    root,
    user_input: str,
    workspace: Path,
) -> bool:
    """Dispatch a slash command and render its result.

    Returns True if the session should exit.
    """
    result = await execute_command(root, user_input)
    if result is None:
        # Not a slash command — shouldn't happen (caller checks startswith "/"),
        # but treat as "not handled" so the caller forwards to LLM.
        return False

    _render_result(result)
    return result.exit_session


def _render_result(result: CommandResult) -> None:
    """Render a CommandResult to the terminal with appropriate colors."""
    from src.cli.theme import fg as _fg

    data = result.data

    if data.get("error"):
        console.print(f"[{_fg('status.error')}]{result.output}[/{_fg('status.error')}]")
        return

    # 命令请求打开 Web 页面（如 /login）：经系统浏览器打开逻辑
    open_url = data.get("open_url")
    if open_url:
        try:
            from urllib.parse import urlparse

            from src.ui.web_server import open_or_focus_web_ui

            parsed = urlparse(str(open_url))
            token = ""
            if parsed.query:
                from urllib.parse import parse_qs

                token = (parse_qs(parsed.query).get("token") or [""])[0]
            open_or_focus_web_ui(
                url=str(open_url),
                path=parsed.path or "",
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 8080,
                token=token,
            )
        except Exception:
            pass

    action = result.action
    if action == "new_session":
        console.print(f"[{_fg('status.info')}]{result.output}[/{_fg('status.info')}]")
        return
    if action == "switch_workspace":
        console.print(f"[{_fg('status.ok')}]{result.output}[/{_fg('status.ok')}]")
        return
    if action == "restart":
        console.print(f"[{_fg('status.warn')}]{result.output}[/{_fg('status.warn')}]")
        return
    if result.exit_session:
        console.print(f"[{_fg('status.warn')}]{result.output}[/{_fg('status.warn')}]")
        return

    # Default: plain output (may contain multiple lines)
    # Add subtle coloring for known structured outputs
    if data.get("enabled") is True:
        console.print(f"[{_fg('status.ok')}]{result.output}[/{_fg('status.ok')}]")
        return
    if data.get("enabled") is False:
        console.print(f"[{_fg('status.error')}]{result.output}[/{_fg('status.error')}]")
        return

    console.print(result.output)
