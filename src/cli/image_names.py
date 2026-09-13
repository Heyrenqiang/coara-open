"""Clipboard image naming — monotonically increasing unique display names.

bitmap 图（剪贴板位图）与路径图（微信复制的文件）混用时也保证唯一：
计数器单调递增，删除后再贴不会重名，按名匹配的撤销/裁剪可区分。
"""

from __future__ import annotations

import re
from pathlib import Path

_COUNTER = 0
_USED: set[str] = set()


def next_image_name(path_hint: str | None = None) -> str:
    """Return a unique display name; bitmap → ``图片N``，路径图优先用真实文件名。"""
    global _COUNTER
    base = Path(path_hint).name if path_hint else ""
    if not base:
        _COUNTER += 1
        base = f"图片{_COUNTER}"
    name = base
    n = 2
    while name in _USED:
        stem, dot, suffix = base.rpartition(".")
        name = f"{stem}({n}){dot}{suffix}" if dot else f"{base}({n})"
        n += 1
    _USED.add(name)
    return name


def forget_image_names(names: list[str]) -> None:
    """Drop names no longer pending（删除后可重新复用该名）。"""
    for name in names:
        _USED.discard(name)


_MARKER_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")


def marker_names_in_text(text: str) -> set[str]:
    """Names inside ``[...]`` markers still present in the input text."""
    return set(_MARKER_TOKEN_RE.findall(text))
