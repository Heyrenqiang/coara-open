"""providers_changed 后各会话 chrome 同步（provider_has_key 权威刷新）。

回归场景：web 配置页填 key 保存 → 热重载换注册表实例 → broadcast_providers_changed。
此前会话持有的旧 provider 引用不换（消息打到无 key 旧连接）、attach 状态栏
provider_has_key 只随 llm_switched 更新而无人发，CLI 恒显「/model 去配置」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.coara.root import RootCoara
from src.core.events import TraceEvent
from src.llm.registry import provider_registry
from src.workspace.manager import WorkspaceManager
from tests.helpers import FakeProvider


async def _make_root(tmp_path: Path, monkeypatch, provider_name: str, provider: FakeProvider):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    workspace_a = tmp_path / "a"
    workspace_a.mkdir()
    provider_registry.register(provider_name, provider)
    root = RootCoara(workspace_dir=workspace_a, provider_name=provider_name)
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=tmp_path / "home")
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    return root


def _collect_llm_switched(root: RootCoara) -> list[TraceEvent]:
    events: list[TraceEvent] = []

    def _collect(event: TraceEvent) -> None:
        events.append(event)

    root.event_bus.subscribe(_collect, topic="llm_switched")
    return events


@pytest.mark.asyncio
async def test_providers_changed_rebinds_session_and_emits_chrome(tmp_path: Path, monkeypatch) -> None:
    root = await _make_root(tmp_path, monkeypatch, "fake-pc", FakeProvider([]))
    root._subscribe_providers_changed()
    coara = root.foreground_coara
    old_provider = coara.provider

    events = _collect_llm_switched(root)
    new_provider = FakeProvider([])
    provider_registry.register("fake-pc", new_provider)

    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    assert coara.provider is new_provider
    assert coara.provider is not old_provider
    chrome = [e for e in events if (e.payload or {}).get("provider") == "fake-pc"]
    assert chrome, "providers_changed 后应补发 llm_switched chrome"
    payload = chrome[-1].payload or {}
    assert payload.get("provider_has_key") is True
    assert payload.get("model") == coara.model_name
    # origin 空：不触发 CLI「他端切换模型」提示
    assert payload.get("origin_source") == ""


@pytest.mark.asyncio
async def test_providers_changed_reports_missing_key(tmp_path: Path, monkeypatch) -> None:
    root = await _make_root(tmp_path, monkeypatch, "fake-pc2", FakeProvider([]))
    root._subscribe_providers_changed()
    events = _collect_llm_switched(root)

    # 注册表换成无 key 实例（删除 key 保存的场景）
    class _NoKeyProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.api_key = ""

    provider_registry.register("fake-pc2", _NoKeyProvider())
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    chrome = [e for e in events if (e.payload or {}).get("provider") == "fake-pc2"]
    assert chrome and chrome[-1].payload.get("provider_has_key") is False


@pytest.mark.asyncio
async def test_providers_changed_does_not_steal_pending_switch(tmp_path: Path, monkeypatch) -> None:
    """回合中的延迟切换由回合结束统一应用；providers_changed 只发 chrome 不抢换。"""
    root = await _make_root(tmp_path, monkeypatch, "fake-pc3", FakeProvider([]))
    root._subscribe_providers_changed()
    coara = root.foreground_coara
    old_provider = coara.provider
    coara._pending_llm_switch = ("fake-pc3", "other-model")

    events = _collect_llm_switched(root)
    provider_registry.register("fake-pc3", FakeProvider([]))
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    assert coara.provider is old_provider
    assert any((e.payload or {}).get("provider") == "fake-pc3" for e in events)


@pytest.mark.asyncio
async def test_providers_changed_defers_rebind_during_active_turn(tmp_path: Path, monkeypatch) -> None:
    """在飞回合不抢换 provider 实例：登记 _pending_llm_switch，回合结束统一应用。"""
    root = await _make_root(tmp_path, monkeypatch, "fake-pc4", FakeProvider([]))
    root._subscribe_providers_changed()
    coara = root.foreground_coara
    old_provider = coara.provider
    coara._inside_turn = True  # 模拟回合在飞

    new_provider = FakeProvider([])
    provider_registry.register("fake-pc4", new_provider)
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    assert coara.provider is old_provider, "在飞回合不得抢换 provider 实例"
    assert coara._pending_llm_switch is not None
    assert coara._pending_llm_switch[0] == "fake-pc4"

    # 回合结束（无 pending 抢占时）再发一次 providers_changed：正常换实例
    coara._inside_turn = False
    coara._pending_llm_switch = None
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)
    assert coara.provider is new_provider


@pytest.mark.asyncio
async def test_providers_changed_switches_keyless_session_to_default(
    tmp_path: Path, monkeypatch
) -> None:
    """发布版：会话停在无 key 的 deepseek，填 minimax 后应切到默认并刷 chrome。"""
    class _NoKey(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.name = "deepseek"
            self.api_key = ""
            self.default_model = "deepseek-flash"

    class _MiniMax(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.name = "minimax"
            self.api_key = "sk-real-minimax"
            self.default_model = "MiniMax-M2.7"

    deepseek = _NoKey()
    root = await _make_root(tmp_path, monkeypatch, "deepseek", deepseek)
    root._subscribe_providers_changed()
    coara = root.foreground_coara
    assert coara.provider_name == "deepseek"

    minimax = _MiniMax()
    provider_registry.register("minimax", minimax)
    # 注册表里 deepseek 仍无 key（模拟只填了 minimax）
    provider_registry.register("deepseek", deepseek)

    from src.core import config as config_mod

    class _Cfg:
        default_provider = "minimax"
        default_model = "MiniMax-M2.7"

    monkeypatch.setattr(config_mod.config_manager, "_config", _Cfg(), raising=False)

    events = _collect_llm_switched(root)
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    assert coara.provider_name == "minimax"
    assert coara.model_name == "MiniMax-M2.7"
    chrome = [e for e in events if (e.payload or {}).get("provider") == "minimax"]
    assert chrome, "切到默认后应发 llm_switched chrome"
    assert chrome[-1].payload.get("provider_has_key") is True
    # last-run 必须改写，否则重启还会装回 deepseek
    entry = root.workspace_manager.active_entry
    assert entry is not None
    assert (entry.provider or "") == "minimax"
    assert (entry.model or "") == "MiniMax-M2.7"


@pytest.mark.asyncio
async def test_providers_changed_mid_turn_pends_default_not_old_keyless(
    tmp_path: Path, monkeypatch
) -> None:
    """回合中填 key：pending 必须挂新默认，不能挂回无 key 的旧名。"""

    class _NoKey(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.name = "deepseek"
            self.api_key = ""
            self.default_model = "deepseek-flash"

    class _MiniMax(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.name = "minimax"
            self.api_key = "sk-real-minimax"
            self.default_model = "MiniMax-M2.7"

    deepseek = _NoKey()
    root = await _make_root(tmp_path, monkeypatch, "deepseek", deepseek)
    root._subscribe_providers_changed()
    coara = root.foreground_coara
    coara._inside_turn = True
    provider_registry.register("minimax", _MiniMax())
    provider_registry.register("deepseek", deepseek)

    from src.core import config as config_mod

    class _Cfg:
        default_provider = "minimax"
        default_model = "MiniMax-M2.7"

    monkeypatch.setattr(config_mod.config_manager, "_config", _Cfg(), raising=False)

    events = _collect_llm_switched(root)
    root.event_bus.publish(
        TraceEvent(coara_id="system", coara_name="config", event_type="providers_changed", message="changed")
    )
    await asyncio.sleep(0)

    assert coara._pending_llm_switch == ("minimax", "MiniMax-M2.7")
    # 实例字段不得提前改：会话按 provider_name 解析 provider，回合中改了等于立刻换连接
    assert coara.provider_name == "deepseek"
    assert coara.model_name == "deepseek-flash"
    # 实例尚未抢换（回合中）
    assert coara.provider is deepseek
    # chrome 先行：llm_switched 事件用局部变量展示新模型（显示同步与本回合连接解耦）
    chrome = [e for e in events if (e.payload or {}).get("provider") == "minimax"]
    assert chrome, "回合中切默认也应补发 llm_switched chrome"
    assert chrome[-1].payload.get("model") == "MiniMax-M2.7"
    assert chrome[-1].payload.get("provider_has_key") is True
