"""录像带自检：打印每条线的完整性摘要。

用法::

    python scripts/dev/audit_view_tape.py            # 全部空间
    python scripts/dev/audit_view_tape.py --json     # 机器可读

关注的不是「有多少帧」，而是有没有**缺口**：序号断档、回合未闭合、来源端缺失。
只读，不改任何文件。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ui.view_tape_audit import audit_all  # noqa: E402


def _stamp(value: float | None) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(description="审计录像带完整性")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--home", default="", help="coara_home 路径，缺省自动解析")
    args = parser.parse_args()

    reports = audit_all(Path(args.home) if args.home else None)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0

    problems = 0
    for report in reports:
        if report["frames"] == 0:
            continue
        flags = []
        if report["seq_gap_count"]:
            flags.append(f"序号缺口 {report['seq_gap_count']}")
        if report["open_turns"]:
            flags.append(f"未闭合回合 {report['open_turns']}")
        if report["bad_lines"]:
            flags.append(f"坏行 {report['bad_lines']}")
        if flags:
            problems += 1
        mark = "!" if flags else " "
        print(f"{mark} {report['path']}")
        print(
            f"    帧 {report['frames']}  序号 {report['view_seq_min']}..{report['view_seq_max']}"
            f"  跨度 {_stamp(report['first_ts'])} → {_stamp(report['last_ts'])}"
        )
        print(f"    回合 起 {report['turns_started']} / 止 {report['turns_ended']}  来源端 {report['sources']}")
        if flags:
            print(f"    问题 {', '.join(flags)}  缺口样例 {report['seq_gaps']}")

    print(f"共 {len(reports)} 条线，其中 {problems} 条有待查项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
