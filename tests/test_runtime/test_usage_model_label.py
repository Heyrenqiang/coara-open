"""用量页模型展示名：providers.yaml 正式名优先，历史别名可回退。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.runtime.usage_display import load_model_display_map, resolve_model_label
from src.runtime.usage_query import summarize_usage_dashboard


def _write_providers(home: Path) -> None:
    system = home / "system"
    system.mkdir(parents=True)
    (system / "providers.yaml").write_text(
        """
providers:
  kimi:
    models:
      available:
        - id: k3
          name: Kimi K3
  deepseek:
    models:
      available:
        - id: deepseek-flash
          name: DeepSeek V4.1 Flash
  zhipu:
    models:
      available:
        - id: glm-5.3
          name: 智谱 GLM-5.3
""",
        encoding="utf-8",
    )


def test_resolve_model_label_catalog_and_alias(tmp_path: Path) -> None:
    _write_providers(tmp_path)
    display = load_model_display_map(tmp_path)
    assert resolve_model_label("kimi", "k3", display) == "Kimi K3"
    assert resolve_model_label("kimi", "k3[1m]", display) == "Kimi K3（k3[1m]）"
    assert resolve_model_label("deepseek", "deepseek-flash", display) == "DeepSeek V4.1 Flash"
    assert resolve_model_label("zhipu", "glm-5.3", display) == "智谱 GLM-5.3"
    assert resolve_model_label("custom", "foo", display) == "custom·foo"
    assert resolve_model_label("", "", display) == "（未知模型）"


def test_dashboard_by_model_uses_catalog_names(tmp_path: Path) -> None:
    _write_providers(tmp_path)
    ws = tmp_path / "workspaces" / "w1" / "usage"
    ws.mkdir(parents=True)
    now = datetime.now(tz=UTC)
    lines = [
        {
            "ts": (now - timedelta(hours=1)).isoformat(),
            "kind": "llm_turn",
            "agent_kind": "root",
            "workspace_id": "w1",
            "provider": "kimi",
            "model": "k3",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "ts": (now - timedelta(hours=2)).isoformat(),
            "kind": "llm_turn",
            "agent_kind": "root",
            "workspace_id": "w1",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "usage": {"input_tokens": 20, "output_tokens": 3},
        },
    ]
    import json

    (ws / "events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in lines) + "\n",
        encoding="utf-8",
    )

    dash = summarize_usage_dashboard(days=7, coara_home=tmp_path)
    by_key = {row["model_key"]: row["label"] for row in dash["by_model"]}
    assert by_key["kimi/k3"] == "Kimi K3"
    assert by_key["deepseek/deepseek-flash"] == "DeepSeek V4.1 Flash"
    assert dash["recent"][0]["model_label"] in {"Kimi K3", "DeepSeek V4.1 Flash"}
