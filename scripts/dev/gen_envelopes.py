#!/usr/bin/env python3
"""从信封协议真源生成两端常量与分类器。用法：scripts/dev/README.md

真源：docs/protocol/coara-envelopes.json
产物：
  - src/matrix_client/envelope_spec.py            （Python 常量 + 分类器，服务端消费）
  - android-app/app/src/main/java/com/example/agentchat/coara/EnvelopeSpec.kt （Kotlin，Android 消费）

设计动机（2026-09-10）：手机端漏出 [COARA_DIRTREE] 控制帧 —— 服务端删了发端、
Android 端从未认识该标签且没有未知信封兜底。三端信封协议此前各写各的常量，
"加/删信封"要改多处、必漏一处。本脚本让两端从同一份真源派生，把改动收敛为一次。

设计动机（2026-09-11）：此前生成物只有标签集合、没有任何消费方——端上靠「body
形状」二次猜信封类型，于是新加的 timeline 类信封（COARA_TOOL / COARA_DIFF）被
"未知信封兜底"整类吞掉，手机端永远不显示。现在真源带 ``role``，生成物给出
**分类器**（classify / is_any / is_unrecognized / should_swallow），端上识别一次、
按类型行事，不再按形状猜。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
SPEC_PATH = _REPO / "docs" / "protocol" / "coara-envelopes.json"
PY_OUT = _REPO / "src" / "matrix_client" / "envelope_spec.py"
KT_OUT = (
    _REPO
    / "android-app"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "example"
    / "agentchat"
    / "coara"
    / "EnvelopeSpec.kt"
)

_HEADER = "本文件由 scripts/dev/gen_envelopes.py 从 docs/protocol/coara-envelopes.json 生成，请勿手改。"

#: 端上语义取值（真源 role 字段）。
_ROLES = ("timeline", "control", "embedded")


def load_spec() -> dict:
    data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    for key in ("version", "envelopes", "deprecated"):
        if key not in data:
            raise SystemExit(f"真源缺少必需字段：{key}")
    for env in data["envelopes"]:
        role = env.get("role")
        if role not in _ROLES:
            raise SystemExit(f"信封 {env.get('tag')} 的 role 非法：{role!r}（应为 {_ROLES}）")
    return data


def _active_tags(spec: dict) -> list[str]:
    """当前有效信封标签（升序）。"""
    return sorted({e["tag"] for e in spec["envelopes"]})


def _roles(spec: dict) -> dict[str, str]:
    """标签 → 端上语义（升序）。"""
    return {e["tag"]: e["role"] for e in sorted(spec["envelopes"], key=lambda x: x["tag"])}


def _timeline_tags(spec: dict) -> list[str]:
    return sorted({e["tag"] for e in spec["envelopes"] if e["role"] == "timeline"})


def render_python(spec: dict) -> str:
    tags = _active_tags(spec)
    deprecated = sorted({d["tag"] for d in spec["deprecated"]})
    roles = _roles(spec)
    timeline = _timeline_tags(spec)
    lines = [
        '"""coara 控制信封协议常量与分类器。',
        "",
        _HEADER,
        f"真源版本：v{spec['version']}（{spec['updated']}）",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import re",
        "",
        f"SPEC_VERSION = {spec['version']}",
        "",
        "#: 当前有效的 [COARA_*] 信封标签。",
        "ENVELOPE_TAGS: tuple[str, ...] = (",
    ]
    lines += [f'    "{t}",' for t in tags]
    lines += [
        ")",
        "",
        "#: 端上语义：timeline = 渲染成时间线条目；control = 消费后隐藏；embedded = 内嵌正文。",
        "ENVELOPE_ROLES: dict[str, str] = {",
    ]
    lines += [f'    "{t}": "{roles[t]}",' for t in tags]
    lines += [
        "}",
        "",
        "#: timeline 类信封（端上渲染成条目的那一类；识别一次后按类型隐藏豁免）。",
        "TIMELINE_TAGS: frozenset[str] = frozenset({",
    ]
    lines += [f'    "{t}",' for t in timeline]
    lines += [
        "})",
        "",
        "#: 已废弃但仍可能从旧客户端飘来的标签（仅用于诊断；兜底靠 is_any_coara_envelope）。",
        "DEPRECATED_TAGS: tuple[str, ...] = (",
    ]
    lines += [f'    "{t}",' for t in deprecated]
    lines += [
        ")",
        "",
        "#: 信封分类取值。",
        'KNOWN = "known"',
        'DEPRECATED = "deprecated"',
        'UNKNOWN = "unknown"',
        'NONE = "none"',
        "",
        r"#: 行首锚定信封头：捕获标签名（成对标签的前后斜杠一并吃掉）。",
        r'_ENVELOPE_HEAD_RE = re.compile(r"^\s*\[(/?)(COARA_[A-Z0-9_]+)\]")',
        r"#: 剥掉所有 [COARA_*] / [/COARA_*] 标签。",
        r'_TAG_STRIP_RE = re.compile(r"\[/?COARA_[A-Z0-9_]+\]")',
        r"#: 纯 JSON 载荷（对象或数组，允许前后空白）。",
        r'_JSON_PAYLOAD_RE = re.compile(r"(?s)[\{\[].*[\}\]]")',
        "",
        "",
        "def envelope_tag(body: str) -> str:",
        '    """行首信封标签名；不是信封则空串（成对标签的前后斜杠都归一为标签名）。"""',
        "    match = _ENVELOPE_HEAD_RE.match(body)",
        '    return match.group(2) if match else ""',
        "",
        "",
        "def is_any_coara_envelope(body: str) -> bool:",
        '    """行首 [COARA_*] 且剥掉标签后只剩 JSON 载荷或空白（含自然语言正文者不算）。"""',
        "    head = _ENVELOPE_HEAD_RE.match(body)",
        "    if head is None:",
        "        return False",
        '    rest = _TAG_STRIP_RE.sub("", body[head.end():]).strip()',
        '    return rest == "" or bool(_JSON_PAYLOAD_RE.fullmatch(rest))',
        "",
        "",
        "def classify_envelope(body: str) -> str:",
        '    """信封分类（单一判据，两端同源）：known / deprecated / unknown / none。"""',
        "    if not is_any_coara_envelope(body):",
        "        return NONE",
        "    tag = envelope_tag(body)",
        "    if tag in ENVELOPE_TAGS:",
        "        return KNOWN",
        "    if tag in DEPRECATED_TAGS:",
        "        return DEPRECATED",
        "    return UNKNOWN",
        "",
        "",
        "def is_unrecognized_coara_envelope(body: str) -> bool:",
        '    """True 仅当行首标签**不在注册表**（真·未知）；已知标签不再算未识别。"""',
        "    return classify_envelope(body) == UNKNOWN",
        "",
        "",
        "def is_timeline_envelope(body: str) -> bool:",
        '    """True 当行首是 timeline 类信封（端上渲染成条目的那一类）。"""',
        "    return envelope_tag(body) in TIMELINE_TAGS",
        "",
    ]
    return "\n".join(lines)


