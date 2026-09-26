"""message_content_to_text: 图片块投影为简短占位，base64 不进 trace/dashboard 文本。"""

from __future__ import annotations

from src.utils.message_content import message_content_to_text

BASE64 = "QUJDREVGRw" * 512  # ~5KB base64


def test_pure_image_block_projects_placeholder_without_base64() -> None:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": BASE64}},
    ]
    text = message_content_to_text(content)
    assert text.startswith("[图片: image/png, ")
    assert text.endswith("KB]")
    assert BASE64 not in text
    assert "base64" not in text


def test_mixed_text_and_image_blocks_project_text_and_placeholder() -> None:
    content = [
        {"type": "text", "text": "看这张图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": BASE64}},
    ]
    text = message_content_to_text(content)
    assert "看这张图" in text
    assert "[图片: image/jpeg, " in text
    assert BASE64 not in text


def test_image_block_without_source_fields_projects_unknown() -> None:
    text = message_content_to_text([{"type": "image"}])
    assert text == "[图片: unknown, 0KB]"


def test_non_text_non_image_blocks_fall_back_to_json() -> None:
    text = message_content_to_text([{"type": "thinking", "thinking": "hmm"}])
    assert "thinking" in text
