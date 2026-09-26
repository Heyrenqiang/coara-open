"""Records package — unified store + facade over agent/user origins."""

from __future__ import annotations

from src.records.facade import FacadeResult, RecordsFacade, facade_from_root
from src.records.store import RecordsStore

__all__ = ["FacadeResult", "RecordsFacade", "RecordsStore", "facade_from_root"]
