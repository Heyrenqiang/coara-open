"""Shared bootstrap helpers for the two Matrix client entry points.

Both ``src.matrix_client.bot.CoaraMatrixBot`` (``python -m src.matrix_client``)
and ``src.cli.matrix_runner.run_matrix_client`` (``coara -x``) assemble the same
client: AsyncClient creation, sync-token load + login + park, file bridge /
notify / mobile-sync wiring, agent discovery, inbound text routing, and the
long-poll sync loop. The verbatim (or parameter-only) pieces live here; each
entry point keeps its own policy at the call site.

Intentional per-entry differences — do NOT unify:

- Mention-strip event rebuild: ``bot.py`` deep-copies ``event.source`` and
  re-parses via ``RoomMessageText.from_dict``; ``matrix_runner.py`` uses a
  dict spread ``{**event.source, "content": {...}}``. Equivalent output,
  different construction.
- Agent-discovery fallback: on /api/agents failure ``bot.py`` assumes it is
  the default agent; ``matrix_runner.py`` keeps its seeded list and, when the
  server returns agents without any default, promotes the descriptor matching
  its own localpart.
- ``matrix_runner.py`` guards dispatch with ``_ensure_joined`` (per-room join
  locks + retry/backoff) before scheduling messages; ``bot.py`` dispatches
  without a join guard.
- Reporting channels differ by design: ``bot.py`` logs via loguru,
  ``matrix_runner.py`` reports to the rich console.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.matrix_client.mention_routing import AgentDescriptor, MentionRoute


def create_matrix_client(*, homeserver: str, user_id: str, device_id: str) -> Any:
    """Create the AsyncClient with the config shared by both entry points."""
    from nio import AsyncClient, AsyncClientConfig

    return AsyncClient(
        homeserver=homeserver,
        user=user_id,
        device_id=device_id,
        config=AsyncClientConfig(
            max_limit_exceeded=5,
            max_timeouts=5,
            store_sync_tokens=False,
            encryption_enabled=False,
        ),
    )


async def login_and_prepare_sync_token(
    client: Any,
    *,
    password: str,
    device_name: str,
    token_path: Path,
    label: str,
    on_parked: Callable[[str], None] | None = None,
) -> Any:
    """Load the saved sync token, log in, and park the token on cold start.

    Returns the raw login response — the caller decides how to report or
    abort. The park runs only after a successful LoginResponse and only when
    no saved token existed; it must happen BEFORE message callbacks are
    registered, otherwise the initial /sync can replay room history into the
    agent as brand-new turns.
    """
    from nio import LoginResponse

    from src.matrix_client.sync_helpers import park_sync_token_without_timeline
    from src.matrix_client.sync_token import load_gomatrix_sync_token

    saved_token = load_gomatrix_sync_token(token_path)
    if saved_token:
        client.next_batch = saved_token

    resp = await client.login(password, device_name=device_name)

    if isinstance(resp, LoginResponse) and not saved_token:
        parked = await park_sync_token_without_timeline(
            client,
            token_path=token_path,
            label=label,
        )
        if parked and on_parked is not None:
            on_parked(parked)
    return resp


def wire_matrix_file_bridge(
    *,
    root: Any,
    client: Any,
    homeserver: str,
    workspace_root: Path,
    ensure_joined: Callable[[str], Awaitable[bool]],
) -> Any:
    """Create the MatrixFileBridge, attach the join strategy, register send_file."""
    from src.matrix_client.file_bridge import MatrixFileBridge
    from src.matrix_client.file_tools import register_remote_file_tools

    bridge = MatrixFileBridge(homeserver=homeserver, client=client)
    bridge.set_ensure_joined(ensure_joined)
    register_remote_file_tools(root, bridge=bridge, workspace_root=workspace_root)
    return bridge


def wire_matrix_notify_pipeline(
    *,
    root: Any,
    client: Any,
    notify_room_id: str,
    coara_home: Path | str | None,
) -> None:
    """Register the notify-room sender and the mobile-sync room sender.

    Both are order-independent setters (one wires ``root.matrix_notify``, the
    other a module-global sender in ``mobile_sync``). Joining the notify room
    and the startup payload push stay with the caller — the two entry points
    intentionally use different join strategies and ordering there.
    """
    from src.coara.mobile_sync import configure_mobile_sync_sender
    from src.matrix_client.ingress_helpers import register_matrix_notify_send
    from src.matrix_client.send_guard import matrix_room_send_text

    register_matrix_notify_send(
        root=root,
        client=client,
        notify_room_id=notify_room_id,
        coara_home=coara_home,
    )
    configure_mobile_sync_sender(lambda room_id, body: matrix_room_send_text(client, room_id, body))


async def fetch_matrix_agents(
    homeserver: str,
    *,
    on_http_error: Callable[[int], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> Any:
    """GET ``{homeserver}/api/agents`` and return the raw ``agents`` list.

    Returns None on non-200 or request failure (callbacks notified). Parsing
    and fallback policy stay with the caller — bot.py and matrix_runner.py
    intentionally differ there (see module docstring).
    """
    import aiohttp

    from src.matrix_client.http_session import shared_matrix_http_session

    url = f"{homeserver.rstrip('/')}/api/agents"
    try:
        session = shared_matrix_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                if on_http_error is not None:
                    on_http_error(resp.status)
                return None
            data = await resp.json()
    except Exception as exc:
        if on_error is not None:
            on_error(exc)
        return None
    return data.get("agents", [])


def warn_if_bot_missing_from_agents(*, bot_localpart: str, agents: list[AgentDescriptor]) -> None:
    """Warn when this bot's localpart is absent from the discovered agent list."""
    if any(a.name.lower() == bot_localpart.lower() for a in agents):
        return
    agent_names = [a.name for a in agents]
    logger.warning(
        f"[Matrix] 本 bot '{bot_localpart}' 不在 /api/agents 列表 "
        f"{agent_names} 中 —— 配置不匹配，未 @mention 的消息将不会被处理"
    )


