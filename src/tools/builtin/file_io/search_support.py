"""Shared helpers for grep/glob workspace search tools."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_RG_EXECUTABLE: str | bool | None = None

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".coara",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
MAX_RESULTS_CAP = 500
DEFAULT_MAX_RESULTS = 50

# os.walk 预算（深度/广度封顶）：防止超大仓库扫爆
DEFAULT_WALK_MAX_DEPTH = 64
DEFAULT_WALK_MAX_DIRECTORIES = 10_000
DEFAULT_WALK_MAX_ENTRIES = 50_000

GLOB_TRUNCATION_HINT = "... 结果已截断；请收窄 pattern（如 src/**/*.py）减少匹配数"
GREP_TRUNCATION_HINT = "... 结果已截断；请收窄 pattern 或增大 offset 翻页"
WALK_TRUNCATION_HINT = "... 遍历已达预算上限（深度/目录数/条目数）；请收窄 path 或 pattern 后重试"


@dataclass(slots=True)
class WalkBudget:
    """Recursive walk caps for glob / grep Python fallback."""

    max_depth: int = DEFAULT_WALK_MAX_DEPTH
    max_directories: int = DEFAULT_WALK_MAX_DIRECTORIES
    max_entries: int = DEFAULT_WALK_MAX_ENTRIES


@dataclass(slots=True)
class WalkStatus:
    truncated: bool = False
    reason: str = ""  # max_directories | max_entries


def iter_budgeted_walk(
    root: Path,
    *,
    budget: WalkBudget | None = None,
    status: WalkStatus | None = None,
):
    """Yield ``(current_root, dirnames, filenames)`` with prune + walk budget.

    When the budget trips, *status.truncated* is set and iteration stops.
    Depth beyond ``max_depth`` only prunes that branch (does not mark truncated).
    """
    budget = budget or WalkBudget()
    status = status if status is not None else WalkStatus()
    dir_count = 0
    entry_count = 0

    for current_root, dirnames, filenames in os.walk(root, topdown=True):
        prune_walk_dirs(dirnames)
        try:
            rel = Path(current_root).relative_to(root)
            depth = len(rel.parts)
        except ValueError:
            depth = 0
        if depth >= budget.max_depth:
            dirnames.clear()
            continue

        dir_count += 1
        if dir_count > budget.max_directories:
            status.truncated = True
            status.reason = "max_directories"
            dirnames.clear()
            break

        # Count this directory node + its immediate children as walk entries.
        entry_count += 1 + len(dirnames) + len(filenames)
        if entry_count > budget.max_entries:
            status.truncated = True
            status.reason = "max_entries"
            dirnames.clear()
            break

        yield current_root, dirnames, filenames


def resolve_search_root(params: dict[str, Any], workspace_root: Path | None) -> str:
    """Return explicit or default workspace-absolute search root."""
    raw = params.get("path", params.get("root"))
    if raw is None or not str(raw).strip():
        return str((workspace_root or Path.cwd()).resolve())
    return str(raw)


def validate_search_directory(root: Path) -> str | None:
    """Return an error when *root* is missing or not a directory."""
    if not root.exists():
        return f"路径不存在: {root}"
    if not root.is_dir():
        return f"不是目录: {root}"
    return None


def validate_search_path(root: Path) -> str | None:
    """Return an error when *root* does not exist."""
    if not root.exists():
        return f"路径不存在: {root}"
    return None


def relocate_missing_search_path(
    root: Path,
    workspace_root: Path | None,
    *,
    max_candidates: int = 20,
) -> list[Path]:
    """路径不存在时按末级名字在 workspace 内定位同名候选（文件或目录）。

    grep 类工具的自我纠偏（对齐 edit 失配自动带上下文的策略）：LLM 凭记忆
    猜错路径时按名字扫一把——唯一候选可直接重试，多候选列出供选择，
    省一轮「报错 → 人工 glob」往返。只读操作， bounded walk。
    """
    name = root.name
    if not name or workspace_root is None:
        return []
    ws = Path(workspace_root)
    try:
        if not ws.is_dir():
            return []
    except OSError:
        return []
    candidates: list[Path] = []
    status = WalkStatus()
    for current_root, dirnames, filenames in iter_budgeted_walk(ws, status=status):
        for d in dirnames:
            if d == name:
                candidates.append(Path(current_root) / d)
        for fn in filenames:
            if fn == name:
                candidates.append(Path(current_root) / fn)
        if len(candidates) >= max_candidates:
            break
    return candidates[:max_candidates]


def resolve_workspace_search_root(
    raw_root: str,
    workspace_root: Path | None,
    action: str,
    *,
    vfs_resolver: Any | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve and validate a search root; read-only search allows paths outside workspace."""
    from src.tools.builtin.file_io.file_support import (
        normalize_workspace_root,
        resolve_workspace_path_from_root,
    )

    root, _, path_error = resolve_workspace_path_from_root(
        raw_root,
        normalize_workspace_root(workspace_root),
        action,
        vfs_resolver=vfs_resolver,
        read_only=True,
    )
    if path_error:
        return None, path_error
    assert root is not None
    return root, None


