"""Tests for YamlPromptLoader modular prompt assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.prompt.yaml_loader import YamlPromptLoader


class TestYamlPromptLoader:
    @pytest.fixture
    def loader(self):
        return YamlPromptLoader()

    @pytest.fixture
    def prompts_dir(self):
        return Path(__file__).parent.parent.parent / "src" / "coara" / "prompts"

    def test_load_advisor_yaml(self, loader, prompts_dir):
        config = loader.load(prompts_dir / "agents" / "aide.yaml")
        assert config.name == "aide"
        # aide 是资料调研与头脑aide：检索工具齐备，不带写工具与编排工具
        assert {"web_search", "web_fetch"} <= set(config.tools_include)
        assert "workflow" not in config.tools_include

    def test_yaml_tools_include(self, loader, prompts_dir):
        config = loader.load(prompts_dir / "agents" / "aide.yaml")
        assert {"read", "grep", "glob"} <= set(config.tools_include)

    def test_yaml_tools_exclude(self, loader, prompts_dir):
        config = loader.load(prompts_dir / "agents" / "aide.yaml")
        assert {"write", "edit", "delete", "shell"} <= set(config.tools_exclude)

    def test_variable_substitution(self, loader, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(
            """
version: 1
agent:
  name: "test"
  extends: []
  system_prompt_args:
    TEST_VAR: "hello_world"
  system_prompt_template: |
    Value is ${TEST_VAR}
  tools:
    include: []
""".strip(),
            encoding="utf-8",
        )
        config = loader.load(yaml_path)
        assert "Value is hello_world" in config.system_prompt
        assert "${TEST_VAR}" not in config.system_prompt

    def test_unknown_variable_preserved(self, loader, tmp_path):
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(
            """
version: 1
agent:
  name: "test"
  extends: []
  system_prompt_template: |
    Unknown ${UNKNOWN_VAR} here
  tools:
    include: []
""".strip(),
            encoding="utf-8",
        )
        config = loader.load(yaml_path)
        assert "${UNKNOWN_VAR}" in config.system_prompt

    def test_include_directive(self, loader, tmp_path):
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        (module_dir / "header.md").write_text("# Header\n", encoding="utf-8")
        yaml_path = tmp_path / "test.yaml"
        yaml_path.write_text(
            """
version: 1
agent:
  name: "test"
  extends: []
  system_prompt_template: |
    {{INCLUDE:modules/header.md}}
    Body text
  tools:
    include: []
""".strip(),
            encoding="utf-8",
        )
        # 根约束：include 只允许落在 prompts 根内（防 ../../.env 逃逸），
        # 测试把 tmp_path 当作 prompts 根
        config = YamlPromptLoader(prompts_dir=tmp_path).load(yaml_path)
        assert "# Header" in config.system_prompt
        assert "Body text" in config.system_prompt

    def test_missing_yaml_raises(self, loader):
        with pytest.raises(FileNotFoundError):
            loader.load("/nonexistent/agent.yaml")

    def test_invalid_yaml_raises(self, loader, tmp_path):
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("version: 1\nother_key: {}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'agent' key"):
            loader.load(yaml_path)
