"""日志噪音折叠 —— 工具结果注入 LLM 前的进度墙/刷屏行压缩。

折叠只动注入侧文本：后台任务的 output.log、前台命令的原始输出均不受影响。
"""

from __future__ import annotations

import re

# pytest/pip 风格进度行：……… [ 42%]（点+百分比方括号，允许空白）
_PROGRESS_LINE_RE = re.compile(r"^\s*[.\s]*\[\s*\d{1,3}%\]\s*$")


def collapse_log_noise(text: str) -> str:
    """折叠日志噪音行：进度点行整段折成一行、连续重复行折成 ×N。

    结果要注入会话（按轮计费），进度墙（成百上千行 …… [ n%]）
    与刷屏重复行是纯烧词元的噪音。折叠只动注入侧，output.log 原样保留。
    """
    out: list[str] = []
    progress_count = 0
    prev_line: str | None = None
    prev_count = 0

    def flush_progress() -> None:
        nonlocal progress_count
        if progress_count:
            out.append(f"（进度行 ×{progress_count} 已折叠）")
            progress_count = 0

    def flush_dup() -> None:
        nonlocal prev_count
        if prev_count > 1:
            out.append(f"（上行 ×{prev_count}）")
        prev_count = 0

    for line in text.splitlines():
        if _PROGRESS_LINE_RE.match(line):
            flush_dup()
            progress_count += 1
            continue
        flush_progress()
        if line and line == prev_line:
            prev_count += 1
            continue
        flush_dup()
        out.append(line)
        prev_line = line
        prev_count = 1
    flush_progress()
    flush_dup()
    return "\n".join(out)
