"""Shared optional-dependency probe for the Office document helpers.

各模块的异常类型与提示文案不同（读失败 / 构建失败 / 编辑失败），这里只统一
「import 探测 + 抛异常」的骨架；异常类型与文案仍由调用方给出，保证对用户的
提示不变。
"""

from __future__ import annotations

import importlib


def require_pkg(pkg_name: str, exc_cls: type[Exception], hint: str, *extra_modules: str) -> None:
    """Import ``pkg_name`` (plus optional extra modules) or raise ``exc_cls(hint)``."""
    try:
        for module in (pkg_name, *extra_modules):
            importlib.import_module(module)
    except ImportError as exc:
        raise exc_cls(hint) from exc
