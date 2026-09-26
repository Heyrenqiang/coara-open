"""Office file build/read errors (Word / Excel / PowerPoint)."""

from __future__ import annotations


class DocumentBuildError(Exception):
    """Raised when document generation fails."""


class DocumentReadError(Exception):
    """Raised when document reading/parsing fails."""
