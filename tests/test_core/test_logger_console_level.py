"""Console vs file log level separation (release CLI quiet-console fix)."""

from __future__ import annotations

from src.core.logger import logger, setup_logger


def test_console_level_filters_info_but_keeps_warning(capsys) -> None:
    setup_logger(
        log_level="INFO",
        console_level="WARNING",
        enable_console=True,
        enable_file_logging=False,
    )
    logger.info("info-should-be-hidden")
    logger.warning("warn-should-appear")
    err = capsys.readouterr().err
    assert "info-should-be-hidden" not in err
    assert "warn-should-appear" in err


def test_console_level_defaults_to_log_level(capsys) -> None:
    setup_logger(
        log_level="INFO",
        enable_console=True,
        enable_file_logging=False,
    )
    logger.info("info-visible-by-default")
    err = capsys.readouterr().err
    assert "info-visible-by-default" in err


def teardown_module() -> None:
    # 恢复静默，避免影响其它测试的输出捕获。
    setup_logger(enable_console=False, enable_file_logging=False)
