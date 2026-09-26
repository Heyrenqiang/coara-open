"""Strip trailing punctuation from tool schema lines. Usage: scripts/dev/README.md"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = ROOT / "src/tools/builtin"

SCHEMA_LINE = re.compile(r'^(\s*"description"\s*:\s*")(.*?)(")(\s*,?\s*)$')
PLAIN_DESC = re.compile(r'^(\s*description\s*=\s*")(.*?)(")(\s*)$')


def strip_body(body: str) -> str:
    if body.endswith("..."):
        return body
    if re.search(r"\.(md|py|yaml|yml|json|txt|docx|xlsx)$", body, re.I):
        return body
    if body.endswith("。"):
        return body[:-1]
    if body.endswith("."):
        return body[:-1]
    return body


def patch_line(line: str) -> str:
    newline = "\n" if line.endswith("\n") else ""
    core = line[:-1] if newline else line
    for pattern in (SCHEMA_LINE, PLAIN_DESC):
        match = pattern.match(core)
        if not match:
            continue
        prefix, body, quote, tail = match.groups()
        new_body = strip_body(body)
        if new_body != body:
            return f"{prefix}{new_body}{quote}{tail}{newline}"
    return line


def patch_file(path: Path) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for index, line in enumerate(lines):
        new_line = patch_line(line)
        if new_line != line:
            lines[index] = new_line
            changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def main() -> int:
    changed: list[str] = []
    for path in sorted(TOOL_DIR.rglob("*.py")):
        if patch_file(path):
            changed.append(str(path.relative_to(ROOT)))
    print(f"Updated {len(changed)} files")
    for name in changed:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
