#!/usr/bin/env python3
"""Parse web_search lines from coara log. Usage: scripts/dev/README.md"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


def default_log_path() -> Path:
    coara_home = os.environ.get("COARA_HOME")
    if coara_home:
        return Path(coara_home) / "logs" / "coara.log"
    return Path("coara.log")


def parse_log(log_path: Path, out_path: Path, *, tail_queries: int = 10, tail_results: int = 15) -> None:
    queries: dict[str, str] = {}
    results: list[tuple[str, str]] = []

    text = log_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        m = re.search(r"\[web_search\] query='([^']+)' -> variants=(.+)$", line)
        if m:
            queries[m.group(1)] = m.group(2).strip()

        m = re.search(r"Tool \[web_search\] -> OK.*### 1\. (.+?)\*\*链接\*\*: (\S+)", line)
        if m:
            results.append((m.group(1).strip(), m.group(2).strip()))

    lines = ["=== 最近搜索的 Query 及 Variants ===\n"]
    for q, v in list(queries.items())[-tail_queries:]:
        lines.append(f"Query: {q}\nVariants: {v}\n")

    lines.append("=== 最近返回的 Top 1 链接 ===\n")
    for title, url in results[-tail_results:]:
        lines.append(f"{title}\n  -> {url}\n")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse web_search queries/results from coara log",
        epilog="See scripts/dev/README.md",
    )
    parser.add_argument("--log", type=Path, default=None, help="Log file (default: $COARA_HOME/logs/coara.log)")
    parser.add_argument("--output", type=Path, help="Output file (default: <log_dir>/_parsed.txt)")
    args = parser.parse_args(argv)

    log_path = args.log or default_log_path()
    if not log_path.is_file():
        print(f"log not found: {log_path}", file=sys.stderr)
        return 1

    out_path = args.output or log_path.parent / "_parsed.txt"
    parse_log(log_path, out_path)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
