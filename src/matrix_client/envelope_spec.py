"""coara 控制信封协议常量与分类器。

本文件由 scripts/dev/gen_envelopes.py 从 docs/protocol/coara-envelopes.json 生成，请勿手改。
真源版本：v2（2026-09-11）
"""

from __future__ import annotations

import re

SPEC_VERSION = 2

#: 当前有效的 [COARA_*] 信封标签。
ENVELOPE_TAGS: tuple[str, ...] = (
    "COARA_COLLECT",
    "COARA_COLLECT_ACK",
    "COARA_DIFF",
    "COARA_MODELS",
    "COARA_QUOTE_MXC",
    "COARA_STATUS",
    "COARA_SUBAGENT",
    "COARA_THINKING",
    "COARA_TOOL",
    "COARA_TURN",
    "COARA_UPDATES",
    "COARA_UPDATE_ACK",
    "COARA_UPDATE_CMD",
    "COARA_USAGE",
    "COARA_VAULT",
    "COARA_VAULT_REPLY",
    "COARA_WORKFLOW_DRAFT",
    "COARA_WORKSPACES",
)

#: 端上语义：timeline = 渲染成时间线条目；control = 消费后隐藏；embedded = 内嵌正文。
ENVELOPE_ROLES: dict[str, str] = {
    "COARA_COLLECT": "control",
    "COARA_COLLECT_ACK": "control",
    "COARA_DIFF": "timeline",
    "COARA_MODELS": "control",
    "COARA_QUOTE_MXC": "embedded",
    "COARA_STATUS": "control",
    "COARA_SUBAGENT": "timeline",
    "COARA_THINKING": "control",
    "COARA_TOOL": "timeline",
    "COARA_TURN": "control",
    "COARA_UPDATES": "control",
    "COARA_UPDATE_ACK": "control",
    "COARA_UPDATE_CMD": "control",
    "COARA_USAGE": "control",
    "COARA_VAULT": "control",
    "COARA_VAULT_REPLY": "control",
    "COARA_WORKFLOW_DRAFT": "control",
    "COARA_WORKSPACES": "control",
}

#: timeline 类信封（端上渲染成条目的那一类；识别一次后按类型隐藏豁免）。
TIMELINE_TAGS: frozenset[str] = frozenset({
    "COARA_DIFF",
    "COARA_SUBAGENT",
    "COARA_TOOL",
})

#: 已废弃但仍可能从旧客户端飘来的标签（仅用于诊断；兜底靠 is_any_coara_envelope）。
DEPRECATED_TAGS: tuple[str, ...] = (
    "COARA_DIRTREE",
)

#: 信封分类取值。
KNOWN = "known"
DEPRECATED = "deprecated"
UNKNOWN = "unknown"
NONE = "none"

#: 行首锚定信封头：捕获标签名（成对标签的前后斜杠一并吃掉）。
_ENVELOPE_HEAD_RE = re.compile(r"^\s*\[(/?)(COARA_[A-Z0-9_]+)\]")
#: 剥掉所有 [COARA_*] / [/COARA_*] 标签。
_TAG_STRIP_RE = re.compile(r"\[/?COARA_[A-Z0-9_]+\]")
#: 纯 JSON 载荷（对象或数组，允许前后空白）。
_JSON_PAYLOAD_RE = re.compile(r"(?s)[\{\[].*[\}\]]")


def envelope_tag(body: str) -> str:
    """行首信封标签名；不是信封则空串（成对标签的前后斜杠都归一为标签名）。"""
    match = _ENVELOPE_HEAD_RE.match(body)
    return match.group(2) if match else ""


def is_any_coara_envelope(body: str) -> bool:
    """行首 [COARA_*] 且剥掉标签后只剩 JSON 载荷或空白（含自然语言正文者不算）。"""
    head = _ENVELOPE_HEAD_RE.match(body)
    if head is None:
        return False
    rest = _TAG_STRIP_RE.sub("", body[head.end():]).strip()
    return rest == "" or bool(_JSON_PAYLOAD_RE.fullmatch(rest))


def classify_envelope(body: str) -> str:
    """信封分类（单一判据，两端同源）：known / deprecated / unknown / none。"""
    if not is_any_coara_envelope(body):
        return NONE
    tag = envelope_tag(body)
    if tag in ENVELOPE_TAGS:
        return KNOWN
    if tag in DEPRECATED_TAGS:
        return DEPRECATED
    return UNKNOWN


def is_unrecognized_coara_envelope(body: str) -> bool:
    """True 仅当行首标签**不在注册表**（真·未知）；已知标签不再算未识别。"""
    return classify_envelope(body) == UNKNOWN


def is_timeline_envelope(body: str) -> bool:
    """True 当行首是 timeline 类信封（端上渲染成条目的那一类）。"""
    return envelope_tag(body) in TIMELINE_TAGS
