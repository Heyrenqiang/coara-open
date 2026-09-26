"""日志格式化不得打断调用方（回归 2026-09-25 压缩降级炸回合）。

事故：上下文压缩到阈值时调 provider，provider 返回 403，异常文本里带
``{'error': {'message': ...}}`` 这类花括号；压缩的降级分支把它内联进 f-string 交给
loguru，loguru 会对 message 再做一次 ``.format()``，于是把 ``{'error': ...}`` 当占位符
解析 → ``KeyError: "'error'"`` 从日志调用处抛出 → 整回合中断。

本文件钉住：内联含花括号的文本不再抛；参数形式照常工作。
"""

from __future__ import annotations

from src.core.logger import _SafeLogger, logger


def test_inline_braces_do_not_raise() -> None:
    """把 provider 错误体这类含花括号的文本内联进消息，不得抛异常。"""
    logger.warning("boom {'error': {'message': 'quota exceeded'}}")
    logger.error('Turn failed: "\'error\'"')
    logger.info("dict repr: {'a': 1, 'b': {'c': 2}}")


def test_param_style_still_works() -> None:
    """参数形式（{} 占位符 + 参数）照常工作。"""
    logger.info("ok {} {}", 1, "x")
    logger.warning("failed: {}", ValueError("some {braces} inside"))


def test_empty_placeholder_without_args_falls_back() -> None:
    """消息里有真占位符却没有参数：兜底转义后写出，不抛。"""
    logger.warning("literal {not_a_field} and {} empty")


def test_chain_methods_keep_the_guard() -> None:
    """链式调用（bind / opt）后仍然受保护。"""
    logger.bind(session_id="s1").opt(exception=False).warning("x {'error': 1}")


def test_proxy_forwards_other_attributes() -> None:
    """非日志方法（remove / add 等）仍然转发到 loguru 本体。"""
    assert isinstance(logger, _SafeLogger)
    assert callable(logger.remove)
    assert callable(logger.add)
