"""无可用模型时 /model 跳转配置页「模型」。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.commands import config as config_cmd
from src.coara.commands.registry import parse_command


@pytest.mark.asyncio
async def test_model_empty_catalog_navigates_to_models_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_cmd,
        "resolve_target_coara",
        lambda root, _args: SimpleNamespace(provider_name="minimax", model_name="MiniMax-M3"),
    )
    monkeypatch.setattr(
        "src.llm.model_catalog._enabled_provider_names",
        lambda _cm: [],
    )
    monkeypatch.setattr(
        "src.llm.model_catalog.list_model_choices",
        lambda _cm: [],
    )

    root = SimpleNamespace(_web_server=SimpleNamespace(build_url=lambda p: f"http://127.0.0.1:8080{p}"))
    args = parse_command("/model")
    assert args is not None
    result = await config_cmd.handle_model(root, args)
    assert result.data.get("navigate") == "/config?focus=models"
    assert result.data.get("open_url") == "http://127.0.0.1:8080/config?focus=models"
    # 静默跳转：零用户可见文案
    assert result.output == ""


@pytest.mark.asyncio
async def test_model_add_flag_redirects_to_models_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_cmd,
        "resolve_target_coara",
        lambda root, _args: SimpleNamespace(provider_name="minimax", model_name="MiniMax-M3"),
    )
    root = SimpleNamespace(_web_server=SimpleNamespace(build_url=lambda p: f"http://x{p}"))
    args = parse_command("/model --add")
    assert args is not None
    result = await config_cmd.handle_model(root, args)
    assert result.data.get("navigate") == "/config?focus=models"
    # 静默跳转：零用户可见文案
    assert result.output == ""
