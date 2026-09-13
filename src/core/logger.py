"""
coara v8 - 日志系统

使用 loguru 提供结构化日志。
所有运行数据存放在当前工作空间文件夹下。

级别约定（交互终端默认 ``console_level=WARNING``，文件默认 ``log_level=INFO``）：

- DEBUG — 开发探针；``COARA_DEBUG=1`` 时控制台才出完整堆栈
- INFO — 可自动恢复的运行痕迹（如 LLM 瞬时重试）；进文件，不刷对话屏
- WARNING — 需留意但仍在继续的情况；默认会打到控制台
- ERROR — 操作失败；控制台 + 文件；用户可见失败另有友好提示
"""

import contextlib
import sys
import traceback
from pathlib import Path

from loguru import logger

from src.core.coara_home import CoaraHomePaths
from src.core.error_log import log_session_error_event

_CONSOLE_LOG_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _console_tracebacks_enabled() -> bool:
    """控制台是否输出完整堆栈：仅 COARA_DEBUG=1。文件日志始终保留完整堆栈。"""
    import os

    return os.environ.get("COARA_DEBUG") == "1"


def _console_format(record) -> str:
    """控制台模板：默认不含 {exception}，屏幕只见一行原因，不刷大段堆栈。"""
    template = _CONSOLE_LOG_FORMAT + "\n"
    if record["exception"] is not None and _console_tracebacks_enabled():
        template += "{exception}\n"
    return template


