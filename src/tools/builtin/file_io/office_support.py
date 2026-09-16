"""Shared optional-dependency probe for the Office document helpers.

各模块的异常类型与提示文案不同（读失败 / 构建失败 / 编辑失败），这里只统一
「import 探测 + 抛异常」的骨架；异常类型与文案仍由调用方给出，保证对用户的
提示不变。
"""

from __future__ import annotations

import contextlib
import importlib
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def atomic_output_path(target: Path):
    """Office 生成/编辑统一落盘口径：先写同目录临时文件，正常退出才原子替换。

    用法::

        with atomic_output_path(output_path) as tmp:
            document.save(str(tmp))

    中途抛错即删临时文件——目标路径要么保持原样、要么是新内容，不会留半截文档。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        yield temp_path
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def require_pkg(pkg_name: str, exc_cls: type[Exception], hint: str, *extra_modules: str) -> None:
    """Import ``pkg_name`` (plus optional extra modules) or raise ``exc_cls(hint)``."""
    try:
        for module in (pkg_name, *extra_modules):
            importlib.import_module(module)
    except ImportError as exc:
        raise exc_cls(hint) from exc
