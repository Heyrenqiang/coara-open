"""Execution sandbox for coara — applies hard boundaries to untrusted callers.

Execution-time interception sandbox (not OS-level process isolation).

Design principle:
    - Sandbox is a switch: ON for untrusted callers, OFF for owner.
    - It does NOT limit tool visibility or prompt approval.
    - It ONLY intercepts at execution time: if the operation crosses the
      boundary, the tool returns ToolResult.error("Sandbox denied: ...").
    - The command/path/host blacklists are best-effort defense-in-depth,
      NOT a security boundary (second-order interpreters like ``iex`` or
      ``cmd /c`` can still bypass string checks).
"""

from __future__ import annotations

import fnmatch
import ipaddress
from pathlib import Path

from src.core.logger import logger
from src.tools.builtin.runtime.shell_support import shell_lex_split


class Sandbox:
    """Application-level execution sandbox."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.enabled_for_untrusted = cfg.get("enabled_for_untrusted", False)
        self.blocked_commands = {cmd.lower() for cmd in cfg.get("blocked_commands", [])}
        self.blocked_paths = list(cfg.get("blocked_paths", []))
        self.blocked_hosts = list(cfg.get("blocked_hosts", []))
        self.blocked_env = list(cfg.get("blocked_env", []))

    def is_enabled(self, trust_level: str) -> bool:
        """Sandbox is active only for untrusted callers."""
        return self.enabled_for_untrusted and trust_level == "untrusted"

    def check_command(self, command: str) -> str | None:
        """Check if a shell command is blocked by the sandbox.

        Returns None if allowed, or a denial reason string if blocked.
        """
        if not command:
            return None

        parts = shell_lex_split(command)
        if not parts:
            return None

        base_cmd = parts[0].lower()

        # Exact command match
        if base_cmd in self.blocked_commands:
            return f"command '{base_cmd}' is blocked by sandbox"

        # Dangerous flag combinations (defense in depth)
        if base_cmd == "find":
            dangerous = {"-exec", "-execdir", "-ok", "-okdir", "-delete"}
            if any(arg in dangerous for arg in parts[1:]):
                return "find with dangerous flags is blocked by sandbox"

        if base_cmd == "git" and any(arg in parts for arg in ("-c", "--config-env")):
            return "git with config override is blocked by sandbox"

        # Shell metacharacters that could bypass command checks.
        # "&" covers PowerShell's single-& call operator and cmd's command
        # separator; it also subsumes "&&" (substring match).
        if any(tok in command for tok in {"|", "&", ";", "$(", "`", ">", "<"}):
            return "shell metacharacters are blocked by sandbox"

        return None

    def check_path(self, path_str: str) -> str | None:
        """Check if a file path is blocked by the sandbox.

        Supports glob patterns in blocked_paths (e.g., '.env', '*.pem', '/etc/*').
        """
        if not path_str:
            return None

        try:
            path = Path(path_str).expanduser().resolve()
            resolved_str = str(path).replace("\\", "/")
        except (OSError, ValueError):
            path = Path(path_str)
            resolved_str = path_str.replace("\\", "/")

        name = path.name
        original_normalized = path_str.replace("\\", "/")

        for pattern in self.blocked_paths:
            pattern_normalized = pattern.replace("\\", "/")

            # Match against file name
            if fnmatch.fnmatch(name, pattern):
                return f"path '{path}' matches blocked pattern '{pattern}'"

            # Match against resolved full path
            if fnmatch.fnmatch(resolved_str, pattern_normalized):
                return f"path '{path}' matches blocked pattern '{pattern}'"

            # Match against original path string (handles cross-platform paths)
            if fnmatch.fnmatch(original_normalized, pattern_normalized):
                return f"path '{path_str}' matches blocked pattern '{pattern}'"

            # Subpath match (e.g., /etc/* matches /etc/passwd)
            if pattern_normalized.endswith("/*"):
                prefix = pattern_normalized[:-2]
                if resolved_str.startswith(prefix + "/") or original_normalized.startswith(prefix + "/"):
                    return f"path '{path_str}' is inside blocked directory '{prefix}'"

        return None

    def check_url(self, url: str) -> str | None:
        """Check if a URL host is blocked by the sandbox."""
        if not url:
            return None

        from urllib.parse import urlsplit

        parts = urlsplit(url)
        hostname = parts.hostname or ""
        hostname_lower = hostname.lower()

        if hostname_lower in ("localhost", "127.0.0.1", "::1"):
            return "localhost access is blocked by sandbox"

        for pattern in self.blocked_hosts:
            if fnmatch.fnmatch(hostname_lower, pattern.lower()):
                return f"host '{hostname}' matches blocked pattern '{pattern}'"

        # Also check resolved IP for private ranges
        try:
            ip = ipaddress.ip_address(hostname_lower)
            if any(
                (
                    ip.is_private,
                    ip.is_loopback,
                    ip.is_link_local,
                    ip.is_reserved,
                    ip.is_multicast,
                    ip.is_unspecified,
                )
            ):
                return f"private/local address '{hostname}' is blocked by sandbox"
        except ValueError:
            pass

        return None

    # 非可信执行默认剥离的敏感环境变量（密钥/凭据后缀），blocked_env 之外的底线。
    _DEFAULT_SENSITIVE_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL", "_AUTH")

    def sanitize_env(self, env: dict[str, str]) -> dict[str, str]:
        """Return a sanitized copy of the environment for untrusted execution.

        Even with no explicit ``blocked_env`` config, common credential variables
        (``*_KEY``/``*_TOKEN``/``*_SECRET``/…) are stripped so an untrusted command
        cannot read provider keys from the process environment.
        """
        sanitized = {}
        for key, value in env.items():
            key_upper = key.upper()
            blocked = key_upper.endswith(self._DEFAULT_SENSITIVE_ENV_SUFFIXES)

            if not blocked:
                for pattern in self.blocked_env:
                    pat_upper = pattern.upper()
                    if pattern.startswith("*") and pattern.endswith("*"):
                        core = pat_upper[1:-1]
                        if core and core in key_upper:
                            blocked = True
                            break
                    elif fnmatch.fnmatch(key_upper, pat_upper):
                        blocked = True
                        break

            if not blocked:
                sanitized[key] = value

        if len(sanitized) < len(env):
            removed = set(env.keys()) - set(sanitized.keys())
            logger.debug(f"Sandbox sanitized env vars: {removed}")

        return sanitized


# Global singleton — lazily assembled by get_sandbox() from config; configure_sandbox() overrides
_sandbox_instance: Sandbox | None = None


def _sandbox_config_from_security() -> dict:
    """从 core/config 读取沙箱配置（延迟 import，避免 tools→core 顶层环）。"""
    try:
        from src.core.config import config_manager

        return dict(config_manager.get_security_config().get("sandbox", {}))
    except Exception:  # noqa: BLE001 — config 未加载/损坏时回落关闭态
        return {}


def configure_sandbox(config: dict | None) -> Sandbox:
    """Configure the global sandbox instance."""
    global _sandbox_instance
    _sandbox_instance = Sandbox(config)
    logger.info(
        f"Sandbox configured: enabled_for_untrusted={_sandbox_instance.enabled_for_untrusted}, "
        f"blocked_commands={len(_sandbox_instance.blocked_commands)}, "
        f"blocked_paths={len(_sandbox_instance.blocked_paths)}, "
        f"blocked_hosts={len(_sandbox_instance.blocked_hosts)}"
    )
    return _sandbox_instance


def get_sandbox() -> Sandbox:
    """Get the global sandbox instance（未显式配置时从 config 惰性装配）。"""
    global _sandbox_instance
    if _sandbox_instance is None:
        _sandbox_instance = Sandbox(_sandbox_config_from_security())
    return _sandbox_instance


def toggle_sandbox() -> bool:
    """Toggle the global sandbox on/off for untrusted callers.

    Returns the new enabled state.
    """
    global _sandbox_instance
    sandbox = get_sandbox()
    sandbox.enabled_for_untrusted = not sandbox.enabled_for_untrusted
    logger.info(f"Sandbox toggled: enabled_for_untrusted={sandbox.enabled_for_untrusted}")
    return sandbox.enabled_for_untrusted


