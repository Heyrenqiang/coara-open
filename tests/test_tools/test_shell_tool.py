"""Shell tool: policy helpers and execution integration tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.tools.builtin.runtime.shell import ShellTool, ShellToolInvocation
from src.tools.builtin.runtime.shell_support import (
    decode_subprocess_output,
    shell_lex_split,
)


def test_decode_subprocess_output_replaces_invalid_bytes() -> None:
    decoded = decode_subprocess_output(b"ok\xff\xfe")
    assert decoded.startswith("ok")
    assert "\ufffd" in decoded


def test_shell_lex_split_handles_trailing_backslash() -> None:
    assert shell_lex_split("dir C:\\") == ["dir", "C:\\"]


def test_pipe_allowed_for_common_windows_chains() -> None:
    """Common Windows pipe chains do not require approval (first command is safe)."""
    assert ShellTool.requires_approval({"command": "systeminfo | findstr OS"}) is False
    assert ShellTool.requires_approval({"command": "netstat -an | findstr LISTENING"}) is False
    assert ShellTool.requires_approval({"command": "net user | findstr /i admin"}) is False
    assert ShellTool.requires_approval({"command": "schtasks /query | findstr coara"}) is False
    assert ShellTool.requires_approval({"command": "curl -s https://example.com | findstr title"}) is False
    assert ShellTool.requires_approval({"command": "cd D:\\ws; pytest tests/ -q 2>&1 | Select-Object -Last 1"}) is False


def test_shell_permission_allows_pip() -> None:
    assert ShellTool.requires_approval({"command": "pip --version"}) is False


def test_shell_permission_allows_get_date() -> None:
    assert ShellTool.requires_approval({"command": "Get-Date -Format 'yyyy-MM-dd'"}) is False


def test_shell_permission_allows_whitelisted_pipe_chain() -> None:
    assert ShellTool.requires_approval({"command": "netstat -an | findstr LISTENING"}) is False


@pytest.mark.asyncio
async def test_shell_dir_root_path(tmp_path: Path) -> None:
    result = await ShellToolInvocation({"command": "dir C:\\"}, workspace_root=tmp_path).execute()
    assert not result.is_error
    assert "Invalid parameters" not in str(result.content)


@pytest.mark.asyncio
async def test_shell_type_reads_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("hello-shell", encoding="utf-8")
    result = await ShellToolInvocation(
        {"command": f'type "{target}"'},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert "hello-shell" in str(result.content)


@pytest.mark.asyncio
async def test_shell_get_date_powershell(tmp_path: Path) -> None:
    result = await ShellToolInvocation({"command": "Get-Date -Format 'yyyy-MM-dd'"}, workspace_root=tmp_path).execute()
    assert not result.is_error
    assert "[执行结束，退出码" not in str(result.content) or result.metadata.get("failed") is False


@pytest.mark.asyncio
async def test_shell_get_content_reads_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("hello-ps", encoding="utf-8")
    result = await ShellToolInvocation(
        {"command": f'Get-Content -LiteralPath "{target}"'},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert "hello-ps" in str(result.content)


@pytest.mark.asyncio
async def test_shell_powershell_quoted_pipe(tmp_path: Path) -> None:
    ps_cmd = "Get-Process | Sort-Object CPU -Descending | Select-Object -First 1 | Format-Table -AutoSize"
    result = await ShellToolInvocation({"command": ps_cmd}, workspace_root=tmp_path).execute()
    assert not result.is_error


@pytest.mark.asyncio
async def test_shell_netstat_pipe_findstr(tmp_path: Path) -> None:
    result = await ShellToolInvocation(
        {"command": "netstat -an | findstr LISTENING"},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert "安全拦截" not in str(result.content)


@pytest.mark.asyncio
async def test_shell_curl_pipe_not_blocked_at_execute(tmp_path: Path) -> None:
    result = await ShellToolInvocation(
        {"command": "echo test | findstr test"},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert "安全拦截" not in str(result.content)


@pytest.mark.asyncio
async def test_shell_allows_redirect_write(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    result = await ShellToolInvocation(
        {"command": f'Set-Content -LiteralPath "{target}" -Value "via-shell" -Encoding ascii -NoNewline'},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert target.read_text(encoding="utf-8").strip() == "via-shell"


@pytest.mark.asyncio
async def test_shell_failed_metadata_on_nonzero_exit(tmp_path: Path) -> None:
    result = await ShellToolInvocation(
        {"command": "exit 3"},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert result.metadata.get("failed") is True
    assert result.metadata.get("return_code") == 3
    assert "[执行结束，退出码: 3]" in str(result.content)


@pytest.mark.asyncio
async def test_shell_failed_metadata_on_nonzero_exit_native_command(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("PowerShell EncodedCommand exit passthrough is Windows-specific")
    result = await ShellToolInvocation(
        {"command": "python -c 'import sys; sys.exit(5)'"},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert result.metadata.get("failed") is True
    assert result.metadata.get("return_code") == 5


@pytest.mark.asyncio
async def test_shell_failed_metadata_false_on_success(tmp_path: Path) -> None:
    result = await ShellToolInvocation(
        {"command": "python -c 'print(1)'"},
        workspace_root=tmp_path,
    ).execute()
    assert not result.is_error
    assert result.metadata.get("failed") is False
    assert result.metadata.get("return_code") == 0
    assert "[执行结束，退出码" not in str(result.content)


@pytest.mark.asyncio
async def test_shell_default_cwd_is_workspace_root_not_process_cwd(tmp_path: Path) -> None:
    """Omit working_directory → run under workspace_root even if process cwd moved."""
    workspace = tmp_path / "ws"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    original = Path.cwd()
    try:
        os.chdir(other)
        marker = "shell_cwd_marker.txt"
        cmd = f'Set-Content -Path "{marker}" -Value "ok" -Encoding utf8' if os.name == "nt" else f'echo ok > "{marker}"'
        result = await ShellToolInvocation(
            {"command": cmd},
            workspace_root=workspace,
        ).execute()
        assert not result.is_error
        assert (workspace / marker).is_file()
        assert not (other / marker).exists()
    finally:
        os.chdir(original)
