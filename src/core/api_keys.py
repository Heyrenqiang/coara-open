"""API key 与 env 落点的 core 原语。

「key 是否可用」「``<coara_home>/system/.env`` 在哪」「如何写一条 env 赋值」是
内核（``coara``）、领域层（``llm``）与核心配置共用的口径，下沉在 core——
支撑层与 core 严禁 import ``src.cli``（见 docs/架构契约.md 铁律）。
"""

from __future__ import annotations

from pathlib import Path

# Common docs / template placeholders that must not count as real keys.
PLACEHOLDER_KEYS = frozenset(
    {
        "",
        "your-key",
        "your_key",
        "your-api-key",
        "your_api_key",
        "xxx",
        "xxxx",
        "sk-xxx",
        "sk-xxxx",
        "changeme",
        "change-me",
        "placeholder",
        "todo",
        "none",
        "null",
    }
)


def is_usable_api_key(value: str | None) -> bool:
    """True when the env value looks like a real API key (not empty / placeholder)."""
    raw = (value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered in PLACEHOLDER_KEYS:
        return False
    if lowered.startswith(("your-", "your_", "xxx", "sk-xxx", "sk-your")):
        return False
    return not ("changeme" in lowered or "your_key" in lowered or "your-key" in lowered)


def system_env_path() -> Path:
    """Return ``<coara_home>/system/.env`` (sole API key landing)."""
    # 延迟导入：core.config 反向依赖本模块（迁移内联 key 用这里的工具）
    from src.core.config import _iter_env_file_paths

    return _iter_env_file_paths(None)[0]


def write_api_key(env_path: Path, env_name: str, value: str) -> None:
    """Write or replace an env assignment in the given .env file.

    名沿用历史（首个调用方是 API key），实际是通用 env 赋值写入（邮箱配置、
    Matrix 凭据同样走它）。已有赋值（含注释掉的占位行）原位替换，否则追加。
    经同目录临时文件 + replace 原子落盘，写到一半崩溃不会截断整个 .env。
    """
    from src.core.json_store import write_text_atomic

    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        candidate = line.strip().lstrip("#").strip()
        if candidate.startswith(f"{env_name}=") or candidate.startswith(f"{env_name} ="):
            lines[index] = f"{env_name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_name}={value}")
    write_text_atomic(env_path, "\n".join(lines) + "\n")
