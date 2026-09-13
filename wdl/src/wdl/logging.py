"""wdl 日志 — 基于 loguru，CLI 输出走 stderr，可挂文件 sink。"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_configured = False


def setup_logger(*, verbose: bool = False, log_file: Path | str | None = None) -> None:
    """配置全局 logger：默认 INFO 到 stderr；verbose → DEBUG。"""
    global _configured
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> <cyan>{name}:{line}</cyan> {message}",
    )
    if log_file is not None:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        logger.add(p, level="DEBUG", rotation="10 MB", retention=3, encoding="utf-8")
    _configured = True


def get_logger():
    """模块级获取 logger（未 setup 时先按默认配置一次）。"""
    if not _configured:
        setup_logger()
    return logger


__all__ = ["logger", "setup_logger", "get_logger"]
