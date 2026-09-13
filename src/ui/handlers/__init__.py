"""WebServer REST handler mixins — split from ``web_server.py`` by domain."""

from __future__ import annotations

from src.ui.attach_ws import AttachWsHandlers
from src.ui.handlers.commands import CommandsHandlers
from src.ui.handlers.files import FilesHandlers
from src.ui.handlers.records import UsageRecordsHandlers
from src.ui.handlers.session import SessionHandlers
from src.ui.handlers.updates import UpdatesHandlers
from src.ui.handlers.vault import VaultHandlers
from src.ui.handlers.workflow import WorkflowHandlers
from src.ui.handlers.workspace import WorkspaceHandlers
from src.ui.trace_broadcast import TraceBroadcastHandlers

HANDLER_MIXINS: tuple[type, ...] = (
    AttachWsHandlers,
    TraceBroadcastHandlers,
    CommandsHandlers,
    FilesHandlers,
    UsageRecordsHandlers,
    SessionHandlers,
    UpdatesHandlers,
    VaultHandlers,
    WorkflowHandlers,
    WorkspaceHandlers,
)

__all__ = [
    "AttachWsHandlers",
    "CommandsHandlers",
    "FilesHandlers",
    "HANDLER_MIXINS",
    "SessionHandlers",
    "TraceBroadcastHandlers",
    "UpdatesHandlers",
    "UsageRecordsHandlers",
    "VaultHandlers",
    "WorkflowHandlers",
    "WorkspaceHandlers",
]