def render_kotlin(spec: dict) -> str:
    tags = _active_tags(spec)
    deprecated = sorted({d["tag"] for d in spec["deprecated"]})
    roles = _roles(spec)
    timeline = _timeline_tags(spec)
    # Kotlin raw string（"""..."""）不处理反斜杠转义 —— 正则要写 \s / \[ 而非 \\s / \\[，
    # 故此处按原样落下（Python 源码里用 \\s 表示这一对字符）。
    head_re = r"^\s*\[(/?)(COARA_[A-Z0-9_]+)\]"
    tag_re = r"\[/?COARA_[A-Z0-9_]+\]"
    json_re = r"(?s)[\{\[].*[\}\]]"
    lines = [
        "package com.example.agentchat.coara",
        "",
        "/**",
        " * coara 控制信封协议常量与分类器。",
        " *",
        f" * {_HEADER}",
        f" * 真源版本：v{spec['version']}（{spec['updated']}）",
        " */",
        "object EnvelopeSpec {",
        f"    const val SPEC_VERSION = {spec['version']}",
        "",
        "    /** 当前有效的 [COARA_*] 信封标签。 */",
        "    val tags: Set<String> = setOf(",
    ]
    lines += [f'        "{t}",' for t in tags]
    lines += [
        "    )",
        "",
        "    /** 端上语义：timeline = 渲染成时间线条目；control = 消费后隐藏；embedded = 内嵌正文。 */",
        "    val roles: Map<String, String> = mapOf(",
    ]
    lines += [f'        "{t}" to "{roles[t]}",' for t in tags]
    lines += [
        "    )",
        "",
        "    /** timeline 类信封（识别一次后按类型隐藏豁免）。 */",
        "    val timelineTags: Set<String> = setOf(",
    ]
    lines += [f'        "{t}",' for t in timeline]
    lines += [
        "    )",
        "",
        "    /** 已废弃但仍可能从旧客户端飘来的标签（兜底仍吞掉）。 */",
        "    val deprecatedTags: Set<String> = setOf(",
    ]
    lines += [f'        "{t}",' for t in deprecated]
    lines += [
        "    )",
        "",
        "    /** 信封分类取值。 */",
        "    enum class EnvelopeKind { KNOWN, DEPRECATED, UNKNOWN, NONE }",
        "",
        "    /** 行首信封标签名；不是信封则空串（成对标签的前后斜杠都归一为标签名）。 */",
        "    fun envelopeTag(body: String): String {",
        '        val match = ENVELOPE_HEAD_REGEX.find(body) ?: return ""',
        '        if (match.range.first != 0) return ""',
        "        return match.groupValues.getOrNull(2).orEmpty()",
        "    }",
        "",
        "    /**",
        "     * 行首 [COARA_*] 且剥掉标签后只剩 JSON 载荷或空白（含自然语言正文者不算，",
        "     * 例如聊天里引用 [COARA_STATUS]）。这是「看起来像信封」的**唯一**判据。",
        "     */",
        "    fun isAnyEnvelope(body: String): Boolean {",
        "        val trimmed = body.trim()",
        "        val match = ENVELOPE_HEAD_REGEX.find(trimmed) ?: return false",
        "        if (match.range.first != 0) return false",
        '        val rest = TAG_STRIP_REGEX.replace(trimmed.substring(match.range.last + 1), "").trim()',
        "        return rest.isEmpty() || JSON_PAYLOAD_REGEX.matches(rest)",
        "    }",
        "",
        "    /** 信封分类（单一判据，与服务端同源）。 */",
        "    fun classify(body: String): EnvelopeKind {",
        "        if (!isAnyEnvelope(body)) return EnvelopeKind.NONE",
        "        val tag = envelopeTag(body)",
        "        return when {",
        "            tag in tags -> EnvelopeKind.KNOWN",
        "            tag in deprecatedTags -> EnvelopeKind.DEPRECATED",
        "            else -> EnvelopeKind.UNKNOWN",
        "        }",
        "    }",
        "",
        "    /** True 仅当行首标签不在注册表（真·未知）；已知标签不再算未识别。 */",
        "    fun isUnrecognizedEnvelope(body: String): Boolean = classify(body) == EnvelopeKind.UNKNOWN",
        "",
        "    /** True 当行首是 timeline 类信封（端上渲染成条目的那一类）。 */",
        "    fun isTimelineEnvelope(body: String): Boolean = envelopeTag(body) in timelineTags",
        "",
        "    /**",
        "     * 应否静默吞掉这条正文：像信封，且不是 timeline 类。",
        "     * control / embedded / deprecated / unknown 一律吞——绝不落成聊天气泡。",
        "     */",
        "    fun shouldSwallow(body: String): Boolean {",
        "        if (!isAnyEnvelope(body)) return false",
        "        return envelopeTag(body) !in timelineTags",
        "    }",
        "",
        f'    private val ENVELOPE_HEAD_REGEX = Regex("""{head_re}""")',
        "",
        "    /** 剥掉所有 [COARA_*] / [/COARA_*] 标签。 */",
        f'    private val TAG_STRIP_REGEX = Regex("""{tag_re}""")',
        "",
        "    /** 纯 JSON 载荷（对象或数组）：允许前后空白。 */",
        f'    private val JSON_PAYLOAD_REGEX = Regex("""{json_re}""")',
        "}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验产物是否与真源一致（CI 用）")
    args = parser.parse_args()

    spec = load_spec()
    # android-app 是闭源件：开源版仓库没有该目录时跳过 Kotlin 端生成
    # （信封真源仍随开源走，Android 侧常量由闭源仓自己的 gen 生成）
    outputs = {PY_OUT: render_python(spec)}
    if KT_OUT.parents[7].exists() or KT_OUT.parent.exists():  # android-app 树存在
        outputs[KT_OUT] = render_kotlin(spec)
    else:
        print("提示：android-app 不在本仓库，跳过 Kotlin 端信封常量生成", file=sys.stderr)

    drift: list[Path] = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        drift.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    if args.check:
        if drift:
            print("信封常量与真源不一致：", file=sys.stderr)
            for p in drift:
                print(f"  - {p.relative_to(_REPO)}", file=sys.stderr)
            print("请运行：python scripts/dev/gen_envelopes.py", file=sys.stderr)
            return 1
        print("✓ 信封常量与真源一致")
        return 0

    if drift:
        for p in drift:
            print(f"已更新 {p.relative_to(_REPO)}")
    else:
        print("无需更新（已与真源一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
