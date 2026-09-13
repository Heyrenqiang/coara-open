"""Check that relative links in Markdown files point to existing files.

Scans all *.md files in the repository (skipping .git / node_modules / build
outputs), extracts [text](target) links, ignores external URLs and pure
anchors, and reports relative targets that do not resolve to an existing
file or directory.

Usage:
    python scripts/dev/check_doc_links.py          # report broken links
    python scripts/dev/check_doc_links.py --quiet  # exit code only
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".qwen",
    ".coara",
    "node_modules",
    "dist",
    "build",
    "out",
    "__pycache__",
    ".venv",
    "venv",
    # 非 Canonical 区域（见 docs/README.md §非 Canonical）与其它工具的产生物
    "third_party",
    "ref-doc",
    "competition",
    "软著申请材料",
    "skills-cursor",
    ".trae",
    ".codeartsdoer",
    ".claude",
    ".atomcode",
    ".arts",
    ".dbg",
    ".lingma",
    ".opencode",
}

SKIP_TARGET_PREFIXES = ("http://", "https://", "mailto:", "file://", "#")

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def iter_markdown_files(root: Path):
    for path in sorted(root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def check_file(md_path: Path) -> list[tuple[int, str]]:
    broken: list[tuple[int, str]] = []
    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return broken
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            target = match.group(1).strip()
            if target.startswith(SKIP_TARGET_PREFIXES):
                continue
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            target = target.split("#", 1)[0]
            if not target or target == "URL":
                continue
            resolved = (md_path.parent / target).resolve()
            if not resolved.exists():
                broken.append((lineno, match.group(1)))
    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only set exit code")
    args = parser.parse_args()

    total_broken = 0
    for md_path in iter_markdown_files(REPO_ROOT):
        broken = check_file(md_path)
        if broken and not args.quiet:
            rel = md_path.relative_to(REPO_ROOT)
            for lineno, target in broken:
                print(f"{rel}:{lineno}: broken link -> {target}")
        total_broken += len(broken)

    if not args.quiet:
        print(f"\n{total_broken} broken link(s)")
    return 1 if total_broken else 0


if __name__ == "__main__":
    sys.exit(main())
