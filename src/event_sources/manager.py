"""Event source manager: load configs, run sources, enqueue Root messages."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from src.core.coara_home import user_paths
from src.core.logger import logger
from src.event_sources.dedupe import event_dedupe_key, event_paths, path_dedupe_key
from src.event_sources.formatter import render_inbound_message
from src.event_sources.sources.cron import CronSource
from src.event_sources.sources.file_watch import FileWatchSource
from src.event_sources.sources.poll import IntervalPollSource
from src.event_sources.sources.webhook_server import WebhookIngressServer
from src.event_sources.state import EventSourceStateStore
from src.event_sources.types import EventSourceDefinition, EventSourceKind, HandleMode, InboundEvent
from src.workspace.manager import WorkspaceManager
from src.workspace.updates.store import WorkspaceUpdatesStore
from src.workspace.updates.types import WorkspaceUpdate


class EventSourceManager:
    """Load event-source definitions and bridge inbound events into UnifiedScheduler."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        *,
        coara_id: str,
        webhook_host: str = "127.0.0.1",
        webhook_port: int = 8765,
        on_update_stored: Callable[[WorkspaceUpdate], Awaitable[None]] | None = None,
        on_janitor_dispatch: Callable[[WorkspaceUpdate], Awaitable[None]] | None = None,
    ):
        self.workspace_manager = workspace_manager
        self.coara_id = coara_id
        self._on_update_stored = on_update_stored
        self._on_janitor_dispatch = on_janitor_dispatch
        self.definitions: dict[str, EventSourceDefinition] = {}
        self._file_sources: list[FileWatchSource] = []
        self._poll_sources: list[IntervalPollSource] = []
        self._cron_sources: list[CronSource] = []
        self._webhook_server = WebhookIngressServer(
            host=webhook_host,
            port=webhook_port,
            ingest=self.ingest_webhook,
        )
        home = workspace_manager.coara_home.expanduser().resolve()
        state_root = user_paths(home).matters_state_dir
        self._state = EventSourceStateStore(state_root)
        self._updates = WorkspaceUpdatesStore(workspace_manager.coara_home, registry=workspace_manager.registry)
        self._loop: asyncio.AbstractEventLoop | None = None
        # 启动即迁移（幂等）：旧集中布局 → 各空间 .coara/（动态收件箱）
        try:
            self._updates.migrate_legacy_inbox()
        except Exception as exc:
            logger.warning(f"Event-source init: inbox legacy migration failed: {exc}")

    def event_source_dirs(self) -> list[Path]:
        """事件源定义目录：registry 布局扫各空间 ``<ws>/.coara/matters/definitions/``，无则旧单一目录。"""
        from src.event_sources.ops import definition_dirs

        return definition_dirs(self.workspace_manager.coara_home, self.workspace_manager.registry)

    def primary_config_dir(self) -> Path:
        dirs = self.event_source_dirs()
        if dirs:
            return dirs[0]
        return user_paths(self.workspace_manager.coara_home).matters_definitions_dir

    def load_definitions(self) -> None:
        self.definitions.clear()
        seen_ids: set[str] = set()
        for directory in self.event_source_dirs():
            directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(directory.glob("*.yaml")):
                try:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    defn = EventSourceDefinition.model_validate(raw)
                    if defn.id in seen_ids:
                        continue
                    seen_ids.add(defn.id)
                    self.definitions[defn.id] = defn
                except Exception as exc:
                    logger.warning(f"Failed to load event source config {path}: {exc}")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.load_definitions()
        await self._start_sources()

    async def reload(self) -> None:
        """Hot-swap reload: start new sources before stopping old ones.

        file_watch / poll 可双开：新旧源短暂并存，stop→start 窗口不再丢事件；
        双开窗口的重复事件由 EventSourceStateStore 带锁 dedupe（file_watch 的
        路径冷却 + seen_keys、poll 的共享 seen 快照）吸收。
        webhook 服务绑定固定端口无法双开，该类退化为原 stop→start（窗口期
        事件是否补发取决于发送方重试）。
        """
        old_definitions = dict(self.definitions)
        old_file_sources = self._file_sources
        old_poll_sources = self._poll_sources
        old_cron_sources = self._cron_sources

        # webhook 端口独占，不能双开：先摘注册并停服务，新定义在 _start_sources 重建
        self._webhook_server.unregister_all()
        await self._webhook_server.stop()

        self._loop = asyncio.get_running_loop()
        self._file_sources = []
        self._poll_sources = []
        self._cron_sources = []
        self.load_definitions()
        try:
            await self._start_sources()
        except Exception:
            # A failed replacement must not turn a running event system into a
            # partially started one.  Existing file/poll/cron sources are still
            # live, so discard only the replacement and restore the old webhook
            # registrations before propagating the failure.
            await self.stop()
            self.definitions = old_definitions
            self._file_sources = old_file_sources
            self._poll_sources = old_poll_sources
            self._cron_sources = old_cron_sources
            for definition in old_definitions.values():
                if definition.enabled and definition.kind == EventSourceKind.WEBHOOK:
                    self._webhook_server.register(definition)
            try:
                await self._webhook_server.start()
            except Exception as restore_exc:
                logger.error(f"Event-source reload failed and webhook rollback also failed: {restore_exc}")
            raise

        for file_source in old_file_sources:
            file_source.stop()
        for poll_source in old_poll_sources:
            await poll_source.stop()
        for cron_source in old_cron_sources:
            await cron_source.stop()

    async def stop(self) -> None:
        for file_source in self._file_sources:
            file_source.stop()
        self._file_sources.clear()
        for poll_source in self._poll_sources:
            await poll_source.stop()
        self._poll_sources.clear()
        for cron_source in self._cron_sources:
            await cron_source.stop()
        self._cron_sources.clear()
        self._webhook_server.unregister_all()
        await self._webhook_server.stop()

    async def _start_sources(self) -> None:
        vfs = self.workspace_manager.vfs
        # start()/reload() 已先设 self._loop；兜底取当前运行循环，不把可选值透传给要求非空的参数
        loop = self._loop or asyncio.get_running_loop()
        for defn in self.definitions.values():
            if not defn.enabled:
                continue
            # Registry dict keys are workspace_id; event YAML uses the human name.
            if self.workspace_manager.registry.resolve_name_or_id(defn.workspace) is None:
                logger.warning(
                    f"Event source '{defn.id}' skipped: workspace {defn.workspace} not in registry "
                    f"(run: coara ws add <path> --name {defn.workspace})"
                )
                continue
            if defn.kind == EventSourceKind.FILE_WATCH:
                watch_source = FileWatchSource(defn, vfs=vfs, emit=self._handle_event, loop=loop)
                try:
                    watch_source.start()
                    self._file_sources.append(watch_source)
                except Exception as exc:
                    logger.warning(f"Failed to start file watch source '{defn.id}': {exc}")
            elif defn.kind == EventSourceKind.INTERVAL_POLL:
                # 循环变量经默认参数绑定，闭包不在循环结束后捕获（ruff B023）
                def _poll_seen(sid: str = defn.id) -> set[str]:
                    return self._state.poll_snapshot(sid)

                def _save_poll_seen(names: set[str], sid: str = defn.id) -> None:
                    self._state.save_poll_snapshot(sid, names)

                poll_source = IntervalPollSource(
                    defn,
                    vfs=vfs,
                    emit=self._handle_event,
                    get_seen=_poll_seen,
                    save_seen=_save_poll_seen,
                )
                poll_source.start()
                self._poll_sources.append(poll_source)
            elif defn.kind == EventSourceKind.WEBHOOK:
                self._webhook_server.register(defn)
            elif defn.kind == EventSourceKind.CRON:
                try:
                    cron_source = CronSource(defn, emit=self._handle_event)
                    cron_source.start()
                    self._cron_sources.append(cron_source)
                except Exception as exc:
                    logger.warning(f"Failed to start cron source '{defn.id}': {exc}")

        await self._webhook_server.start()

    async def ingest_webhook(self, source_id: str, body: dict[str, Any]) -> None:
        defn = self.definitions.get(source_id)
        if defn is None or defn.kind != EventSourceKind.WEBHOOK:
            raise ValueError(f"Unknown webhook event source '{source_id}'")

        event_type = str(body.get("event_type") or "webhook.received")
        feedback_id = body.get("feedback_id")
        body_id = body.get("id")
        if feedback_id is not None:
            dedupe_key = f"webhook:feedback:{feedback_id}"
        elif body_id:
            dedupe_key = f"webhook:{source_id}:{event_type}:{body_id}"
        else:
            # No id/feedback_id: fall back to a content hash so distinct
            # payloads don't collapse into a single dedupe key.
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            dedupe_key = f"webhook:{source_id}:{event_type}:{digest}"
        event = InboundEvent(
            source_id=source_id,
            workspace=defn.workspace,
            event_type=event_type,
            payload=body,
            dedupe_key=dedupe_key,
        )
        await self._handle_event(event)

    async def _handle_event(self, event: InboundEvent) -> None:
        defn = self.definitions.get(event.source_id)
        if defn is None:
            return
        dedupe_key = event_dedupe_key(event)
        # Hold the store lock across the whole check-and-mark sequence so a
        # concurrent duplicate event cannot pass should_emit before the mark.
        with self._state.locked():
            if not self._state.should_emit(defn.id, dedupe_key, cooldown_seconds=defn.cooldown_seconds):
                return
            for path in event_paths(event):
                path_key = path_dedupe_key(event.workspace, path)
                if not self._state.should_emit_path(path_key, cooldown_seconds=defn.cooldown_seconds):
                    logger.debug(f"Event source '{defn.id}' skipped — path cooldown: {path}")
                    return
            self._state.mark_emitted(defn.id, dedupe_key)
            for path in event_paths(event):
                self._state.mark_path_emitted(path_dedupe_key(event.workspace, path))
        content = render_inbound_message(defn, event)

        # 事实层：所有事件无条件落收件箱，先于任何曝光或派发
        stored = self._updates.append(
            workspace=event.workspace,
            source_id=event.source_id,
            event_type=event.event_type,
            dedupe_key=dedupe_key,
            text=content,
            salience=defn.salience,
            handle_mode=defn.handle.value,
            source_kind="external_sensor",
            expires_at=_update_expires_at(defn, event),
            payload={
                "event": event.model_dump(),
                "routing": {
                    "domain": defn.routing_domain,
                    "suggested_delegate": defn.suggested_delegate,
                },
            },
            type=_update_type_for(defn),
            payload_ref=_update_payload_ref(event),
        )
        if stored is not None:
            logger.info(f"Event source '{defn.id}' → updates @{event.workspace} ({stored.message_id})")
            if self._on_update_stored is not None:
                await self._on_update_stored(stored)

        # 处理层：handle=janitor 时叫醒管家 janitor 过目处置（处置轨迹写回消息）
        if defn.handle == HandleMode.JANITOR and stored is not None and self._on_janitor_dispatch is not None:
            try:
                await self._on_janitor_dispatch(stored)
            except Exception as exc:
                logger.warning(f"Janitor dispatch callback failed for event '{defn.id}': {exc}")

    def updates_store(self) -> WorkspaceUpdatesStore:
        return self._updates

    def list_status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for defn in self.definitions.values():
            rows.append(
                {
                    "id": defn.id,
                    "enabled": defn.enabled,
                    "kind": defn.kind.value,
                    "workspace": defn.workspace,
                    "salience": defn.salience,
                    "handle": defn.handle.value,
                    "running": defn.enabled
                    and (
                        defn.id in {s.definition.id for s in self._file_sources + self._poll_sources}
                        or (
                            defn.kind == EventSourceKind.WEBHOOK
                            and self._webhook_server.is_running
                            and self._webhook_server.has_source(defn.id)
                        )
                    ),
                    "webhook_url": (
                        f"{self._webhook_server.url_base}/webhook/{defn.id}"
                        if defn.kind == EventSourceKind.WEBHOOK
                        else None
                    ),
                }
            )
        return rows


def _update_type_for(defn: EventSourceDefinition) -> str:
    """Map event-source kind to the updates entry type."""
    if defn.kind == EventSourceKind.WEBHOOK:
        return "webhook"
    if defn.kind == EventSourceKind.CRON:
        return "note"
    return "file_change"


def _update_expires_at(defn: EventSourceDefinition, event: InboundEvent) -> str | None:
    """事件源声明 ttl_seconds 时给出消息保质期（ISO，本地时间）。"""
    if not defn.ttl_seconds or defn.ttl_seconds <= 0:
        return None
    from datetime import datetime, timedelta

    from src.core.time import parse_iso_to_datetime

    base = parse_iso_to_datetime(event.occurred_at) or datetime.now()
    if base.tzinfo is not None:
        base = base.astimezone().replace(tzinfo=None)
    return (base + timedelta(seconds=defn.ttl_seconds)).isoformat()


def _update_payload_ref(event: InboundEvent) -> str | None:
    """详情引用：文件变动取文件路径，webhook 取事件 id，兜底 None。"""
    paths = event_paths(event)
    if paths:
        return str(paths[0])
    for key in ("id", "feedback_id"):
        value = event.payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None
