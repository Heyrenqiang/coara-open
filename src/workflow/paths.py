"""Canonical filesystem layout for coara workflow draft assets.

WDL 草案（会话产 WDL 的中间态）锚定 config home，与运行工作空间无关。
执行层（引擎/实例库/触发器）已剥离为独立 WDL 软件（仓库内 wdl/），coara
只保留草案生产侧。

Layout:
    <config_home>/users/default/workflows/
        ├── drafts/                      # draft JSON files (kernel projection text)
        ├── session_events.jsonl         # 构建对话 + 会话内节点录像带
        └── flow_session_state.json      # 构建对话恢复索引

解析优先级：显式 coara_home > 运行时配置 config.coara_home > COARA_HOME
bootstrap > cwd/.coara（未配置全局 home 的开发环境，行为与旧布局一致）。
"""

from __future__ import annotations

from pathlib import Path

from src.core.coara_home import resolve_config_home, user_paths


def workflow_home(coara_home: Path | str | None = None) -> Path:
    """Resolve the system-level home for workflow assets."""
    if coara_home is not None:
        return Path(coara_home).expanduser().resolve()
    try:
        from src.core.config import config_manager

        configured = getattr(config_manager.config, "coara_home", None)
        if configured:
            return Path(configured).expanduser().resolve()
    except Exception:  # noqa: BLE001 — 配置未加载（测试/早期启动）走 bootstrap 链
        pass
    return resolve_config_home()


def workflow_assets_root(coara_home: Path | str | None = None) -> Path:
    """Root directory for all workflow assets (system-level)."""
    root = user_paths(workflow_home(coara_home)).workflows_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def workflow_drafts_dir(coara_home: Path | str | None = None) -> Path:
    """Editable workflow drafts (``draft-*.json``)."""
    drafts = workflow_assets_root(coara_home) / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    return drafts


def workflow_assets_layout(coara_home: Path | str | None = None) -> dict[str, str]:
    """Human-readable paths for UI and tool messages."""
    root = workflow_assets_root(coara_home)
    return {
        "root": str(root),
        "drafts": str(workflow_drafts_dir(coara_home)),
    }
