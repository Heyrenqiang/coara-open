"""Agnes base URL resolution (media providers)."""

from __future__ import annotations

from src.tools.builtin.media import providers


def test_resolve_agnes_base_priority(monkeypatch, tmp_path) -> None:
    """Env > providers.yaml > default CN hub; /v1 normalized; poll origin strips /v1."""
    monkeypatch.delenv("COARA_HOME", raising=False)
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    assert providers.resolve_agnes_base() == "https://api.agnes-ai.cn/v1"
    assert providers.resolve_agnes_poll_base() == "https://api.agnes-ai.cn"

    monkeypatch.setenv("AGNES_BASE_URL", "https://api.agnes-ai.cn")
    assert providers.resolve_agnes_base() == "https://api.agnes-ai.cn/v1"

    monkeypatch.setenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
    assert providers.resolve_agnes_base() == "https://apihub.agnes-ai.com/v1"
    assert providers.resolve_agnes_poll_base() == "https://apihub.agnes-ai.com"

    home = tmp_path / "home"
    (home / "system").mkdir(parents=True)
    (home / "system" / "providers.yaml").write_text(
        "providers:\n  agnes:\n    base_url: https://apihub.agnes-ai.com/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(providers, "_coara_home", lambda: home)
    monkeypatch.delenv("AGNES_BASE_URL", raising=False)
    assert providers.resolve_agnes_base() == "https://apihub.agnes-ai.com/v1"
    assert providers.resolve_agnes_poll_base() == "https://apihub.agnes-ai.com"
