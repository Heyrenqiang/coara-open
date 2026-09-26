"""Wiring: foreground sessions get record; janitor tools follow parent."""

from __future__ import annotations

from src.coara import janitor_maintenance
from src.coara.builtin_agents import get_subagent
from src.tools.builtin.records.local_search import LocalSearchTool
from src.tools.builtin.records.record import RecordTool


def test_janitor_config_no_whitelist_tools():
    janitor = janitor_maintenance._janitor_config()
    assert janitor is not None
    assert janitor.tools == []


def test_daily_yaml_includes_record():
    daily = get_subagent("daily")
    assert daily is not None
    include = set(daily.tools or [])
    assert "record" in include
    assert "memory" not in include


def test_tool_defaults_match_plan():
    assert LocalSearchTool.should_defer is True
    assert RecordTool.should_defer is False
    assert LocalSearchTool.name == "local_search"
    assert RecordTool.name == "record"


def test_root_yaml_includes_record():
    from pathlib import Path

    from src.prompt.yaml_loader import YamlPromptLoader

    path = Path(__file__).resolve().parents[2] / "src" / "coara" / "prompts" / "agents" / "root.yaml"
    cfg = YamlPromptLoader().load(path)
    assert "record" in (cfg.tools_include or [])
