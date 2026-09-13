"""启动时 API key 审计：检查 → 快照对比 → 状态变更才写系统消息。

职责边界：
- **key 有无的权威判定复用工具侧实现**（不重复维护 env 名，天然与运行时一致）：
  - web_search：``WebSearchRawTool.PROVIDER_ENV_KEYS``
  - media：``media.providers.agnes_key()``
  - LLM：``config_manager.get_provider(...).api_key_env``
- 本模块只做：汇总、快照去重、生成提醒文案、写系统消息。

「移除」语义分层：
- web_search：无 key 的 provider 从可用集合剔除（baidu 免 key 恒可用，工具本身永不移除）。
- media：无 AGNES_API_KEY → 工具不注册。
- LLM：不移除（provider 无 key 也注册，保证 /model 列表完整、首次调用才报错），只提醒。

去重：状态指纹（``capability:provider`` → 有无 key）与持久化快照对比，
未变则静默；变更才写系统消息并更新快照。频繁重启不会重复提醒。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.logger import logger

_SNAPSHOT_FILE = "apikey_state.json"

# 展示层文案：去哪申请 key（工具侧没有这份元数据）。
# key 名以工具侧权威定义为准，这里只映射「申请入口」。
_SEARCH_HOW_TO_GET: dict[str, str] = {
    "exa": "exa.ai 申请 EXA_API_KEY",
    "serper": "serper.dev 申请 SERPER_API_KEY",
    "linkup": "linkup.so 申请 LINKUP_API_KEY",
    "doubao": "火山方舟控制台申请 DOUBAO_API_KEY",
}
_MEDIA_HOW_TO_GET: dict[str, str] = {
    "agnes": "api.agnes-ai.cn 申请 AGNES_API_KEY",
}

# 不参与「移除」的搜索 provider（免 key，恒可用）。
_NO_KEY_SEARCH_PROVIDERS = ("baidu",)


def available_search_providers() -> list[str]:
    """有 key 的 web_search provider（不含免 key 的 baidu）。"""
    from src.tools.builtin.web.raw_tool import WebSearchRawTool

    env_keys = WebSearchRawTool.PROVIDER_ENV_KEYS
    available = list(_NO_KEY_SEARCH_PROVIDERS)
    for provider in env_keys:
        if all(_is_usable_key(_getenv(k)) for k in env_keys[provider]):
            available.append(provider)
    return available


def available_media_providers() -> list[str]:
    """有 key 的 media provider（key 函数调用成功即视为可用）。"""
    from src.tools.builtin.media import providers

    available: list[str] = []
    try:
        providers.agnes_key()
        available.append("agnes")
    except ValueError:
        pass
    return available


def _missing_search_providers() -> list[str]:
    from src.tools.builtin.web.raw_tool import WebSearchRawTool

    env_keys = WebSearchRawTool.PROVIDER_ENV_KEYS
    return [p for p in env_keys if not all(_is_usable_key(_getenv(k)) for k in env_keys[p])]


def _missing_media_providers() -> list[str]:
    from src.tools.builtin.media import providers

    try:
        providers.agnes_key()
        return []
    except ValueError:
        return ["agnes"]


def _missing_llm_providers(config_manager: Any) -> list[tuple[str, str]]:
    """Return (provider_name, api_key_env) pairs lacking a usable key."""
    missing: list[tuple[str, str]] = []
    for name in config_manager.list_providers():
        provider = config_manager.get_provider(name)
        env_name = (getattr(provider, "api_key_env", "") or "").strip()
        if not env_name:
            continue
        if not _is_usable_key(_getenv(env_name)):
            missing.append((name, env_name))
    return missing


def _getenv(name: str) -> str:
    import os

    return os.getenv(name, "").strip()


def _is_usable_key(value: str) -> bool:
    """API key 可用性：非空 + printable ASCII（可安全放进 HTTP header）。

    参考 deepseek-harness 的 key 校验：空白/控制字符/非 ASCII 的值无法承载
    在 HTTP header 里，视为格式非法（等价未配置），提醒用户重新填写——
    避免拖到网络层才暴露 opaque 错误。
    """
    if not value:
        return False
    return all(0x21 <= ord(ch) <= 0x7E for ch in value)


def _state_fingerprint(missing: dict[str, list[str]]) -> str:
    """稳定指纹：capability:provider 的缺失集合，排序后序列化。"""
    parts: list[str] = []
    for capability in sorted(missing):
        for provider in sorted(missing[capability]):
            parts.append(f"{capability}:{provider}")
    return json.dumps(parts, ensure_ascii=True)


def _snapshot_path(coara_home: str | Path) -> Path:
    return Path(coara_home) / "system" / _SNAPSHOT_FILE


def _load_snapshot(coara_home: str | Path) -> str | None:
    path = _snapshot_path(coara_home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("fingerprint") or "")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _save_snapshot(coara_home: str | Path, fingerprint: str) -> None:
    path = _snapshot_path(coara_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"fingerprint": fingerprint}, ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning(f"apikey snapshot persist failed: {exc}")


def build_missing_map(config_manager: Any) -> dict[str, list[str]]:
    """汇总当前缺失的 key：capability → provider 列表（仅需 key 的项）。"""
    missing: dict[str, list[str]] = {}
    search_missing = _missing_search_providers()
    if search_missing:
        missing["web_search"] = search_missing
    media_missing = _missing_media_providers()
    if media_missing:
        missing["media"] = media_missing
    llm_missing = _missing_llm_providers(config_manager)
    if llm_missing:
        missing["llm"] = [f"{name}({env})" for name, env in llm_missing]
    return missing


def render_reminder(missing: dict[str, list[str]], coara_home: str | Path) -> str:
    """生成提醒正文：缺哪些 key、去哪申请、填哪。"""
    env_path = f"{Path(coara_home) / 'system' / '.env'}"
    lines: list[str] = []
    if "web_search" in missing:
        names = ", ".join(missing["web_search"])
        lines.append(f"- web_search 缺 key：{names}")
        for p in missing["web_search"]:
            if p in _SEARCH_HOW_TO_GET:
                lines.append(f"    {_SEARCH_HOW_TO_GET[p]}")
    if "media" in missing:
        names = ", ".join(missing["media"])
        lines.append(f"- media 缺 key：{names}")
        for p in missing["media"]:
            if p in _MEDIA_HOW_TO_GET:
                lines.append(f"    {_MEDIA_HOW_TO_GET[p]}")
    if "llm" in missing:
        lines.append(f"- LLM provider 缺 key：{', '.join(missing['llm'])}")
        lines.append("    在对应服务商官网申请，填入下方 .env")
    lines.append(f"\n填写位置：{env_path}（填后重启生效）")
    return "\n".join(lines)


def audit_api_keys(config_manager: Any, coara_home: str | Path) -> bool:
    """启动审计：状态变更时写系统消息并更新快照，返回「是否写了提醒」。

    首次运行（无快照）视为变更，写一条提醒；此后状态不变则静默。
    """
    from src.core.system_messages import add_system_message

    missing = build_missing_map(config_manager)
    fingerprint = _state_fingerprint(missing)
    previous = _load_snapshot(coara_home)

    if previous == fingerprint:
        logger.debug(f"API key audit: no change ({fingerprint or 'all set'})")
        return False

    if missing:
        title = f"API key 未配置（{sum(len(v) for v in missing.values())} 项）"
        body = render_reminder(missing, coara_home)
    else:
        title = "API key 已全部配置"
        body = "所有已登记的 API key 均已配置。"
    add_system_message(coara_home, kind="api_key", title=title, body=body)
    _save_snapshot(coara_home, fingerprint)
    logger.info(f"API key audit: state changed, reminder written ({fingerprint})")
    return True
