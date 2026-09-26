"""wdl 家目录与资产路径解析。

解析优先级：WDL_HOME 环境变量 > ~/.wdl。

Layout:
    <wdl_home>/
        ├── providers.yaml    # LLM provider 配置
        ├── triggers.json     # Workflow trigger registry
        ├── instances.db      # SQLite workflow run instances
        └── logs/             # 引擎日志
"""

from __future__ import annotations

import os
from pathlib import Path


def wdl_home() -> Path:
    """Resolve the wdl home directory (WDL_HOME env > ~/.wdl)."""
    env = os.environ.get("WDL_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".wdl").resolve()


def providers_config_path() -> Path:
    """Path of providers.yaml."""
    return wdl_home() / "providers.yaml"


def instances_db_path() -> Path:
    """SQLite database for workflow run instances."""
    return wdl_home() / "instances.db"


def triggers_path() -> Path:
    """Workflow trigger registry JSON file."""
    return wdl_home() / "triggers.json"


def logs_dir() -> Path:
    """Engine log directory."""
    d = wdl_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
