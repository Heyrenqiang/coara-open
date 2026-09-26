"""Strip trailing punctuation from agent prompts. Usage: scripts/dev/README.md"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

MD_GLOBS = [
    ROOT / "src/coara/prompts",
]
YAML_AGENT_DIR = ROOT / "src/coara/prompts/agents"

TRAILING_PUNCT_RE = re.compile(r"[。.]$")
EXT_SUFFIX_RE = re.compile(r"\.(md|py|yaml|yml|json|txt|docx|xlsx|png|jpg|jpeg|gif|webp)$", re.I)
ELLIPSIS_END_RE = re.compile(r"\.\.\.$")


def should_strip_line(line: str) -> bool:
    stripped = line.rstrip("\n\r")
    trimmed = stripped.rstrip()
    if not trimmed:
        return False
    if trimmed.startswith("```"):
        return False
    if ELLIPSIS_END_RE.search(trimmed):
        return False
    if EXT_SUFFIX_RE.search(trimmed):
        return False
    return bool(TRAILING_PUNCT_RE.search(trimmed))


def strip_line(line: str) -> str:
    if not should_strip_line(line):
        return line
    stripped = line.rstrip("\n\r")
    suffix = line[len(stripped) :]
    trimmed = stripped.rstrip()
    ws = stripped[len(trimmed) :]
    if trimmed.endswith("。"):
        return trimmed[:-1] + ws + suffix
    if trimmed.endswith("."):
        return trimmed[:-1] + ws + suffix
    return line


def transform_text(text: str, *, in_code_block: bool = False) -> tuple[str, bool]:
    changed = False
    out: list[str] = []
    code = in_code_block
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            code = not code
            out.append(line)
            continue
        if code:
            out.append(line)
            continue
        new_line = strip_line(line)
        if new_line != line:
            changed = True
        out.append(new_line)
    return "".join(out), changed


def process_md(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    updated, changed = transform_text(original)
    if changed:
        path.write_text(updated, encoding="utf-8")
    return changed


def process_yaml(path: Path) -> bool:
    original = path.read_text(encoding="utf-8")
    updated, changed = transform_text(original)
    if changed:
        path.write_text(updated, encoding="utf-8")
    return changed


def main() -> int:
    changed_files: list[str] = []

    for base in MD_GLOBS:
        for path in sorted(base.rglob("*.md")):
            if process_md(path):
                changed_files.append(str(path.relative_to(ROOT)))

    for path in sorted(YAML_AGENT_DIR.glob("*.yaml")):
        if process_yaml(path):
            changed_files.append(str(path.relative_to(ROOT)))

    print(f"Updated {len(changed_files)} files")
    for name in changed_files:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
