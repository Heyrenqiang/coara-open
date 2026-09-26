"""当前配置概况 — 配置助手对话的尾部注入文本（draft_overview 的配置版）"""

from __future__ import annotations

from src.core.logger import logger


def build_config_overview() -> str | None:
    """渲染当前配置概况；配置未加载返回 None（调用方跳过注入）。"""
    from src.core.config import config_manager

    cfg = config_manager._config
    if cfg is None:
        return None

    lines: list[str] = []

    # 常规
    general_parts = []
    if cfg.log_level:
        general_parts.append(f"日志级别={cfg.log_level}")
    if cfg.default_provider:
        model_part = f"·{cfg.default_model}" if cfg.default_model else ""
        general_parts.append(f"默认模型={cfg.default_provider}{model_part}")
    if cfg.coara_home:
        general_parts.append(f"coara_home={cfg.coara_home}")
    general_parts.append(f"技能={'开' if cfg.skills_enabled else '关'}")
    from src.ext import config_rows

    general_parts.extend(config_rows())
    records = cfg.records
    general_parts.append(f"记录={'开' if getattr(records, 'enabled', True) else '关'}")
    # 用户规则与开机自启：只注状态不注全文（规则全文防概况膨胀，改时配置助手自行 read）
    try:
        from src.core.coara_home import system_dir_for_home

        home = cfg.coara_home
        if home is not None:
            rules_path = system_dir_for_home(home) / "user_rules.md"
            if rules_path.is_file():
                n = len(rules_path.read_text(encoding="utf-8").strip())
                general_parts.append(f"用户规则=已设置 {n} 字" if n else "用户规则=空")
            else:
                general_parts.append("用户规则=未设置")
    except Exception:
        logger.debug("config overview: user-rules status failed; omitted", exc_info=True)
    try:
        from src.cli.autostart import is_enabled as _autostart_enabled

        general_parts.append(f"开机自启={'开' if _autostart_enabled() else '关'}")
    except Exception:
        logger.debug("config overview: autostart status failed; omitted", exc_info=True)
    lines.append("常规：" + "；".join(general_parts))

    # 手机连接与隧道
    matrix = cfg.matrix
    tunnel = getattr(matrix, "tunnel_enabled", None)
    tunnel_str = "默认" if tunnel is None else ("开" if tunnel else "关")
    lines.append(
        f"手机连接：托管={'开' if getattr(matrix, 'host_enabled', True) else '关'}；"
        f"端口={getattr(matrix, 'port', 8008)}；隧道={tunnel_str}（{getattr(matrix, 'tunnel_mode', '') or 'quick'}）"
    )

    # 模型提供者
    providers = cfg.providers
    if providers:
        lines.append(f"模型提供者（{len(providers)} 个，顺序即展示顺序）：")
        for name, p in providers.items():
            status = "" if p.enabled else "（已停用）"
            key_hint = "有 key" if (p.api_key or p.api_key_env) else "无 key"
            models = p.models.get("available", [])
            model_ids = []
            if isinstance(models, list):
                for m in models:
                    if isinstance(m, dict):
                        model_ids.append(m.get("id", ""))
                    elif isinstance(m, str):
                        model_ids.append(m)
            default_model = p.default_model or p.models.get("default", "")
            model_str = f"，默认={default_model}" if default_model else ""
            lines.append(
                f"- {name}{status}：{p.driver or 'openai'}，{p.base_url or '无 URL'}，{key_hint}{model_str}"
                + (f"，可选模型={', '.join(model_ids)}" if model_ids else "")
            )
    else:
        lines.append("模型提供者：未配置")

    # 工作空间
    try:
        from src.core.config import config_manager as cm
        from src.workspace.registry import WorkspaceRegistry

        registry = WorkspaceRegistry(cm.coara_home)
        doc = registry.load()
        entries = doc.workspaces
        if entries:
            ws_parts = []
            for ws_id, entry in entries.items():
                mark = "（默认）" if ws_id == doc.default_workspace else ""
                name = entry.name or entry.path
                ws_parts.append(f"{name}{mark}")
            lines.append(f"工作空间（{len(entries)}）：" + "、".join(ws_parts))
    except Exception:
        logger.debug("config overview: workspace summary section failed; omitted", exc_info=True)

    # 事件源
    try:
        from src.core.event_source import list_event_sources

        sources = list_event_sources()
        if sources:
            es_parts = []
            for s in sources:
                status = "运行中" if s.get("running") else ("停用" if not s.get("enabled") else "待命")
                es_parts.append(f"{s['id']}（{s.get('kind', '?')}·{status}）")
            lines.append(f"事件源（{len(sources)}）：" + "、".join(es_parts))
    except Exception:
        logger.debug("config overview: event-source summary section failed; omitted", exc_info=True)

    # 技能
    skills_cfg = cfg.skills
    default_include = getattr(skills_cfg, "default_include", None) or []
    if default_include and default_include != ["*"]:
        lines.append(f"技能默认启用：{', '.join(default_include)}")
    elif default_include == ["*"]:
        lines.append("技能默认启用：全部")

    return "\n".join(lines) if lines else None


__all__ = ["build_config_overview"]
