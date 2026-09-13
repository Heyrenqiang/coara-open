#!/usr/bin/env python3
"""Normalize prompt/tool schema punctuation. Usage: scripts/dev/README.md"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DEV = Path(__file__).resolve().parent
_STEPS = (
    "strip_prompt_terminal_punct.py",
    "strip_tool_schema_punct.py",
)


def main() -> int:
    rc = 0
    for name in _STEPS:
        script = _DEV / name
        print(f"\n=== {name} ===")
        result = subprocess.run([sys.executable, str(script)], check=False)
        rc |= result.returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
