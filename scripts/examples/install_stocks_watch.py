#!/usr/bin/env python3
"""Install stocks-watch example. Usage: scripts/examples/README.md"""

from __future__ import annotations

import sys

from src.examples.install_stocks_watch import format_install_next_steps, install_stocks_watch


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Install stocks-watch example into coara Home")
    parser.add_argument("--coara-home", help="Target coara Home (default: resolve from env/cwd)")
    parser.add_argument(
        "--workspace",
        help="Workspace path to register as @stocks-watch (default: examples/stocks-watch/workspace)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = install_stocks_watch(
            coara_home=args.coara_home,
            workspace=args.workspace,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    prefix = "would copy" if result.dry_run else "copied"
    print(f"coara_home: {result.coara_home}")
    for path in result.copied:
        print(f"{prefix}: {path}")
    if result.registry_updated:
        action = "would update" if result.dry_run else "updated"
        print(f"{action}: registry @stocks-watch")
    print(f"\n{format_install_next_steps()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
