"""空间级能力声明（space.yaml 单真源的 tools/skills 白名单）行为测试。

默认口径：空间不声明（无 space.yaml 或字段缺省）时，工具面与技能清单逐字节不变。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.coara.workspace_capabilities import (
    CORE_TOOL_NAMES,
    resolve_skill_allowed,
    resolve_tool_whitelist,
)
from src.skills.manager import SkillManager
from src.workspace.identity import load_space_identity, space_home_view, write_space_identity


def _write_space_yaml(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "space.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class TestSpaceIdentityFields:
    def test_no_space_yaml_returns_none(self, tmp_path):
        assert load_space_identity(tmp_path) is None

    def test_capability_fields_round_trip(self, tmp_path):
        _write_space_yaml(
            tmp_path,
            {"tools": ["read", "grep"], "skills": ["event-source"], "home_view": "plugin:bundle.js"},
        )
        identity = load_space_identity(tmp_path)
        assert identity is not None
        assert identity.tools == ["read", "grep"]
        assert identity.skills == ["event-source"]
        assert identity.home_view == "plugin:bundle.js"

    def test_legacy_three_fields_still_parse(self, tmp_path):
        # 存量 space.yaml 只有 type/storefront/view：新字段缺省 None
        _write_space_yaml(tmp_path, {"type": "code", "storefront": "warehouse", "view": "all"})
        identity = load_space_identity(tmp_path)
        assert identity is not None
        assert identity.type == "code"
        assert identity.tools is None
        assert identity.skills is None

    def test_bad_yaml_returns_none(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "space.yaml").write_text("[not: a mapping", encoding="utf-8")
        assert load_space_identity(tmp_path) is None


class TestWriteSpaceIdentity:
    def test_writes_declared_keys_only(self, tmp_path):
        write_space_identity(tmp_path, space_type="usage", storefront="display", home_view="/usage")
        identity = load_space_identity(tmp_path)
        assert identity is not None
        assert identity.type == "usage"
        assert identity.storefront == "display"
        assert identity.home_view == "/usage"
        assert identity.tools is None

    def test_overwrite_replaces_previous(self, tmp_path):
        write_space_identity(tmp_path, home_view="/a")
        write_space_identity(tmp_path, home_view="/b", tools=["read"])
        assert space_home_view(tmp_path) == "/b"
        identity = load_space_identity(tmp_path)
        assert identity is not None and identity.home_view == "/b"


class TestSpaceHomeView:
    def test_undeclared_returns_empty(self, tmp_path):
        assert space_home_view(tmp_path) == ""

    def test_plugin_entry_passes_through(self, tmp_path):
        _write_space_yaml(tmp_path, {"home_view": "plugin:canvas.js"})
        assert space_home_view(tmp_path) == "plugin:canvas.js"


class TestResolveHelpers:
    def test_undeclared_returns_none(self, tmp_path):
        assert resolve_tool_whitelist(tmp_path) is None
        assert resolve_skill_allowed(tmp_path) is None

    def test_tool_whitelist_unions_core_set(self, tmp_path):
        _write_space_yaml(tmp_path, {"tools": ["read", "grep"]})
        whitelist = resolve_tool_whitelist(tmp_path)
        assert whitelist is not None
        assert {"read", "grep"} <= whitelist
        assert whitelist >= CORE_TOOL_NAMES  # 核心集恒在，白名单管不到

    def test_skill_allowed_exact(self, tmp_path):
        _write_space_yaml(tmp_path, {"skills": ["event-source"]})
        assert resolve_skill_allowed(tmp_path) == {"event-source"}

    def test_skill_empty_list_means_none_allowed(self, tmp_path):
        _write_space_yaml(tmp_path, {"skills": []})
        assert resolve_skill_allowed(tmp_path) == set()

    def test_per_workspace_isolation(self, tmp_path):
        # 每个空间的声明只由自己的 space.yaml 决定，跨空间零泄漏
        a = tmp_path / "a"
        b = tmp_path / "b"
        _write_space_yaml(a, {"tools": ["read"]})
        b.mkdir(parents=True)
        assert resolve_tool_whitelist(a) is not None
        assert resolve_tool_whitelist(b) is None


class _FakeSkill:
    def __init__(self, name: str) -> None:
        self.name = name
        self.listed = True


class TestApplyDeferredWithAllowed:
    def _manager(self, names: list[str]) -> SkillManager:
        manager = SkillManager.__new__(SkillManager)
        manager._skills = {n: _FakeSkill(n) for n in names}
        return manager

    def test_allowed_none_keeps_legacy_semantics(self):
        manager = self._manager(["a", "b", "c"])
        manager.apply_deferred(["b"])
        assert manager._skills["a"].listed is True
        assert manager._skills["b"].listed is False
        assert manager._skills["c"].listed is True

    def test_allowed_whitelist_narrows_listing(self):
        manager = self._manager(["a", "b", "c"])
        manager.apply_deferred([], allowed={"a"})
        assert manager._skills["a"].listed is True
        assert manager._skills["b"].listed is False  # 白名单外视同挂起
        assert manager._skills["c"].listed is False

    def test_allowed_combines_with_deferred(self):
        manager = self._manager(["a", "b", "c"])
        manager.apply_deferred(["a"], allowed={"a", "b"})
        assert manager._skills["a"].listed is False  # 全局挂起命中
        assert manager._skills["b"].listed is True
        assert manager._skills["c"].listed is False  # 白名单外

    def test_empty_allowed_hides_all(self):
        manager = self._manager(["a", "b"])
        manager.apply_deferred([], allowed=set())
        assert all(not s.listed for s in manager._skills.values())


@pytest.mark.asyncio
async def test_whitelist_feeds_visible_tools(tmp_path):
    """set_whitelist 后可见工具收窄且核心集保留——会话装配链的核心断言。"""
    from src.coara.tool_manager import ToolManager

    manager = ToolManager()
    for name in ["read", "grep", "shell", "todo", "delegate"]:
        tool = SimpleNamespace(
            name=name,
            owner_only=False,
            definition={"name": name, "description": "", "parameters": {"type": "object"}},
        )
        manager._tools[name] = tool

    # 未声明：全量可见（现状不变）
    assert set(manager.get_visible_tool_names(True)) == {"read", "grep", "shell", "todo", "delegate"}

    # 声明 read+grep：核心集自动保留，shell 被裁
    manager.set_whitelist({"read", "grep"} | set(CORE_TOOL_NAMES))
    visible = set(manager.get_visible_tool_names(True))
    assert "shell" not in visible
    assert {"read", "grep", "todo", "delegate"} <= visible