def _make_console_rich_handler(rich_console):
    """RichHandler 变体：非调试模式下剥掉 exc_info，控制台不渲染堆栈框。"""
    import logging

    from rich.logging import RichHandler

    class _ConsoleRichHandler(RichHandler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info and not _console_tracebacks_enabled():
                record = logging.makeLogRecord({**record.__dict__, "exc_info": None, "exc_text": None})
            super().emit(record)

    return _ConsoleRichHandler(
        console=rich_console,
        show_time=True,
        show_path=True,
        rich_tracebacks=True,
    )


def _console_filter(record) -> bool:
    """用户侧已分类错误（log_agent_error，agent_error 标记）不打 console；
    文件日志与 error_log 仍保留。避免刷用户屏，只留友好提示。"""
    return not record["extra"].get("agent_error")


def _format_loguru_exception(record) -> str | None:
    """把 loguru 记录的异常渲染为 traceback 文本。"""
    exc = record.get("exception")
    if exc is None or exc.type is None:
        return None
    return "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))


def _make_error_log_sink(*, workspace_dir: Path | None, coara_home: Path | None):
    """ERROR 级日志统一落 errors.jsonl：一处收口，全系统覆盖。

    显式路径（工具失败、LLM/回合失败）已由调用方自己写结构化记录，
    这里跳过带 ``agent_error`` 标记的记录，避免重复。
    """

    def sink(message) -> None:
        try:
            record = message.record
            extra = record.get("extra") or {}
            if extra.get("agent_error"):
                return
            target = extra.get("workspace_dir") or workspace_dir
            if target is None:
                return
            metadata = {
                key: value
                for key, value in extra.items()
                if key not in {"workspace_dir", "session_id", "coara_id", "coara_name", "event", "agent_error"}
            }
            log_session_error_event(
                workspace_dir=Path(target),
                session_id=str(extra.get("session_id") or ""),
                coara_id=str(extra.get("coara_id") or ""),
                coara_name=str(extra.get("coara_name") or ""),
                event=str(extra.get("event") or record.get("name") or "log_error"),
                message=str(record.get("message") or ""),
                metadata=metadata or None,
                traceback=_format_loguru_exception(record),
                coara_home=coara_home,
            )
        except Exception:
            # 错误日志写入绝不能反噬正常流程
            return

    return sink


def setup_logger(
    log_level: str = "INFO",
    log_file: Path | None = None,
    enable_file_logging: bool = True,
    enable_console: bool = True,  # 新增：是否启用控制台输出
    workspace_dir: Path | None = None,
    coara_home: Path | None = None,
    rich_console=None,
    clear_on_start: bool = False,
    console_level: str | None = None,  # 控制台级别；缺省与 log_level 相同
) -> None:
    """
    配置日志系统。

    Args:
        log_level: 文件日志级别 (DEBUG, INFO, WARNING, ERROR)
        log_file: 日志文件路径，默认 ``<workspace>/logs/coara.log``（coara_home 模式）
                  或 ``<workspace>/.coara/logs/coara.log``（本地模式）
        enable_file_logging: 是否启用文件日志
        enable_console: 是否启用控制台日志输出
        workspace_dir: 工作区目录，用于确定日志存放位置
        coara_home: 可选的用户级 coara Home；配置后日志写入其中
        rich_console: 可选的 Rich Console 实例。提供时，console log 会通过 RichHandler
                      输出，与 Rich Live/Status 协调，不会破坏 spinner 显示。
        console_level: 控制台日志级别，缺省跟随 log_level。交互终端通常传 WARNING，
                      让 INFO 只进文件，避免刷屏影响对话体验。
    """
    # 移除默认 handler
    logger.remove()
    console_level = console_level or log_level

    # 控制台输出（带颜色）- 可选
    if enable_console:
        if rich_console is not None:
            try:
                logger.add(
                    _make_console_rich_handler(rich_console),
                    level=console_level,
                    format=_console_format,
                    filter=_console_filter,
                )
            except Exception:
                # Fallback to stderr if RichHandler fails
                logger.add(
                    sys.stderr,
                    level=console_level,
                    format=_console_format,
                    colorize=True,
                    filter=_console_filter,
                )
        else:
            logger.add(
                sys.stderr,
                level=console_level,
                format=_console_format,
                colorize=True,
                filter=_console_filter,
            )

    # 文件输出 - 默认存放在当前工作空间文件夹
    if enable_file_logging:
        if log_file is None:
            if workspace_dir:
                paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=coara_home)
                log_file = paths.logs_dir / "coara.log"
            else:
                # 回退到当前工作目录
                log_file = Path.cwd() / ".coara" / "logs" / "coara.log"

        log_file.parent.mkdir(parents=True, exist_ok=True)

        if clear_on_start and log_file.exists():
            log_file.write_text("")

        logger.add(
            str(log_file),
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation="10 MB",
            retention="7 days",
            compression="zip",
            enqueue=True,
            mode="a",
        )

    # 错误收口：ERROR 级日志（含未捕获异常）统一写入 errors.jsonl，与 coara.log 同目录
    if enable_file_logging:
        with contextlib.suppress(Exception):
            logger.add(
                _make_error_log_sink(workspace_dir=workspace_dir, coara_home=coara_home),
                level="ERROR",
                format="{message}",
            )

    # Startup note: DEBUG so WARNING/ERROR console modes stay quiet.
    logger.debug("Logger initialized with level={}", log_level)


# 导出 logger 实例
def log_agent_error(message: str, *, exc_info: bool = False, **context: object) -> None:
    """Record agent/runtime errors into the workspace error log and coara.log."""
    workspace_dir = context.get("workspace_dir")
    session_id = context.get("session_id")
    coara_id = str(context.get("coara_id", ""))
    coara_name = str(context.get("coara_name", ""))
    event = str(context.get("event", "agent_error"))

    traceback_text: str | None = traceback.format_exc() if exc_info else None

    if workspace_dir and session_id:
        try:
            metadata = {
                key: value
                for key, value in context.items()
                if key not in {"workspace_dir", "session_id", "coara_id", "coara_name", "event"}
            }
            log_session_error_event(
                workspace_dir=Path(workspace_dir),
                session_id=str(session_id),
                coara_id=coara_id,
                coara_name=coara_name,
                event=event,
                message=message,
                metadata=metadata or None,
                traceback=traceback_text,
            )
        except Exception as exc:
            logger.debug(f"session error log write failed: {exc}")

    logger.bind(**context, agent_error=True).opt(exception=exc_info).error(message)


__all__ = ["logger", "setup_logger", "log_agent_error"]
