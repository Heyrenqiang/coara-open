from __future__ import annotations

from types import SimpleNamespace

from src.core.tool_base import BaseTool, ToolResult
from src.ui.tools_payload import build_tools_payload


class _FakeTool(BaseTool):
    name = "demo"
    description = "演示工具第一行\n第二行忽略"
    category = "filesystem"

    def create_invocation(self, **params):
        raise NotImplementedError

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success("ok")


class _FakeEmail(_FakeTool):
    name = "email"
    summary = "收发邮件"
    category = "communication"


def _empty_registry(monkeypatch) -> None:
    monkeypatch.setattr("src.tools.registry.tool_registry", SimpleNamespace(list_all=lambda: []))


def _root_with(tools: dict[str, BaseTool]):
    manager = SimpleNamespace(tools=tools)
    return SimpleNamespace(foreground_coara=SimpleNamespace(_tool_manager=manager))


def test_no_root_falls_back_to_global_registry(monkeypatch):
    monkeypatch.setattr("src.tools.registry.tool_registry", SimpleNamespace(list_all=lambda: [_FakeTool()]))
    payload = build_tools_payload(None)
    names = {row["name"] for row in payload["tools"]}
    assert {"demo", "web_search", "email", "media"} <= names
    row = next(item for item in payload["tools"] if item["name"] == "demo")
    assert row["name"] == "demo"
    assert row["category"] == "filesystem"
    assert row["description"] == "演示工具第一行"
    assert row["status"] is None
    assert row["config_hint"] is None


def test_email_unconfigured_gives_hint(monkeypatch):
    _empty_registry(monkeypatch)
    from src.tools.builtin.email import email_client

    monkeypatch.setattr(email_client, "_email_config", {})
    payload = build_tools_payload(_root_with({"email": _FakeEmail()}))
    row = payload["tools"][0]
    assert row["name"] == "email"
    assert row["description"] == "收发邮件"
    assert row["status"] == "needs_config"
    assert "COARA_EMAIL" in row["config_hint"]
    assert row["credentials"][0]["id"] == "COARA_EMAIL"
