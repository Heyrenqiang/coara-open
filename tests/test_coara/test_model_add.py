"""/model --add：复用统一添加流程，成功后切模型。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.commands import config as config_cmd
from src.coara.frontend import get_frontend


@pytest.mark.asyncio
async def test_model_add_switches_after_flow(monkeypatch):
    switched = {}

    class _Root:
        foreground_coara = SimpleNamespace(provider_name="", model_name="")

        def switch_llm(self, provider, model=None):
            switched["to"] = (provider, model)
            return provider, model or "default-model"

    monkeypatch.setattr(
        get_frontend(),
        "run_add_model_flow",
        lambda console=None: _async_return("deepseek"),
    )
    result = await config_cmd._handle_model_add(_Root())
    assert not result.data.get("error")
    assert switched["to"] == ("deepseek", None)
    assert "deepseek/default-model" in result.output


@pytest.mark.asyncio
async def test_model_add_cancel(monkeypatch):
    monkeypatch.setattr(
        get_frontend(),
        "run_add_model_flow",
        lambda console=None: _async_return(None),
    )
    result = await config_cmd._handle_model_add(SimpleNamespace(foreground_coara=SimpleNamespace()))
    assert result.data.get("cancelled") is True


@pytest.mark.asyncio
async def test_model_add_switch_failure_still_reports_added(monkeypatch):
    class _Root:
        foreground_coara = SimpleNamespace()

        def switch_llm(self, provider, model=None):
            raise RuntimeError("no model")

    monkeypatch.setattr(
        get_frontend(),
        "run_add_model_flow",
        lambda console=None: _async_return("kimi"),
    )
    result = await config_cmd._handle_model_add(_Root())
    assert not result.data.get("error")
    assert "kimi" in result.output


def _async_return(value):
    async def _coro():
        return value

    return _coro()