OutputMode = Literal["content", "files_with_matches", "count"]

# Ripgrep -t aliases (subset of common types; unknown types pass through as globs).
RG_TYPE_ALIASES: dict[str, str] = {
    "py": "py",
    "python": "py",
    "js": "js",
    "javascript": "js",
    "ts": "ts",
    "typescript": "ts",
    "tsx": "ts",
    "jsx": "js",
    "rust": "rust",
    "go": "go",
    "java": "java",
    "kotlin": "kt",
    "kt": "kt",
    "md": "md",
    "markdown": "md",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "xml": "xml",
    "html": "html",
    "css": "css",
    "sql": "sql",
    "sh": "sh",
    "bash": "sh",
    "c": "c",
    "cpp": "cpp",
    "h": "h",
    "rb": "ruby",
    "ruby": "ruby",
    "php": "php",
    "swift": "swift",
    "scala": "scala",
}


@dataclass(slots=True)
class SearchLimits:
    max_results: int = DEFAULT_MAX_RESULTS
    offset: int = 0

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> SearchLimits:
        raw_limit = params.get(
            "limit",
            params.get("head_limit", params.get("max_results", DEFAULT_MAX_RESULTS)),
        )
        raw_offset = params.get("offset", 0)
        try:
            max_results = max(1, min(int(raw_limit), MAX_RESULTS_CAP))
        except (TypeError, ValueError):
            max_results = DEFAULT_MAX_RESULTS
        try:
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            offset = 0
        return cls(max_results=max_results, offset=offset)


@dataclass(slots=True)
class GrepParams:
    pattern: str
    root: str = ""
    glob: str | None = None
    case_sensitive: bool = False
    output_mode: OutputMode = "files_with_matches"
    context_lines: int = 0
    multiline: bool = False
    file_type: str | None = None
    limits: SearchLimits = field(default_factory=SearchLimits)

    @classmethod
    def from_tool_params(cls, params: dict[str, Any], *, workspace_root: Path | None = None) -> GrepParams:
        if "pattern" not in params:
            raise ValueError("Missing required parameter: pattern")

        output_mode = str(params.get("output_mode", "files_with_matches"))
        if output_mode not in ("content", "files_with_matches", "count"):
            raise ValueError("output_mode must be content, files_with_matches, or count")

        context_lines = _parse_nonneg_int(params.get("context_lines", params.get("-C", 0)))
        if context_lines <= 0:
            before = _parse_nonneg_int(params.get("context_before", params.get("-B", 0)))
            after = _parse_nonneg_int(params.get("context_after", params.get("-A", 0)))
            if before > 0 or after > 0:
                context_lines = max(before, after)

        glob_val = params.get("glob", params.get("glob_file_search"))
        if glob_val is None:
            glob_filter: str | None = None
        elif isinstance(glob_val, str) and not glob_val.strip():
            glob_filter = None
        else:
            glob_filter = str(glob_val)

        root = resolve_search_root(params, workspace_root)
        file_type = params.get("type")
        if isinstance(file_type, str) and not file_type.strip():
            file_type = None

        case_sensitive = not bool(params.get("-i")) if "-i" in params else bool(params.get("case_sensitive", False))

        return cls(
            pattern=params["pattern"],
            root=root,
            glob=glob_filter,
            case_sensitive=case_sensitive,
            output_mode=output_mode,  # type: ignore[arg-type]
            context_lines=context_lines,
            multiline=bool(params.get("multiline", False)),
            file_type=str(file_type).strip() if file_type else None,
            limits=SearchLimits.from_params(params),
        )


