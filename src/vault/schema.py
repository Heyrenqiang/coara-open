"""Logical path helpers for vault open-tree files."""

from __future__ import annotations


def normalize_logical_path(value: str) -> str:
    raw = (value or "").replace("\\", "/").strip()
    parts = [p.strip() for p in raw.split("/") if p.strip() and p.strip() != "."]
    if not parts:
        raise ValueError("path must not be empty")
    if any(p == ".." for p in parts):
        raise ValueError("path must not contain '..'")
    return "/".join(parts)
