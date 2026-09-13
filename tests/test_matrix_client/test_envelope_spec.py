"""信封分类器契约：识别一次、按类型行事。

回归门禁（防「新信封被未知兜底整类吞掉」复发）：2026-09-11 手机端工具行 / diff
整类消失，根因是隐藏层按 body 形状二次猜信封、且不查注册表——COARA_TOOL /
COARA_DIFF 被当未知信封吞掉。此后新加 timeline 类信封必须自动通过下方不变量。
"""

from __future__ import annotations

from src.matrix_client import envelope_spec as spec


def test_classify_known_timeline_and_control() -> None:
    assert spec.classify_envelope('[COARA_TOOL]{"tool_name":"shell"}') == spec.KNOWN
    assert spec.classify_envelope('[COARA_DIFF]\n{"path":"a"}\n[/COARA_DIFF]') == spec.KNOWN
    assert spec.classify_envelope('[COARA_STATUS]\n{"status":"idle"}') == spec.KNOWN


def test_classify_deprecated_unknown_none() -> None:
    assert spec.classify_envelope("[COARA_DIRTREE]\n{}") == spec.DEPRECATED
    assert spec.classify_envelope("[COARA_FUTURE_THING]\n{}") == spec.UNKNOWN
    assert spec.classify_envelope("你好") == spec.NONE
    # 自然语言正文里提到标签不算信封（行首锚定 + 纯 JSON 载荷）
    assert spec.classify_envelope("正文提到 [COARA_TOOL] 不算信封") == spec.NONE


def test_unrecognized_only_covers_truly_unknown() -> None:
    """已知 / 已废弃标签不得算「未识别」——这正是工具行被吞的根因。"""
    assert spec.is_unrecognized_coara_envelope("[COARA_FUTURE_THING]\n{}") is True
    assert spec.is_unrecognized_coara_envelope("[COARA_DIRTREE]\n{}") is False
    assert spec.is_unrecognized_coara_envelope('[COARA_TOOL]{"a":1}') is False
    assert spec.is_unrecognized_coara_envelope('[COARA_DIFF]\n{"a":1}\n[/COARA_DIFF]') is False


def test_is_any_envelope_is_shape_based_catch_all() -> None:
    assert spec.is_any_coara_envelope("[COARA_DIRTREE]\n{}") is True
    assert spec.is_any_coara_envelope('[COARA_TOOL]{"a":1}') is True
    assert spec.is_any_coara_envelope("[COARA_STATUS] 自然语言正文") is False
    assert spec.is_any_coara_envelope("你好") is False


def test_timeline_tags_never_unrecognized() -> None:
    """回归门禁：timeline 类信封是端上要渲染的条目，绝不能被兜底判为未知。"""
    assert spec.TIMELINE_TAGS, "真源必须至少声明一个 timeline 信封"
    for tag in spec.TIMELINE_TAGS:
        body = f'[{tag}]{{"x":1}}'
        assert spec.is_any_coara_envelope(body) is True, tag
        assert spec.is_unrecognized_coara_envelope(body) is False, tag
        assert spec.classify_envelope(body) == spec.KNOWN, tag


def test_every_registered_tag_is_known() -> None:
    """回归门禁：注册表里每个标签都必须判为 KNOWN（不会再掉进未知兜底）。"""
    for tag in spec.ENVELOPE_TAGS:
        assert spec.classify_envelope(f"[{tag}]{{}}") == spec.KNOWN, tag


def test_roles_cover_every_tag() -> None:
    """每个有效标签都必须有端上语义（role），否则端上无从判断该隐藏还是该渲染。"""
    assert set(spec.ENVELOPE_ROLES) == set(spec.ENVELOPE_TAGS)
    assert set(spec.ENVELOPE_ROLES) >= spec.TIMELINE_TAGS
