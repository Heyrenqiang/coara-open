"""providers.yaml 解析与节点 LLM 解析顺序（节点级 > 图级 > 配置默认）。"""

from __future__ import annotations

import pytest

from wdl.errors import WDLError
from wdl.executor import LLMNodeExecutor
from wdl.providers import load_providers_config

PROVIDERS_YAML = """\
default: main
providers:
  main:
    type: openai
    base_url: https://example.com/v1
    api_key: ${TEST_WDL_API_KEY}
    models:
      - m-1
      - m-2
  alt:
    type: anthropic
    base_url: https://alt.example.com
    api_key: fixed-key
    models:
      - c-1
"""


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_WDL_API_KEY", "sk-test")
    path = tmp_path / "providers.yaml"
    path.write_text(PROVIDERS_YAML, encoding="utf-8")
    return load_providers_config(path)


def test_load_and_env_ref(config, monkeypatch) -> None:
    assert config.default == "main"
    assert config.providers["main"].resolved_api_key() == "sk-test"
    assert config.providers["alt"].resolved_api_key() == "fixed-key"
    monkeypatch.delenv("TEST_WDL_API_KEY")
    assert config.providers["main"].resolved_api_key() == ""


def test_resolve_priority(config) -> None:
    ex = LLMNodeExecutor(config, graph_llm=("alt", "c-1"), node_llms={"n1": ("main", "m-2")})
    # 节点级优先
    name, cfg, model = ex.resolve_for_node("n1")
    assert (name, model) == ("main", "m-2")
    # 图级次之
    name, cfg, model = ex.resolve_for_node("n2")
    assert (name, model) == ("alt", "c-1")
    # 配置文件默认兜底
    ex2 = LLMNodeExecutor(config)
    name, cfg, model = ex2.resolve_for_node("n3")
    assert (name, model) == ("main", "m-1")  # 未给模型 → provider 首个模型


def test_resolve_errors(config, monkeypatch) -> None:
    with pytest.raises(WDLError, match="未配置"):
        ex = LLMNodeExecutor(config, graph_llm=("ghost", ""))
        ex.resolve_for_node("n")
    monkeypatch.delenv("TEST_WDL_API_KEY")
    with pytest.raises(WDLError, match="api_key"):
        LLMNodeExecutor(config).resolve_for_node("n")