def _parse_nonneg_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def find_rg_executable() -> str | None:
    global _RG_EXECUTABLE
    if _RG_EXECUTABLE is False:
        return None
    if isinstance(_RG_EXECUTABLE, str):
        return _RG_EXECUTABLE
    found = shutil.which("rg")
    _RG_EXECUTABLE = found if found else False
    return found


def normalize_find_pattern(pattern: str) -> str:
    if "/" not in pattern and not pattern.startswith("**"):
        return f"**/{pattern}"
    return pattern


def resolve_rg_type(file_type: str | None) -> tuple[str | None, str | None]:
    """Return (rg -t type, fallback glob) for a type hint."""
    if not file_type:
        return None, None
    key = file_type.strip().lower().lstrip(".")
    if key in RG_TYPE_ALIASES:
        return RG_TYPE_ALIASES[key], None
    if key.startswith("*"):
        return None, key
    return None, f"**/*.{key}"


def prune_walk_dirs(dirnames: list[str]) -> None:
    dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES and not name.startswith("."))


def iter_glob_paths(
    root: Path,
    pattern: str,
    *,
    kind: str,
    budget: WalkBudget | None = None,
    status: WalkStatus | None = None,
):
    """Yield paths under *root* matching *pattern*, skipping common vendor dirs."""
    normalized = normalize_find_pattern(pattern)
    for current_root, dirnames, filenames in iter_budgeted_walk(root, budget=budget, status=status):
        current = Path(current_root)
        candidates: list[Path] = []
        if kind in ("any", "dir"):
            candidates.extend(current / name for name in dirnames)
        if kind in ("any", "file"):
            candidates.extend(current / name for name in filenames)
        for path in candidates:
            if path == root:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if path_matches_find_pattern(rel, normalized):
                yield path


def compile_grep_pattern(pattern: str, *, case_sensitive: bool, multiline: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    if multiline:
        flags |= re.DOTALL | re.MULTILINE
    return re.compile(pattern, flags)


def path_matches_glob(rel_posix: str, glob_pattern: str | None) -> bool:
    if not glob_pattern or glob_pattern in ("**/*", "*"):
        return True
    rel = Path(rel_posix)
    patterns = [glob_pattern]
    if glob_pattern.startswith("**/"):
        patterns.append(glob_pattern[3:])
    return any(rel.match(candidate) for candidate in patterns)


def path_matches_find_pattern(rel: Path, pattern: str) -> bool:
    """Match a walked path against a glob pattern (supports root-level files)."""
    patterns = [pattern]
    if pattern.startswith("**/"):
        patterns.append(pattern[3:])
    return any(rel.match(candidate) for candidate in patterns)


def append_truncation_footer(
    lines: list[str],
    *,
    shown: int,
    total: int | None,
    offset: int,
    unit: str = "matches",
    hint: str | None = None,
) -> list[str]:
    if hint:
        lines.append(hint)
    elif total is not None and shown < total:
        lines.append(f"... 已截断，显示 {shown}/{total} 条（offset={offset}）")
    elif shown > 0:
        lines.append(f"... 已截断；增大 offset（当前 {offset}）获取更多结果")
    return lines
