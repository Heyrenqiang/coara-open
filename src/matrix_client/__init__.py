"""Coara Matrix Client — run coara as a native Matrix bot."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.matrix_client.bot import CoaraMatrixBot

__all__ = ["CoaraMatrixBot"]


def __getattr__(name: str) -> object:
    if name == "CoaraMatrixBot":
        from src.matrix_client.bot import CoaraMatrixBot

        return CoaraMatrixBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
