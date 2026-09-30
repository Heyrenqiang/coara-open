"""Turn output stream: ring buffer + broadcast, decoupled from WS"""

from __future__ import annotations

import asyncio
import collections
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.ui.web_server import WebServer

# Micro-batch window for text / subagent chunks
_TURN_BATCH_WINDOW_S = 0.016
# Ring buffer cap for reconnect replay (drop oldest)
_TURN_BUFFER_MAX = 200


class TurnStream:
    """Turn/standby output stream: emit → buffer → persist → broadcast."""

    def __init__(
        self,
        turn_id: str,
        source: str,
        subject: str,
        server: WebServer,
        channel_id: str = "",
        session_id: str = "",
        workspace_dir: str = "",
        persist: Callable[[dict[str, Any]], int | None] | None = None,
        mark_replayed: bool = False,
    ) -> None:
        self.turn_id = turn_id
        self.source = source
        self.subject = subject
        self.session_id = session_id
        # Frame workspace stamp for client boundary guards
        self.workspace_dir = workspace_dir
        # Awakened turn: client has no main stream; mark replayed for direct render
        self._mark_replayed = mark_replayed
        self._server = server
        # Persist callback → view_seq; default = kernel recorder
        self._persist = persist
        # EndRoute: source+channel_id chooses delivery end
        from src.coara.output_router import EndRoute

        self.route = EndRoute(source=source, channel_id=channel_id)
        self.buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=_TURN_BUFFER_MAX)
        self.seq = 0
        self.done = False
        # Attach reconnect filters replay by pin workspace; web may leave empty
        self.workspace_id: str = ""
        self.task: asyncio.Task[None] | None = None
        # Desk stamp (@daily): CLI prefixes [daily]
        self.desk = ""
        self._pending_text: list[str] = []
        # Subagent buckets keyed by tool_call_id (avoid 1-token-1-frame flood)
        self._pending_subagent: dict[str, dict[str, Any]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None

    def emit_user_message(
        self,
        text: str,
        *,
        attachments: list[Any] | None = None,
        client_msg_id: str | None = None,
    ) -> None:
        """User row (turn start and mid-turn follow-up share the same shape).
        注入信封不落 user 气泡，但作为 kind=inject 帧落带——录像带是全部活动的
        唯一事实源；聊天屏读端（web_views._is_injected_user_frame）照旧拦截不上屏。"""
        from src.ui.web_views import is_injected_user_text, user_frame_display_text

        display = user_frame_display_text(text)
        if is_injected_user_text(display):
            tag = "系统消息"
            stripped = display.lstrip()
            for prefix, name in (
                ("<后台结果>", "后台结果"),
                ("<系统提醒>", "系统提醒"),
                ("<系统消息>", "系统消息"),
                ("<子智能体消息>", "子智能体消息"),
                ("<途中消息>", "途中消息"),
            ):
                if stripped.startswith(prefix):
                    tag = name
                    break
            self.emit("inject", text=display, tag=tag)
            return
        kwargs: dict[str, Any] = {
            "content": display,
            "attachments": list(attachments or []),
        }
        cid = str(client_msg_id or "").strip()
        if cid:
            kwargs["client_msg_id"] = cid
        self.emit("user_message", **kwargs)

    def emit(self, kind: str, **payload: Any) -> None:
        """Record a frame; text/subagent_chunk micro-batch, else immediate."""
        if kind == "chunk":
            if payload.get("block"):
                # Leading newlines force a new block even inside an existing bubble
                self._flush_pending()
                text = str(payload.get("text") or "")
                self._record({"type": "chunk", "text": f"\n\n{text}"})
                return
            self._pending_text.append(str(payload.get("text") or ""))
            self._schedule_flush()
            return
        if kind == "subagent_chunk":
            key = str(payload.get("tool_call_id") or "")
            bucket = self._pending_subagent.get(key)
            if bucket is None:
                from src.core.message_tags import is_preformatted_injection

                bucket = {
                    "texts": [],
                    "fields": {k: v for k, v in payload.items() if k != "text"},
                    # Drop whole bucket if first piece is an inject envelope
                    "drop": is_preformatted_injection(str(payload.get("text") or "")),
                }
                self._pending_subagent[key] = bucket
            bucket["texts"].append(str(payload.get("text") or ""))
            self._schedule_flush()
            return
        self._flush_pending()
        self._record({"type": kind, **payload})

    def _record(self, frame: dict[str, Any]) -> None:
        self.seq += 1
        frame["seq"] = self.seq
        if not frame.get("turn_id"):
            frame["turn_id"] = self.turn_id
        frame.setdefault("source", self.source)
        frame.setdefault("subject", self.subject)
        frame.setdefault("session_id", self.session_id)
        if self.workspace_dir:
            frame.setdefault("workspace_dir", self.workspace_dir)
        if self.desk:
            frame.setdefault("desk", self.desk)
        view_seq = self._persist_frame(frame)
        if view_seq:
            frame["view_seq"] = int(view_seq)
        self.buffer.append(frame)
        self._broadcast(frame)

    def _persist_frame(self, frame: dict[str, Any]) -> int | None:
        """Persist frame; return view_seq. Failures must not break the turn."""
        if self._persist is not None:
            with contextlib.suppress(Exception):
                return self._persist(frame)
            return None
        from src.ui.view_recorder import record_view_frame

        return record_view_frame(frame, coara_home=getattr(self._server, "coara_home", None))

    def _broadcast(self, frame: dict[str, Any]) -> None:
        """Deliver one frame via EndRoute (attach → channel; else active web)."""
        if self._mark_replayed:
            # Awakened: no client main stream; replayed → direct render path
            frame.setdefault("replayed", True)
        if self.route.is_attach:
            self._server.attach_registry.send_to_nowait(self.route.channel_id, frame)
            # attach（CLI）回合的实时帧同样给只读观察者（录像带拖出窗）
            registry = getattr(self._server, "registry", None)
            broadcast = getattr(registry, "broadcast_observers_nowait", None)
            if callable(broadcast):
                broadcast(frame)
            return
        if self._server.registry.has_active():
            self._server.registry.send_to_active_nowait(frame)
        # 只读观察者（录像带拖出窗）收同一份广播帧；替身/精简注册表无此方法时跳过
        broadcast = getattr(self._server.registry, "broadcast_observers_nowait", None)
        if callable(broadcast):
            broadcast(frame)

    def _schedule_flush(self) -> None:
        if self._flush_handle is None:
            loop = asyncio.get_running_loop()
            self._flush_handle = loop.call_later(_TURN_BATCH_WINDOW_S, self._flush_pending)

    def _flush_pending(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._pending_text:
            text = "".join(self._pending_text)
            self._pending_text = []
            if text.strip():
                self._record({"type": "chunk", "text": text})
        if self._pending_subagent:
            pending = self._pending_subagent
            self._pending_subagent = {}
            for bucket in pending.values():
                if bucket.get("drop"):
                    continue
                text = "".join(bucket["texts"])
                if text.strip():
                    self._record({"type": "subagent_chunk", "text": text, **bucket["fields"]})

    def finish(self) -> None:
        """Flush leftover chunks and mark done."""
        self._flush_pending()
        self.done = True

    def replay(self) -> list[dict[str, Any]]:
        """Snapshot of unconsumed buffer events for reconnect."""
        return list(self.buffer)
