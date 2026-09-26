"""Tests for AgentRegistry loading prompt definitions from Markdown files."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.prompt.agent_registry import AgentDefinition, AgentRegistry


class TestAgentRegistry:
    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Reset the singleton before each test."""
        registry = AgentRegistry()
        registry._agents.clear()
        registry._instance = None
        yield
        registry._agents.clear()

    def test_scan_default_directories(self):
        registry = AgentRegistry()
        registry.scan()

        assert len(registry._agents) > 0
        root_agent = registry.get_agent("root")
        assert root_agent is not None
        assert root_agent.id == "root"

    def test_scan_nonexistent_directory(self):
        registry = AgentRegistry()
        registry.scan([Path("/nonexistent/path")])
        assert len(registry._agents) == 0

    def test_reload(self):
        registry = AgentRegistry()
        registry.scan()
        registry._agents["test_dummy"] = AgentDefinition(Path("test.md"), "Test content")
        assert "test_dummy" in registry._agents

        registry.reload()
        assert "test_dummy" not in registry._agents

    def test_agent_content_validity(self):
        registry = AgentRegistry()
        registry.scan()

        for agent_id, agent in registry._agents.items():
            assert agent.system_prompt_template, f"Agent {agent_id} has empty system prompt"
            assert len(agent.system_prompt_template) > 10, f"Agent {agent_id} content too short"
