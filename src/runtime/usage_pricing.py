"""LLM 价格表读取与单轮费用计算（用量事件流的派生视图）。

价格按模型配置在 providers.yaml 的 ``models.available[].pricing``（元/百万 token），
是纯配置、随时可改，因此聚合时现读现算，不做持久化快照。未配价的模型只记 token、
费用记 0，由展示层标注「未配置价格」。

费用口径（见 docs/USAGE_COST_DASHBOARD.md §3，跨 provider 统一）：
- 有效输入 total = input + cache_read + cache_creation（OpenAI 风格 input 已含缓存时 cache 为 0）
- 非命中费 = max(0, total − cached) × input（缓存创建不单独计价，并入非命中按输入价）
- 命中费   = cached × cache_hit
- 输出费   = output × output
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import config_manager
from src.llm.usage import cache_read_tokens, total_prompt_tokens


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_m: float = 0.0
    cache_hit_per_m: float = 0.0
    output_per_m: float = 0.0


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_coara_home() -> str | None:
    """从当前配置管理器解析默认 coara_home（无则 None）。"""
    cfg = getattr(config_manager, "_config", None)
    home = getattr(cfg, "coara_home", None) if cfg else None
    return str(home) if home else None


def _pricing_from_available(provider_name: str, available: list[Any], into: dict[str, ModelPricing]) -> None:
    for entry in available:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        pricing = entry.get("pricing")
        if not model_id or not isinstance(pricing, dict):
            continue
        into[f"{provider_name}/{model_id}"] = ModelPricing(
            input_per_m=_float(pricing.get("input")) or 0.0,
            cache_hit_per_m=_float(pricing.get("cache_hit")) or 0.0,
            output_per_m=_float(pricing.get("output")) or 0.0,
        )


def _pricing_from_providers_yaml(path: Path, into: dict[str, ModelPricing]) -> None:
    """直接解析 providers.yaml（devtools 等未 load config_manager 时的回退）。"""
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return
    for provider_name, cfg in providers.items():
        if not isinstance(cfg, dict):
            continue
        available = ((cfg.get("models") or {}) if isinstance(cfg.get("models"), dict) else {}).get("available")
        if isinstance(available, list):
            _pricing_from_available(str(provider_name), available, into)


def load_base_pricing_map(coara_home: Path | str | None = None) -> dict[str, ModelPricing]:
    """仅读 providers.yaml 配置价（不合并价格覆盖）。价格编辑列表用它取「默认价」。

    优先用已 load 的 ``config_manager``；未加载时回退读磁盘
    ``<coara_home>/system/providers.yaml``（及仓根 ``providers.yaml``），
    保证独立 ``coara-devtools`` 也能算费用。
    """
    result: dict[str, ModelPricing] = {}
    if getattr(config_manager, "_config", None) is not None:
        try:
            provider_names = config_manager.list_providers()
        except Exception:
            provider_names = []
        for provider_name in provider_names:
            try:
                cfg = config_manager.get_provider(provider_name)
            except Exception:
                continue
            available = (cfg.models or {}).get("available")
            if isinstance(available, list):
                _pricing_from_available(provider_name, available, result)
        if result:
            return result

    # 未 load / 空表：直接读 yaml
    candidates: list[Path] = []
    if coara_home:
        from src.core.coara_home import system_dir_for_home

        candidates.append(system_dir_for_home(Path(coara_home)) / "providers.yaml")
    # 仓根回退：从本文件向上找带 providers.yaml 的仓库根（避免依赖 config 私有 API）
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "providers.yaml").is_file():
            candidates.append(parent / "providers.yaml")
            break
        if (parent / "pyproject.toml").is_file() and (parent / "providers.yaml.example").is_file():
            break
    home_fallback = _default_coara_home()
    if home_fallback:
        from src.core.coara_home import system_dir_for_home

        candidates.append(system_dir_for_home(Path(home_fallback)) / "providers.yaml")
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        _pricing_from_providers_yaml(path, result)
        if result:
            break
    return result


def _catalog_keys_for_provider(keys: list[str], provider: str) -> list[str]:
    provider_lower = str(provider or "").strip().lower()
    if not provider_lower:
        return []
    return [key for key in keys if "/" in key and key.split("/", 1)[0].lower() == provider_lower]


def resolve_catalog_model_key(
    catalog_keys: list[str] | dict[str, Any],
    provider: str,
    model: str,
) -> str | None:
    """把事件里的 (provider, model) 对齐到目录里的 ``provider/model`` 键。

    与 :func:`lookup_pricing` 同一套历史改名回退（精确 → 去 ``[…]`` → 前缀 →
    该 provider 仅一模型）。找不到返回 None。
    """
    keys = list(catalog_keys.keys()) if isinstance(catalog_keys, dict) else list(catalog_keys)
    provider_key = str(provider or "").strip()
    model_key = str(model or "").strip()
    full = f"{provider_key}/{model_key}" if provider_key and model_key else (model_key or provider_key)
    if not full:
        return None
    if full in (catalog_keys if isinstance(catalog_keys, dict) else set(keys)):
        return full
    # dict 精确 miss 时仍可能大小写不一致：再扫一遍
    full_lower = full.lower()
    for key in keys:
        if key.lower() == full_lower:
            return key

    candidates = _catalog_keys_for_provider(keys, provider_key)
    if not candidates:
        return None

    base_model = model_key.lower().split("[", 1)[0]
    if base_model and base_model != model_key.lower():
        for key in candidates:
            if key.split("/", 1)[1].lower() == base_model:
                return key

    best_len = -1
    best: str | None = None
    for key in candidates:
        cand_model = key.split("/", 1)[1].lower()
        if not cand_model or not base_model:
            continue
        if (base_model.startswith(cand_model) or cand_model.startswith(base_model)) and len(cand_model) > best_len:
            best_len = len(cand_model)
            best = key
    if best is not None:
        return best

    if len(candidates) == 1:
        return candidates[0]
    return None


def lookup_pricing(
    pricing_map: dict[str, ModelPricing] | None,
    provider: str,
    model: str,
) -> ModelPricing | None:
    """按事件里的 (provider, model) 找价格，容忍模型改名的历史写法。

    历史事件里的模型名换过好几轮（``deepseek-v4-flash`` → ``deepseek-flash``、
    ``k3[1m]`` → ``k3``、``MiniMax-M2.7-highspeed`` → ``MiniMax-M3``），精确 key
    匹配会让整段历史按 0 元计价——那既不是"没花费"也不是"便宜"，是账目消失。
    回退顺序见 :func:`resolve_catalog_model_key`。

    仍找不到返回 None：调用方按 0 计并记一条 unpriced，绝不猜一个价。
    """
    if not pricing_map:
        return None
    key = resolve_catalog_model_key(pricing_map, provider, model)
    if key is None:
        return None
    return pricing_map.get(key)


def load_pricing_map(coara_home: Path | str | None = None) -> dict[str, ModelPricing]:
    """``provider/model`` → 价格；key 与用量聚合的 model_key（provider/model）对齐。

    先读 providers.yaml 的配置价，再用价格覆盖文件（``provider/model`` 精确覆盖）
    合并，覆盖优先；覆盖项若命中了未配置在 providers.yaml 的模型也一并纳入。
    """
    result = load_base_pricing_map(coara_home)
    try:
        from src.runtime.pricing_store import load_overrides

        overrides = load_overrides(coara_home or _default_coara_home())
    except Exception:
        overrides = {}
    for key, fields in overrides.items():
        base_mp = result.get(key)
        if base_mp is not None:
            # 字段级覆盖：只替换覆盖文件中出现的字段，其余继承默认配置
            result[key] = ModelPricing(
                input_per_m=_float(fields["input"]) if "input" in fields else base_mp.input_per_m,
                cache_hit_per_m=_float(fields["cache_hit"]) if "cache_hit" in fields else base_mp.cache_hit_per_m,
                output_per_m=_float(fields["output"]) if "output" in fields else base_mp.output_per_m,
            )
        else:
            result[key] = ModelPricing(
                input_per_m=_float(fields.get("input")) or 0.0,
                cache_hit_per_m=_float(fields.get("cache_hit")) or 0.0,
                output_per_m=_float(fields.get("output")) or 0.0,
            )
    return result


def pricing_is_configured(pricing: ModelPricing | None) -> bool:
    if pricing is None:
        return False
    return pricing.input_per_m > 0 or pricing.cache_hit_per_m > 0 or pricing.output_per_m > 0


def compute_turn_cost(usage: dict[str, Any] | None, pricing: ModelPricing | None) -> dict[str, float]:
    """单小轮费用（元）：miss / hit / out / total；未配价返回全 0。"""
    usage = usage or {}
    total = total_prompt_tokens(usage)
    cached = cache_read_tokens(usage)
    output = int(usage.get("output_tokens") or 0)
    if pricing is None:
        return {"cost_miss": 0.0, "cost_hit": 0.0, "cost_out": 0.0, "cost_total": 0.0}
    miss = max(0, total - cached) * pricing.input_per_m / 1_000_000
    hit = cached * pricing.cache_hit_per_m / 1_000_000
    out = output * pricing.output_per_m / 1_000_000
    # 不在此处 round：中间精度保留给上层累加，输出端统一 round（见 usage_query / detail）
    return {
        "cost_miss": miss,
        "cost_hit": hit,
        "cost_out": out,
        "cost_total": miss + hit + out,
    }


__all__ = [
    "ModelPricing",
    "compute_turn_cost",
    "load_base_pricing_map",
    "load_pricing_map",
    "lookup_pricing",
    "pricing_is_configured",
    "resolve_catalog_model_key",
]
