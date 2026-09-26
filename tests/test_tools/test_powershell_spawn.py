"""Unit tests for PowerShell EncodedCommand spawn helpers."""

from __future__ import annotations

import base64

from src.core.process import powershell_encoded_argv


def test_powershell_encoded_argv_wraps_utf8_and_command() -> None:
    argv = powershell_encoded_argv("Get-Date; Write-Output 'hi'")
    assert argv[0] == "-NoProfile"
    assert argv[1] == "-NonInteractive"
    assert argv[2] == "-EncodedCommand"
    raw = base64.b64decode(argv[3])
    script = raw.decode("utf-16-le")
    assert "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" in script
    assert "$OutputEncoding=[Console]::OutputEncoding;" in script
    assert script.endswith("Get-Date; Write-Output 'hi';exit $LASTEXITCODE")
