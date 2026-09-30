"""用量展示的单一格式化来源"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.config import config_manager

# 命中率配色阈值：≥50% 为 good（各端统一，不再各自写死）
_CACHE_HIT_GOOD = 0.5


def format_usage_money(value: float | int | None) -> str:
    """金额：¥ 千分位；≥100 整数，≥1 两位小数，≥0.0001 四位小数，极小 <¥0.0001。"""
    n = float(value or 0)
    if not n > 0:
        return "¥0"
    if n >= 100:
        return f"¥{n:,.0f}"
    if n >= 1:
        return f"¥{n:,.2f}"
    if n >= 0.0001:
        return f"¥{n:,.4f}"
    return "<¥0.0001"


def format_token_count(value: int | float | None) -> str:
    """词元计数：一万以下千分位；一万及以上一律折算 M（0.01M 起，≥1M 一位小数，去尾零）。"""
    v = int(value or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if v >= 10_000:
        return f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    return f"{v:,}"


def format_hit_rate(rate: float | None) -> str:
    """0..1 → 百分比文案（0-100 截断，一位小数）。"""
    return f"{hit_rate_pct(rate):.1f}%"


def hit_rate_pct(rate: float | None) -> float:
    """0..1 → 0-100 数值（一位小数），供进度条/仪表渲染宽度。"""
    return round(min(max(float(rate or 0.0) * 100.0, 0.0), 100.0), 1)


def cost_state(cost_total: float | None, input_tokens: int | None) -> str:
    """费用单元格状态：priced（正常金额）/ unpriced（有输入未配价）/ zero。"""
    if float(cost_total or 0) > 0:
        return "priced"
    return "unpriced" if int(input_tokens or 0) > 0 else "zero"


def cache_hit_level(rate: float | None) -> str:
    """命中率配色档位：≥50% good（绿），否则 normal。"""
    return "good" if float(rate or 0.0) >= _CACHE_HIT_GOOD else "normal"


def _parse_iso_local(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or UTC
        dt = dt.replace(tzinfo=local_tz).astimezone(UTC)
    return dt.astimezone()


def format_ts_display(ts: str | None) -> str:
    """明细行时间：本地 'YYYY-MM-DD HH:MM'；解析失败回退原串前 16 位去 T。"""
    raw = (ts or "").strip()
    dt = _parse_iso_local(raw)
    if dt is None:
        return raw[:16].replace("T", " ") or "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def format_day_display(ts: str | None) -> str:
    """时间窗日期：本地 'YYYY-MM-DD'；解析失败回退原串前 10 位。"""
    raw = (ts or "").strip()
    dt = _parse_iso_local(raw)
    if dt is None:
        return raw[:10]
    return dt.strftime("%Y-%m-%d")


def decorate_token_totals(row: dict[str, Any]) -> dict[str, Any]:
    """给 bucket 输出行（totals 与各分组行共用）追加 ``*_display`` 展示字段"""
    input_tokens = int(row.get("input_tokens") or 0)
    rate = float(row.get("cache_hit_rate") or 0.0)
    cost_total = row.get("cost_total")
    row["input_display"] = format_token_count(input_tokens)
    row["output_display"] = format_token_count(row.get("output_tokens"))
    row["cache_read_display"] = format_token_count(row.get("cache_read_tokens"))
    row["reasoning_display"] = format_token_count(row.get("reasoning_tokens"))
    row["cost_total_display"] = format_usage_money(cost_total)
    row["cost_miss_display"] = format_usage_money(row.get("cost_miss"))
    row["cost_hit_display"] = format_usage_money(row.get("cost_hit"))
    row["cost_out_display"] = format_usage_money(row.get("cost_out"))
    row["cache_hit_display"] = format_hit_rate(rate)
    row["cache_hit_pct"] = hit_rate_pct(rate)
    row["cache_hit_level"] = cache_hit_level(rate)
    row["cost_state"] = cost_state(cost_total, input_tokens)
    row["has_cost_breakdown"] = any(float(row.get(k) or 0) > 0 for k in ("cost_miss", "cost_hit", "cost_out"))
    return row


def _names_from_available(provider_name: str, available: list[Any], into: dict[str, str]) -> None:
    for entry in available:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if model_id and name:
            into[f"{provider_name}/{model_id}"] = name


def _names_from_providers_yaml(path: Path, into: dict[str, str]) -> None:
    from src.runtime.usage_pricing import _available_model_entries, _load_providers_yaml

    for provider_name, cfg in _load_providers_yaml(path).items():
        available = _available_model_entries(cfg)
        if available is not None:
            _names_from_available(str(provider_name), available, into)


def load_model_display_map(coara_home: Path | str | None = None) -> dict[str, str]:
    """读目录正式名：``provider/model`` → ``available[].name``。只供页面贴标签。"""
    result: dict[str, str] = {}
    from src.runtime.usage_pricing import _available_model_entries, _default_coara_home

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
            available = _available_model_entries(cfg)
            if available is not None:
                _names_from_available(str(provider_name), available, result)
        if result:
            return result

    home = Path(coara_home) if coara_home else None
    if home is None:
        default = _default_coara_home()
        home = Path(default) if default else None
    candidates: list[Path] = []
    if home is not None:
        from src.core.coara_home import system_dir_for_home

        candidates.append(system_dir_for_home(home) / "providers.yaml")
    candidates.append(Path("providers.yaml"))
    for path in candidates:
        if path.is_file():
            _names_from_providers_yaml(path, result)
            if result:
                break
    return result


def resolve_model_label(
    provider: str,
    model: str,
    display_map: dict[str, str] | None = None,
) -> str:
    """页面用模型标签：目录正式名；历史别名旁注原 id；否则 ``provider·model``。

    只影响 ``label`` / ``model_label``，不改变聚合键 ``provider/model``。
    """
    provider_s = str(provider or "").strip()
    model_s = str(model or "").strip()
    if not provider_s and not model_s:
        return "（未知模型）"
    fallback = f"{provider_s}·{model_s}" if provider_s and model_s else (model_s or provider_s)
    if not display_map:
        return fallback
    from src.runtime.usage_pricing import resolve_catalog_model_key

    catalog_key = resolve_catalog_model_key(display_map, provider_s, model_s)
    if catalog_key is None:
        return fallback
    name = str(display_map.get(catalog_key) or "").strip()
    if not name:
        return fallback
    catalog_model = catalog_key.split("/", 1)[1] if "/" in catalog_key else catalog_key
    if catalog_model.lower() != model_s.lower():
        return f"{name}（{model_s}）"
    return name
