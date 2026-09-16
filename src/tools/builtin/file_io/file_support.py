"""Shared helpers for text file tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.json_store import write_bytes_atomic
from src.core.logger import logger
from src.core.text import absolute_path_error as absolute_path_error
from src.core.text import is_absolute_path as is_absolute_path
from src.tools.cache import tool_cache


@dataclass
class FileFormatInfo:
    """Encoding and line-ending style for a text file."""

    encoding: str = "utf-8"
    bom: bytes = b""
    line_ending: str = "\n"


@dataclass
class FileReadState:
    """In-memory snapshot captured by read/write/edit tools (content + mtime cache)."""

    content: str
    timestamp: int
    file_size: int
    format_info: FileFormatInfo = field(default_factory=FileFormatInfo)


def path_outside_workspace_mounts(
    vfs_resolver: Any | None,
    workspace_root: Path,
    raw_path: Any,
) -> bool:
    """True when *raw_path* is a valid absolute path outside every mounted workspace.

    Used by write-class tools to declare ``requires_approval`` — out-of-mount
    writes go through the approval gate instead of being hard-denied.
    Vault paths are excluded (gated by the vault flow, not workspace mounts).
    """
    from src.vault.guard import is_under_vault_open, vault_path_denied

    candidate = str(raw_path or "").strip()
    if not candidate or candidate.startswith(("\\\\", "//")):
        return False
    if not Path(candidate).expanduser().is_absolute():
        return False
    try:
        target = Path(candidate).expanduser().resolve()
    except OSError:
        return False
    if vault_path_denied(target) or is_under_vault_open(target):
        return False
    if vfs_resolver is not None:
        return vfs_resolver.mount_for_path(target) is None
    try:
        target.relative_to(Path(workspace_root).resolve())
        return False
    except ValueError:
        return True


def resolve_workspace_path(
    raw_path: str,
    workspace_root: Path,
    action: str,
    *,
    vfs_resolver: Any | None = None,
    read_only: bool = False,
) -> tuple[Path | None, str | None]:
    """Resolve a path inside the workspace and reject obviously risky variants.

    When ``read_only`` is True (read/grep/glob), absolute paths may lie outside
    ``workspace_root``; write/edit/delete keep the workspace boundary.
    Encrypted vault roots are always denied (password gate via vault tool only).
    """
    from src.vault.guard import vault_path_denied

    if vfs_resolver is not None and not read_only:
        try:
            resolved = vfs_resolver.resolve(raw_path, action=action)
        except Exception as exc:
            from src.workspace.vfs import UnmountedPathError

            # 越界写入不硬拒：非 strict 解析器下放行到通用绝对路径处理，
            # 由工具类 requires_approval 触发审批门；strict（delegate 作用域）保持拒绝。
            if isinstance(exc, UnmountedPathError) and not getattr(vfs_resolver, "strict", False):
                pass
            else:
                return None, f"{action} 访问被拒绝: {exc}"
        else:
            denied = vault_path_denied(resolved.path)
            if denied:
                return None, f"{action} 被拒绝: {denied}"
            return resolved.path, None

    if vfs_resolver is not None and read_only:
        try:
            resolved = vfs_resolver.resolve(raw_path, action=action)
            denied = vault_path_denied(resolved.path)
            if denied:
                return None, f"{action} 被拒绝: {denied}"
            return resolved.path, None
        except Exception:
            logger.debug(f"vfs resolve 失败（只读工具放行到绝对路径处理）: {raw_path}")
            # fall through — allow absolute paths outside mounts for read-only tools

    workspace_root = workspace_root.resolve()
    candidate = str(raw_path or "").strip()
    if not candidate:
        return None, f"{action} 路径不能为空"

    abs_error = absolute_path_error(candidate, action)
    if abs_error:
        return None, abs_error

    if candidate.startswith(("\\\\", "//")):
        return None, f"{action} 拒绝 UNC 路径: {raw_path}"

    if candidate.startswith(("\\\\?\\", "//?/")):
        return None, f"{action} 拒绝 Windows 扩展路径: {raw_path}"

    if _has_windows_alternate_data_stream(candidate):
        return None, f"{action} 拒绝备用数据流路径: {raw_path}"

    expanded = Path(candidate).expanduser()
    path = expanded.resolve()

    denied = vault_path_denied(path)
    if denied:
        return None, f"{action} 被拒绝: {denied}"

    # 越界（挂载表 / 工作区根之外）的写入不再在此硬拒，
    # 由工具类 requires_approval 在审批门处理；宝箱目录始终受 vault 门控。
    return path, None


def resolve_workspace_path_from_root(
    raw_path: str,
    workspace_root: Path,
    action: str,
    *,
    vfs_resolver: Any | None = None,
    read_only: bool = False,
) -> tuple[Path | None, Path, str | None]:
    """Resolve an absolute workspace path. Rejects relative paths and paths outside the root."""
    workspace_root = workspace_root.resolve()
    path, path_error = resolve_workspace_path(
        raw_path,
        workspace_root,
        action,
        vfs_resolver=vfs_resolver,
        read_only=read_only,
    )
    return path, workspace_root, path_error


def normalize_workspace_root(workspace_root: Path | None) -> Path:
    """Return a resolved workspace root, defaulting to the current working directory."""
    return Path(workspace_root or Path.cwd()).resolve()


def normalize_read_state_store(
    read_state_store: dict[str, FileReadState] | None,
) -> dict[str, FileReadState]:
    """Return a mutable read-state store for tools that require one."""
    return read_state_store if read_state_store is not None else {}


#: Default size cap for whole-file text reads (100MB). ``edit`` aligns its
#: rejection threshold to this value instead of bypassing it with ``max_bytes=None``.
DEFAULT_MAX_READ_BYTES = 100_000_000


def read_text_file(path: Path, max_bytes: int | None = DEFAULT_MAX_READ_BYTES) -> tuple[str, FileFormatInfo]:
    """Read a text file, preserving encoding and original line-ending style.

    Args:
        path: Path to the file.
        max_bytes: Optional maximum file size in bytes. If the file exceeds
            this limit, a ``ValueError`` is raised to prevent OOM.
    """
    if max_bytes is not None:
        file_size = path.stat().st_size
        if file_size > max_bytes:
            raise ValueError(
                f"File too large ({file_size} bytes, limit: {max_bytes} bytes). "
                "Use read(path, offset=<line>, limit=<lines>) for a smaller slice."
            )
    raw = path.read_bytes()
    format_info = _detect_file_format(raw)

    try:
        decoded = raw[len(format_info.bom) :].decode(format_info.encoding)
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(exc.encoding, exc.object, exc.start, exc.end, exc.reason) from exc

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, format_info


def write_text_file(path: Path, content: str, format_info: FileFormatInfo | None = None) -> None:
    """Write text while preserving the original encoding and line-ending style."""
    from src.vault.guard import mutate_vault_open_tree

    format_info = format_info or FileFormatInfo()
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if format_info.line_ending == "\r\n":
        normalized = normalized.replace("\n", "\r\n")
    raw = format_info.bom + normalized.encode(format_info.encoding)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, raw)

    mutate_vault_open_tree(path, _write)


def current_timestamp(path: Path) -> int:
    return path.stat().st_mtime_ns


def current_file_size(path: Path) -> int:
    return path.stat().st_size


def is_snapshot_current(path: Path, read_state: FileReadState) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return stat.st_mtime_ns == read_state.timestamp and stat.st_size == read_state.file_size


def read_state_key(path: Path) -> str:
    """Canonical read-state store key (resolved absolute path string)."""
    return str(Path(path).resolve())


def update_read_state(
    read_states: dict[str, FileReadState] | None,
    path: Path,
    *,
    content: str,
    format_info: FileFormatInfo | None = None,
) -> FileReadState:
    state = FileReadState(
        content=content,
        timestamp=current_timestamp(path),
        file_size=current_file_size(path),
        format_info=format_info or FileFormatInfo(),
    )
    if read_states is not None:
        read_states[read_state_key(path)] = state
    return state


def refresh_snapshot_after_write(
    read_states: dict[str, FileReadState] | None,
    path: Path,
    *,
    content: str,
    format_info: FileFormatInfo | None = None,
) -> FileReadState:
    state = update_read_state(
        read_states,
        path,
        content=content,
        format_info=format_info,
    )
    invalidate_read_cache_for_path(path)
    return state


def invalidate_read_cache_for_path(path: Path) -> int:
    target = str(path)
    removed = tool_cache.invalidate_matching(
        "read",
        lambda params: params.get("path") == target,
    )
    logger.debug(f"read_cache_invalidate path={target} removed={removed}")
    return removed


def invalidate_read_caches_for_paths(paths: Iterable[Path | str]) -> int:
    removed = 0
    for path in paths:
        removed += invalidate_read_cache_for_path(Path(path))
    return removed


def log_file_cache_event(event: str, path: Path, **details: object) -> None:
    parts = [f"path={path}"]
    for key, value in details.items():
        parts.append(f"{key}={value}")
    logger.debug(f"{event} " + " ".join(parts))


def load_text_with_snapshot_fallback(
    path: Path,
    read_state: FileReadState | None,
    *,
    tool_name: str,
) -> tuple[str, FileFormatInfo]:
    if read_state is not None and is_snapshot_current(path, read_state):
        log_file_cache_event("snapshot_hit", path, tool=tool_name)
        return read_state.content, read_state.format_info

    log_file_cache_event("disk_read_fallback", path, tool=tool_name, reason="missing_or_stale_snapshot")
    return read_text_file(path, max_bytes=None)


def _detect_file_format(raw: bytes) -> FileFormatInfo:
    if raw.startswith(b"\xef\xbb\xbf"):
        return FileFormatInfo(
            encoding="utf-8",
            bom=b"\xef\xbb\xbf",
            line_ending=_detect_line_ending(raw[3:].decode("utf-8", errors="replace")),
        )
    if raw.startswith(b"\xff\xfe"):
        return FileFormatInfo(
            encoding="utf-16-le",
            bom=b"\xff\xfe",
            line_ending=_detect_line_ending(raw[2:].decode("utf-16-le", errors="replace")),
        )
    if raw.startswith(b"\xfe\xff"):
        return FileFormatInfo(
            encoding="utf-16-be",
            bom=b"\xfe\xff",
            line_ending=_detect_line_ending(raw[2:].decode("utf-16-be", errors="replace")),
        )

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded, encoding = _decode_legacy_text(raw)
        return FileFormatInfo(encoding=encoding, bom=b"", line_ending=_detect_line_ending(decoded))

    return FileFormatInfo(encoding="utf-8", bom=b"", line_ending=_detect_line_ending(decoded))


_LEGACY_DETECT_SAMPLE_BYTES = 2_000_000
_BINARY_SNIFF_BYTES = 8192
#: Strict-decode candidates tried in order for non-UTF-8 text. CJK first
#: (cp1252 accepts nearly every byte sequence, so it must come last).
_LEGACY_CANDIDATE_ENCODINGS = ("gb18030", "big5", "cp1252")


def _decode_legacy_text(raw: bytes) -> tuple[str, str]:
    """Decode non-UTF-8 text; reject true binaries."""
    sniff = raw[:_BINARY_SNIFF_BYTES]
    if b"\x00" in sniff or _has_binary_control_chars(sniff):
        raise UnicodeDecodeError("utf-8", raw, 0, 1, "binary content")
    for encoding in _LEGACY_CANDIDATE_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes
    except ImportError as exc:
        raise UnicodeDecodeError("utf-8", raw, 0, 1, "unknown legacy encoding") from exc
    sample = raw if len(raw) <= _LEGACY_DETECT_SAMPLE_BYTES else raw[:_LEGACY_DETECT_SAMPLE_BYTES]
    best = from_bytes(sample).best()
    if best is None or not best.encoding or best.percent_chaos > 30.0:
        raise UnicodeDecodeError("utf-8", raw, 0, 1, "unknown legacy encoding")
    encoding = best.encoding
    try:
        return raw.decode(encoding), encoding
    except (LookupError, UnicodeDecodeError):
        return raw.decode(encoding, errors="replace"), encoding


def _has_binary_control_chars(sample: bytes) -> bool:
    if not sample:
        return False
    allowed = {9, 10, 12, 13}
    control = sum(1 for b in sample if (b < 32 and b not in allowed) or b == 127)
    return control / len(sample) > 0.05


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _has_windows_alternate_data_stream(path_str: str) -> bool:
    if len(path_str) < 3:
        return False
    # Support both ``D:\`` and ``D:/`` style Windows absolute paths.
    if path_str[0].isalpha() and path_str[1] == ":" and path_str[2] in ("\\", "/"):
        return ":" in path_str[3:]
    # POSIX absolute paths: a leading ``/`` means any colon is an ordinary
    # filename character (e.g. ``/tmp/weird:name.txt``), not a Windows ADS.
    if path_str.startswith("/"):
        return False
    return ":" in path_str
