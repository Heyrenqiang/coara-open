"""模型价格覆盖存储：独立于 providers.yaml 的可写价格层"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.json_store import interprocess_file_lock, write_text_atomic

_OVERRIDE_FILENAME = "pricing_override.json"
_PRICING_KEYS = ("input", "cache_hit", "output")

# 用量页只展示这四家。元/百万 token。
FACTORY_PROVIDERS = frozenset({"deepseek", "kimi", "zhipu", "minimax"})
FACTORY_PRICING: dict[str, dict[str, float]] = {
    "deepseek/deepseek-flash": {"input": 1.0, "cache_hit": 0.02, "output": 4.0},
    "deepseek/deepseek-v4-pro": {"input": 4.5, "cache_hit": 0.15, "output": 13.5},
    "kimi/k3": {"input": 20.0, "cache_hit": 2.0, "output": 100.0},
    "zhipu/glm-5.3": {"input": 8.0, "cache_hit": 2.0, "output": 28.0},
    "zhipu/glm-5.3-flash": {"input": 0.8, "cache_hit": 0.23, "output": 2.8},
    "minimax/MiniMax-M3": {"input": 2.10, "cache_hit": 0.42, "output": 8.40},
    "minimax/MiniMax-M2.7-highspeed": {"input": 4.2, "cache_hit": 0.42, "output": 16.8},
}


def override_path(coara_home: Path | str | None) -> Path:
    """价格覆盖文件路径，位于与 providers.yaml 同级的 ``system/``。"""
    from src.core.coara_home import system_dir_for_home

    if not coara_home:
        raise ValueError("coara_home required to resolve pricing override path")
    return system_dir_for_home(Path(coara_home)) / _OVERRIDE_FILENAME


def _model_key(provider: str, model: str) -> str:
    return f"{provider}/{model}".strip("/")


def _read(path: Path) -> dict[str, dict[str, float]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for key, val in data.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        entry: dict[str, float] = {}
        for field in _PRICING_KEYS:
            raw = val.get(field)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                entry[field] = float(raw)
        if entry:
            result[key] = entry
    return result


def load_overrides(coara_home: Path | str | None) -> dict[str, dict[str, float]]:
    """读取全部价格覆盖（``provider/model`` → 各字段价格）。"""
    return _read(override_path(coara_home))


def save_override(
    coara_home: Path | str | None,
    provider: str,
    model: str,
    pricing: dict[str, Any] | None,
) -> Path:
    """写入/更新某 `provider/model` 覆盖价；``pricing=None`` 或全空时删除该覆盖项。

    返回覆盖文件路径。
    """
    key = _model_key(provider, model)
    path = override_path(coara_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 读改写整段持跨进程锁：web 配置页与 CLI 同时改价时，后写者不会拿旧快照覆盖
    # 先写者刚落的字段（锁文件仅作互斥，不承载数据）
    with interprocess_file_lock(path.with_name(f"{path.name}.lock")):
        overrides = load_overrides(coara_home)
        if pricing is None or (isinstance(pricing, dict) and not pricing):
            overrides.pop(key, None)
        else:
            # 字段级合并：只更新传入字段，其余保留现有覆盖值（避免单字段编辑误伤其它）
            entry: dict[str, float] = dict(overrides.get(key) or {})
            for field in _PRICING_KEYS:
                if field not in pricing:
                    continue  # 未提供 → 保留现有覆盖值
                raw = pricing.get(field)
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    entry[field] = float(raw)
                else:
                    entry.pop(field, None)  # 显式 null/非数值 → 清除该字段，回退默认
            if entry:
                overrides[key] = entry
            else:
                overrides.pop(key, None)
        write_text_atomic(path, json.dumps(overrides, ensure_ascii=False, indent=2) + "\n")
    return path


def list_pricing_entries(coara_home: Path | str | None = None) -> list[dict[str, Any]]:
    """列出出厂四家的标价（供用量页编辑展示）"""
    overrides = load_overrides(coara_home)
    entries: list[dict[str, Any]] = []
    for key in sorted(FACTORY_PRICING):
        provider, _, model = key.partition("/")
        if provider not in FACTORY_PROVIDERS:
            continue
        base = dict(FACTORY_PRICING[key])
        override = overrides.get(key)
        if override:
            pricing = dict(base)
            pricing.update(override)
            source = "override"
        else:
            pricing = base
            source = "config"
        entries.append(
            {
                "model_key": key,
                "provider": provider,
                "model": model,
                "pricing": pricing,
                "source": source,
            }
        )
    return entries
