"""模块注册磁盘化（《空间能力系统》槽位三）与 AgentRegistry 三层扫描的行为测试。"""

from __future__ import annotations

import yaml

from src.coara.module_registry import _spec_from_yaml, module_registry


class TestModuleSpecFromYaml:
    def test_builtin_seven_loaded(self):
        # 出厂七个模块一个不少（chat/review/files/workflow/records/usage/config）
        ids = {m.id for m in module_registry.list()}
        assert {"chat", "review", "files", "workflow", "records", "usage", "config"} <= ids

    def test_builtin_workflow_agentic_intact(self, tmp_path):
        # workflow 模块的 agentic 声明原样搬出：persona/subject 与旧代码字面量一致
        wf = module_registry.get("workflow")
        assert wf is not None
        assert wf.agentic.enabled is True
        assert wf.agentic.persona_agent == "flow-root"
        assert wf.agentic.subject == "flow"
        assert wf.route == "/workflow"

    def test_review_config_records_usage_present(self):
        for mid, subject in (("review", "review"), ("config", "config"), ("records", "records"), ("usage", "usage")):
            spec = module_registry.get(mid)
            assert spec is not None, mid
            assert spec.agentic.enabled is True
            assert spec.agentic.subject == subject

    def test_spec_from_yaml_round_trip(self, tmp_path):
        f = tmp_path / "custom.yaml"
        f.write_text(
            yaml.safe_dump(
                {
                    "title": "自定义",
                    "icon": "X",
                    "route": "/custom",
                    "workspace_relation": "scoped",
                    "order": 42,
                    "agentic": {
                        "enabled": True,
                        "persona_agent": "my-agent",
                        "role": "助手",
                        "expertise_areas": ["a", "b"],
                        "subject": "custom",
                    },
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        spec = _spec_from_yaml(f)
        assert spec is not None
        assert spec.id == "custom"
        assert spec.agentic.expertise_areas == ("a", "b")
        assert spec.workspace_relation == "scoped"

    def test_bad_yaml_returns_none(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("[not: a mapping", encoding="utf-8")
        assert _spec_from_yaml(f) is None

    def test_invalid_relation_returns_none(self, tmp_path):
        f = tmp_path / "badrel.yaml"
        f.write_text(yaml.safe_dump({"workspace_relation": "bogus"}), encoding="utf-8")
        assert _spec_from_yaml(f) is None


class TestAgentRegistryLayeredScan:
    def test_workspace_overrides_builtin(self, tmp_path, monkeypatch):
        from src.prompt.agent_registry import AgentRegistry

        ws_agents = tmp_path / "ws" / ".coara" / "agents"
        ws_agents.mkdir(parents=True)
        (ws_agents / "root.yaml").write_text(
            yaml.safe_dump({"agent": {"name": "root-custom", "system_prompt_template": "空间级覆盖"}}),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path / "ws")
        monkeypatch.setenv("COARA_HOME", str(tmp_path / "home"))

        registry = AgentRegistry.__new__(AgentRegistry)
        registry._agents = {}
        registry.scan()
        agent = registry._agents.get("root")
        assert agent is not None
        assert agent.system_prompt_template == "空间级覆盖"
        AgentRegistry.reset_for_tests()

    def test_no_extra_dirs_keeps_builtin(self, tmp_path, monkeypatch):
        from src.prompt.agent_registry import AgentRegistry

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("COARA_HOME", str(tmp_path / "home"))

        registry = AgentRegistry.__new__(AgentRegistry)
        registry._agents = {}
        registry.scan()
        # 内置 agents 目录照常加载（root 在）
        assert "root" in registry._agents
        AgentRegistry.reset_for_tests()
