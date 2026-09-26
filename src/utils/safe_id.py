"""Safe filename / storage-key sanitization."""

from __future__ import annotations


def safe_storage_key(name: str) -> str:
    """Keep only alnum, dash, and underscore for on-disk record ids."""
    value = name.strip()
    return "".join(c for c in value if c.isalnum() or c in "-_")