def route_inbound_text_event(
    *,
    sender: str,
    bot_user_id: str,
    body: str,
    bot_localpart: str,
    known_agents: list[AgentDescriptor],
) -> MentionRoute | None:
    """Self-skip + @mention routing. Returns None when this bot should stay silent."""
    from src.matrix_client.ingress_helpers import should_skip_matrix_self_event
    from src.matrix_client.mention_routing import should_process

    if should_skip_matrix_self_event(sender=sender, bot_user_id=bot_user_id):
        return None
    route = should_process(body, bot_localpart, known_agents)
    if not route.should_process:
        return None
    return route


async def join_pending_invite_rooms(
    client: Any,
    join_room: Callable[[str], Awaitable[None]],
    *,
    label: str,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Full catch-up sync, then join pending invite rooms (best-effort)."""
    from src.matrix_client.sync_helpers import sync_for_pending_invites

    try:
        await sync_for_pending_invites(client, join_room, label=label)
    except Exception as exc:
        if on_error is not None:
            on_error(exc)
        else:
            logger.error(f"[!] Error during initial invite check: {exc}")


async def _homeserver_link_watchdog(
    client: Any,
    *,
    label: str,
    on_link_change: Callable[[bool], None] | None = None,
    interval_seconds: float = 60.0,
) -> None:
    """Periodic homeserver liveness probe (coara→gomatrix leg).

    The sync loop surfaces hard failures but a silently-hung homeserver
    (accepts then never answers) produces nothing. Two consecutive probe
    failures mark the link down; first success marks it back up. Transitions
    are logged and reported via ``on_link_change``.
    """
    import aiohttp

    homeserver = str(getattr(client, "homeserver", "") or "").rstrip("/")
    if not homeserver:
        return
    url = f"{homeserver}/_matrix/client/versions"
    down = False
    misses = 0
    # 看门狗常驻：整生命周期一个会话，避免每 60s 新建一次的周期性卡顿
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            ok = False
            try:
                async with session.get(url) as resp:
                    ok = resp.status == 200
            except asyncio.CancelledError:
                raise
            except Exception:
                ok = False
            if ok:
                misses = 0
                if down:
                    down = False
                    logger.warning("[%s] homeserver link restored (%s)", label, url)
                    if on_link_change is not None:
                        on_link_change(True)
            else:
                misses += 1
                if not down and misses >= 2:
                    down = True
                    logger.warning("[%s] homeserver link DOWN — probe failing (%s)", label, url)
                    if on_link_change is not None:
                        on_link_change(False)
    finally:
        await session.close()


async def run_matrix_client_sync_loop(
    client: Any,
    *,
    token_path: Path,
    label: str,
    on_batch_saved: Callable[[str], None] | None = None,
    on_sync_error: Callable[[object], None] | None = None,
    on_link_change: Callable[[bool], None] | None = None,
    dispatcher: Any | None = None,
) -> None:
    """Run the long-poll sync loop until cancelled (CancelledError swallowed)."""
    import contextlib

    from src.matrix_client.sync_helpers import run_matrix_sync_loop

    watchdog = asyncio.create_task(
        _homeserver_link_watchdog(client, label=label, on_link_change=on_link_change),
        name=f"{label}-link-watchdog",
    )
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await run_matrix_sync_loop(
                client,
                on_batch_saved=on_batch_saved,
                on_sync_error=on_sync_error,
                token_path=token_path,
                timeout_ms=30_000,
                label=label,
                dispatcher=dispatcher,
            )
    finally:
        watchdog.cancel()
