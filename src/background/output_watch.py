"""Line-level shell output pattern matching for agent wake."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger


@dataclass(slots=True)
class OutputWatchConfig:
    pattern: str
    debounce_ms: int = 5000
    max_notifications: int = 3
    reason: str = "shell_output"


def parse_output_watch_config(raw: Any) -> OutputWatchConfig | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        pattern = raw.strip()
        if not pattern:
            return None
        return OutputWatchConfig(pattern=pattern)
    if isinstance(raw, dict):
        pattern = str(raw.get("pattern") or "").strip()
        if not pattern:
            return None
        return OutputWatchConfig(
            pattern=pattern,
            debounce_ms=max(0, int(raw.get("debounce_ms", 5000))),
            max_notifications=max(1, int(raw.get("max_notifications", 3))),
            reason=str(raw.get("reason") or "shell_output").strip() or "shell_output",
        )
    return None


def get_shell_notify_config_defaults() -> OutputWatchConfig:
    from src.core.config import config_manager

    cfg = getattr(config_manager, "_config", None)
    shell_raw: dict[str, Any] = {}
    if cfg is not None:
        shell_raw = cfg.runtime_enhancements.shell_notify.model_dump()
    else:
        raw = getattr(config_manager, "_raw_config", {}).get("runtime_enhancements") or {}
        shell_raw = raw.get("shell_notify") or {}
    return OutputWatchConfig(
        pattern="",
        debounce_ms=int(shell_raw.get("default_debounce_ms", 5000)),
        max_notifications=int(shell_raw.get("default_max_notifications", 3)),
    )


def is_shell_notify_enabled() -> bool:
    from src.core.config import config_manager

    cfg = getattr(config_manager, "_config", None)
    if cfg is not None:
        return bool(cfg.runtime_enhancements.shell_notify.enabled)
    raw = getattr(config_manager, "_raw_config", {}).get("runtime_enhancements") or {}
    shell_raw = raw.get("shell_notify") or {}
    return bool(shell_raw.get("enabled", True))


def merge_output_watch_config(raw: Any) -> OutputWatchConfig | None:
    """Parse notify_on_output and apply global debounce/max defaults."""
    parsed = parse_output_watch_config(raw)
    if parsed is None:
        return None
    defaults = get_shell_notify_config_defaults()
    if isinstance(raw, str):
        parsed.debounce_ms = defaults.debounce_ms
        parsed.max_notifications = defaults.max_notifications
    elif isinstance(raw, dict):
        if "debounce_ms" not in raw:
            parsed.debounce_ms = defaults.debounce_ms
        if "max_notifications" not in raw:
            parsed.max_notifications = defaults.max_notifications
    return parsed


@dataclass(slots=True)
class OutputWatchMatcher:
    config: OutputWatchConfig
    task_id: str
    log_path: Path
    on_match: Callable[[dict[str, Any]], None]
    _regex: re.Pattern[str] = field(init=False, repr=False)
    _match_count: int = 0
    _last_match_at: float | None = None
    _recent_lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._regex = re.compile(self.config.pattern)

    def feed_line(self, line: str) -> None:
        text = line.rstrip("\r\n")
        self._recent_lines.append(text)
        if len(self._recent_lines) > 20:
            self._recent_lines.pop(0)
        if self._match_count >= self.config.max_notifications:
            return
        if not self._regex.search(text):
            return
        now = time.monotonic()
        debounce_sec = self.config.debounce_ms / 1000.0
        if self._last_match_at is not None and (now - self._last_match_at) < debounce_sec:
            return
        self._match_count += 1
        self._last_match_at = now
        context = self._context_lines(text)
        payload = {
            "task_id": self.task_id,
            "pattern": self.config.pattern,
            "reason": self.config.reason,
            "matched_line": text,
            "context_lines": context,
            "log_path": str(self.log_path),
            "match_count": self._match_count,
            "max_notifications": self.config.max_notifications,
        }
        try:
            self.on_match(payload)
        except Exception as exc:
            logger.warning(f"Output watch callback failed for {self.task_id}: {exc}")

    def _context_lines(self, matched_line: str) -> list[str]:
        lines = list(self._recent_lines)
        if matched_line not in lines:
            lines.append(matched_line)
        return lines[-12:]

    def write_meta(self, task_dir: Path) -> None:
        meta = {
            "pattern": self.config.pattern,
            "debounce_ms": self.config.debounce_ms,
            "max_notifications": self.config.max_notifications,
            "reason": self.config.reason,
            "match_count": self._match_count,
        }
        write_json_atomic(task_dir / "watch_meta.json", meta)
