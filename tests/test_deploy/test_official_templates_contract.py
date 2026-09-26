"""Contract: end-user templates stay release-safe (no retired drivers/URLs)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.errors import ConfigError

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPO / "deploy" / "official" / "templates"
_OFFICIAL_DOCS = _REPO / "deploy" / "official" / "docs"

_EXPECTED = {
    "deepseek": {"driver": "responses", "base_url": "https://api.deepseek.com"},
    "kimi": {"driver": "openai", "base_url": "https://api.kimi.com/coding/v1"},
    "zhipu": {"driver": "responses", "base_url": "https://open.bigmodel.cn/api/v1"},
    "minimax": {"driver": "responses", "base_url": "https://api.minimaxi.com/v1"},
}


def test_official_providers_template_has_current_endpoints() -> None:
    raw = yaml.safe_load((_TEMPLATES / "providers.yaml").read_text(encoding="utf-8"))
    providers = raw["providers"]
    assert raw.get("default_provider") in ("", None)
    for name, expect in _EXPECTED.items():
        block = providers[name]
        assert block["driver"] == expect["driver"], name
        assert block["base_url"].rstrip("/") == expect["base_url"], name
        assert "anthropic" not in str(block.get("driver", "")).lower()
        assert "/anthropic" not in str(block.get("base_url", "")).lower()
        for entry in block["models"]["available"]:
            assert int(entry.get("context_window") or 0) > int(entry.get("max_tokens") or 0), entry.get("id")


def test_official_docs_point_to_current_endpoints() -> None:
    text = (_OFFICIAL_DOCS / "接入模型服务商.md").read_text(encoding="utf-8")
    assert "api.kimi.com/coding/v1" in text
    assert "api.minimaxi.com/v1" in text
    assert "driver: openai" in text
    assert "driver: responses" in text


def test_infer_driver_rejects_anthropic() -> None:
    from src.llm.drivers import infer_driver

    with pytest.raises(ConfigError, match="Unknown LLM driver"):
        infer_driver("kimi", "https://api.kimi.com/coding/v1", "anthropic")
