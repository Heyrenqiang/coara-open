"""Review board rules — single source of truth for janitor 过目规则.

规则头随 updates_board 下发，与 docs/工作空间动态模型.md 同源。
改动规则时同步更新文档 §2 处置轨迹规则。
"""

from __future__ import annotations

# 处置选项（与 DISPOSITIONS 中 janitor 可选的三个动作对应）
DISPOSITION_OPTIONS = (
    ("dismiss", "勾掉"),
    ("elevate", "呈阅"),
    ("resolve", "已处置"),
)

# note 校验
NOTE_MAX_CHARS = 200
NOTE_REQUIRED_FOR = ("dismiss", "elevate", "resolve")  # janitor 处置必须写理由

# high 显著条目禁止 dismiss（必须呈阅或处置）
HIGH_MUST_ACT = True

SWEEP_LOW_AGE_DAYS = 7


def format_review_rules() -> str:
    """渲染过目单规则头（随 updates_board 下发）。"""
    lines = [
        "## 处理规则",
        f"- 处置三选一：{' / '.join(f'{k} {v}' for k, v in DISPOSITION_OPTIONS)}",
        f"- 每条处置必须写 note，≤{NOTE_MAX_CHARS} 字，写清结论与理由",
    ]
    if HIGH_MUST_ACT:
        lines.append("- high 显著必须 elevate 或 resolve，禁止 dismiss")
    lines += [
        f"- 过保质期 / low 超龄 {SWEEP_LOW_AGE_DAYS} 天 → 自动 dismiss，留轨迹",
        "- 调优先级：`review(action=salience, message_id=…, salience=low|normal|high)`，勿手改 JSON",
        "- 处置轨迹完整保留：by / at / action / note",
    ]
    return "\n".join(lines)
