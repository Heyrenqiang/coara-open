"""Tests for the execution sandbox and call-layer policy."""

from __future__ import annotations

from src.tools.sandbox import Sandbox, configure_sandbox, get_sandbox


class TestSandbox:
    """Unit tests for the Sandbox execution interceptor."""

    def test_command_blocking(self) -> None:
        sandbox = Sandbox(
            {
                "enabled_for_untrusted": True,
                "blocked_commands": ["rm", "sudo", "mkfs"],
            }
        )
        assert sandbox.check_command("rm file") is not None
        assert sandbox.check_command("sudo apt install") is not None
        assert sandbox.check_command("cat file") is None
        assert sandbox.check_command("npm test") is None

    def test_command_shell_metacharacters(self) -> None:
        sandbox = Sandbox({"enabled_for_untrusted": True, "blocked_commands": ["rm"]})
        assert sandbox.check_command("cat file | rm file") is not None
        assert sandbox.check_command("cat file && rm file") is not None

    def test_command_single_ampersand_blocked(self) -> None:
        """PowerShell single '&' (call operator / cmd separator) is blocked."""
        sandbox = Sandbox({"enabled_for_untrusted": True})
        assert sandbox.check_command("cmd1 & cmd2") is not None
        assert sandbox.check_command("cmd1 && cmd2") is not None
        assert sandbox.check_command("cat file") is None
        assert sandbox.check_command("npm test") is None

    def test_command_windows_backslash(self) -> None:
        """Windows paths ending with a backslash must not raise shlex errors."""
        sandbox = Sandbox({"enabled_for_untrusted": True})
        assert sandbox.check_command("dir D:\\") is None
        assert sandbox.check_command("dir D:\\ /S /B") is None

    def test_path_blocking(self) -> None:
        sandbox = Sandbox(
            {
                "enabled_for_untrusted": True,
                "blocked_paths": [".env", "*.pem", "/etc/*"],
            }
        )
        assert sandbox.check_path(".env") is not None
        assert sandbox.check_path("/etc/passwd") is not None
        assert sandbox.check_path("src/main.py") is None

    def test_url_blocking(self) -> None:
        sandbox = Sandbox(
            {
                "enabled_for_untrusted": True,
                "blocked_hosts": ["192.168.*", "10.*", "127.0.0.1"],
            }
        )
        assert sandbox.check_url("http://192.168.1.1/") is not None
        assert sandbox.check_url("http://example.com/") is None
        assert sandbox.check_url("http://localhost/") is not None

    def test_env_sanitization(self) -> None:
        sandbox = Sandbox(
            {
                "enabled_for_untrusted": True,
                "blocked_env": ["ANTHROPIC_API_KEY", "*TOKEN*", "*SECRET*"],
            }
        )
        env = {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "secret",
            "MY_TOKEN": "x",
            "NORMAL": "ok",
        }
        sanitized = sandbox.sanitize_env(env)
        assert "PATH" in sanitized
        assert "ANTHROPIC_API_KEY" not in sanitized
        assert "MY_TOKEN" not in sanitized
        assert "NORMAL" in sanitized

    def test_enabled_only_for_untrusted(self) -> None:
        sandbox = Sandbox({"enabled_for_untrusted": True})
        assert sandbox.is_enabled("untrusted") is True
        assert sandbox.is_enabled("owner") is False

    def test_disabled_when_config_false(self) -> None:
        sandbox = Sandbox({"enabled_for_untrusted": False})
        assert sandbox.is_enabled("untrusted") is False
        assert sandbox.is_enabled("owner") is False


class TestSandboxGlobal:
    """Tests for the global sandbox singleton."""

    def test_configure_and_get(self) -> None:
        cfg = {
            "enabled_for_untrusted": True,
            "blocked_commands": ["rm"],
        }
        sb = configure_sandbox(cfg)
        assert sb is get_sandbox()
        assert get_sandbox().is_enabled("untrusted")

    def teardown_method(self) -> None:
        # Reset global singleton between tests
        from src.tools import sandbox as sandbox_module

        sandbox_module._sandbox_instance = None
