#!/usr/bin/env python3
"""coara 自持 flow 内核 ↔ wdl-engine 漂移校验（CI / 提交前）。

用法：
    python scripts/dev/check_core_fork.py            # 校验，漂移返回 1
    python scripts/dev/check_core_fork.py --quiet    # 只返回退出码

背景：coara 的 src/workflow/core/ 是 wdl-engine `wdl/src/wdl/core/` 的自持副本
（2026-09-10 搬运，让 coara 的内嵌 flow 不依赖 wdl 安装）。图语义静默漂移极难
定位——只会在运行时表现成"工作流偶尔卡住"，故设此哨兵。

判定分两级：
  - **等价性**（硬性）：两边对同一批图的行为必须一致。用已安装的 wdl 当 oracle
    跑 tests/test_workflow/test_core_fork_parity.py。wdl 未安装时跳过（不失败）。
  - **源码漂移**（提示）：逐文件比对源码哈希，不一致仅**提示**，返回 0。
    因为分叉后两边独立演进是**预期行为**，不该当成错误拦截提交。

想让源码级差异也变成硬性失败：加 --strict。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_MINE = _REPO / "src" / "workflow" / "core"
_THEIRS = _REPO / "wdl" / "src" / "wdl" / "core"
_FILES = ("model.py", "semantics.py", "serde.py")
_PARITY_TEST = "tests/test_workflow/test_core_fork_parity.py"


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _source_drift() -> list[tuple[str, str, str]]:
    """返回源码哈希不一致的 (文件名, 自持哈希, wdl 哈希)。"""
    drift = []
    for name in _FILES:
        mine, theirs = _digest(_MINE / name), _digest(_THEIRS / name)
        if mine and theirs and mine != theirs:
            drift.append((name, mine, theirs))
    return drift


def _wdl_installed() -> bool:
    return (
        subprocess.run(
            [sys.executable, "-c", "import wdl.core"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--strict", action="store_true", help="源码差异也视为失败")
    args = parser.parse_args()

    def say(*a: object) -> None:
        if not args.quiet:
            print(*a)

    exit_code = 0

    # 1) 等价性（硬性）—— 用 pytest 跑哨兵测试
    if _wdl_installed():
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", _PARITY_TEST, "-q", "--no-header"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            say("✗ 自持内核与 wdl-engine 行为不一致（等价性哨兵失败）：")
            say(proc.stdout.strip()[-2000:] or proc.stderr.strip()[-2000:])
            exit_code = 1
        else:
            say("✓ 自持内核与 wdl-engine 行为一致（等价性通过）")
    else:
        say("· wdl-engine 未安装 —— 跳过等价性比对（这正是解耦的目标状态）")

    # 2) 源码漂移（默认提示，--strict 才拦截）
    drift = _source_drift()
    if drift:
        msg = "源码与 wdl-engine 存在差异（分叉后属预期，请确认是否需要同步）："
        if args.strict:
            say(f"✗ {msg}")
            exit_code = 1
        else:
            say(f"· {msg}")
        for name, mine, theirs in drift:
            say(f"    {name}: 自持 {mine} · wdl {theirs}")
    else:
        say("✓ 三个内核源文件与 wdl-engine 逐字节一致")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
