"""统一视觉能力门控（对照 codex ``input_modalities`` 语义，三端 / 三 provider 共用）。

设计原则：
- 后端转换层（anthropic / openai / responses 的 ``_convert``）用它决定图片块
  「透传 input_image / image_url / image block」还是「替换为占位文案」——fail-closed，
  只有明确支持图像输入（显式配置或白名单命中）才透传，否则占位，避免非视觉模型收到
  图片被服务端 400。
- 输入侧（CLI Alt+V / Web 上传 / 发送前）用它提示「当前模型不支持图像」并保留草稿——
  对齐 codex ``attach_image`` 的前置检查。读不到配置时走白名单兜底。

模型元数据识别字段（providers.yaml ``models.available[]``）：
  - ``vision: true`` —— 显式声明支持图像输入
  - ``input_modalities: [image, text]`` 或 ``modalities: [image, ...]`` —— 结构化模态
白名单片段匹配兜底（不区分大小写），覆盖主流视觉型号。
"""

from __future__ import annotations

import re
from typing import Any

#: 图片被省略时替换的占位文案（对齐 codex "image content omitted because..."）。
IMAGE_OMITTED_PLACEHOLDER = "[图片内容：当前模型不支持图像输入，已省略]"

#: 视觉型号白名单（片段匹配，兜底防止配置未声明 vision 时误拦截）。
#: 仅保留通用能力 marker（*vision* / *-vision*），不指向任何具体品牌/年份——
#: 具体视觉模型一律由 providers.yaml 显式 ``vision: true`` 声明（配置为权威）。
_VISION_MODEL_MARKERS: tuple[str, ...] = (
    "vision",  # *-vision-* 系列；兜底任何含 vision 的型号
    "-vision",  # 兜底：任何含 -vision 的型号
)

def _normalize_modalities(value: Any) -> set[str] | None:
    """把 input_modalities / modalities 值归一成小写集合；不是模态描述则 None。"""
    if isinstance(value, str):
        return {part.strip().lower() for part in re.split(r"[,;\s]+", value) if part.strip()}
    if isinstance(value, (list, tuple)):
        return {str(v).strip().lower() for v in value if str(v).strip()}
    return None


def _entry_declares_vision(entry: dict[str, Any]) -> bool | None:
    """读单个模型条目的视觉声明。返回 True/False 表示显式声明；None 表示未声明。"""
    if not isinstance(entry, dict):
        return None
    # 显式布尔 vision
    if isinstance(entry.get("vision"), bool):
        return entry["vision"]
    if isinstance(entry.get("supports_vision"), bool):
        return entry["supports_vision"]
    # 结构化模态
    for field in ("input_modalities", "modalities"):
        mods = _normalize_modalities(entry.get(field))
        if mods is not None:
            return "image" in mods
    return None


def is_known_vision_model(model_id: str) -> bool:
    """白名单片段判定：命中即认为支持图像输入（不区分大小写）。"""
    if not model_id:
        return False
    m = (model_id or "").lower()
    return any(marker in m for marker in _VISION_MODEL_MARKERS)


def _find_model_entry(config_manager: Any, provider_name: str, model_id: str) -> dict[str, Any] | None:
    """在 provider 配置的 ``models.available[]`` 里按 id 精确定位模型条目。"""
    get_provider = getattr(config_manager, "get_provider", None)
    if not callable(get_provider):
        return None
    try:
        cfg = get_provider(provider_name)
    except Exception:
        return None  # 配置读取回落：拿不到证据返回 None，交上层按「未知放行」处理
    models = getattr(cfg, "models", None) or {}
    available = models.get("available") or []
    if not isinstance(available, list):
        return None
    for entry in available:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip().lower() == str(model_id or "").lower():
            return entry
    return None


def model_vision_explicit(
    model_id: str,
    *,
    provider_name: str = "",
    config_manager: Any | None = None,
) -> bool | None:
    """只按「显式证据」判定视觉能力：True / False / None（拿不到证据）。

    用途：**不持内核配置**的调用方（CLI/手机端在发送前做提示）不该凭白名单说"不支持"
    —— 白名单只认 *vision* 这类通用 marker，配置了 ``vision: true`` 的型号
    （deepseek-flash、k3、MiniMax-M3）在客户端会被判成不支持，把用户刚贴的图片吃掉。
    这类调用方拿到 None 时应放行（后端转换层仍按配置 fail-closed 占位），
    拿到 False（配置显式声明不支持）才提示并丢弃。
    """
    if not model_id:
        return None
    if config_manager and provider_name:
        entry = _find_model_entry(config_manager, provider_name, model_id)
        if entry is not None:
            declared = _entry_declares_vision(entry)
            if declared is not None:
                return declared
    if is_known_vision_model(model_id):
        return True
    return None


def model_supports_vision(
    model_id: str,
    *,
    provider_name: str = "",
    config_manager: Any | None = None,
    vision_model_ids: frozenset[str] | None = None,
) -> bool:
    """统一视觉门。

    - 显式配置（``models.available[]`` 里的 vision / input_modalities，或
      ``vision_model_ids`` 预先解析的集合）→ 优先，以它为准。
    - 配置里没有该模型 / 未声明视觉能力：走白名单兜底。
    - 全部无法判定：返回 False（fail-closed，后端安全占位）。
    """
    if not model_id:
        return False
    if vision_model_ids and model_id in vision_model_ids:
        return True
    if config_manager and provider_name:
        entry = _find_model_entry(config_manager, provider_name, model_id)
        if entry is not None:
            declared = _entry_declares_vision(entry)
            if declared is not None:
                return declared
    return is_known_vision_model(model_id)


def vision_model_ids_from_config(config: Any) -> frozenset[str]:
    """从 LLMProviderConfig 的 ``models.available[]`` 解析显式声明支持图像的模型 id。

    供 provider factory 注入转换层，使「配置了 vision:true」的模型真正透传图片。
    """
    models = getattr(config, "models", None) or {}
    available = models.get("available") or []
    ids: set[str] = set()
    if isinstance(available, list):
        for entry in available:
            if (
                isinstance(entry, dict)
                and str(entry.get("id") or "").strip()
                and _entry_declares_vision(entry)
            ):
                ids.add(str(entry["id"]).strip())
    return frozenset(ids)

