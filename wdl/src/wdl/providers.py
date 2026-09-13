"""wdl 自建最小 provider 体系。

配置文件：``<wdl_home>/providers.yaml``（WDL_HOME 或 ~/.wdl 下），形态：

.. code-block:: yaml

    default: my-openai          # 全局默认 provider（节点/图未指定时用）
    settings:                   # 可选：工具循环参数（WDL 图级 settings 覆盖同名项）
      max_tool_rounds: 20       # 工具循环轮数上限
      shell_timeout: 120        # shell 工具超时秒数
    providers:
      my-openai:                # openai 兼容（chat/completions）
        type: openai
        base_url: https://api.openai.com/v1
        api_key: ${OPENAI_API_KEY}      # ${VAR} 引用环境变量
        models:
          - gpt-4o-mini
          - gpt-4o
      claude:                   # anthropic 兼容（messages）
        type: anthropic
        base_url: https://api.anthropic.com
        api_key: ${ANTHROPIC_API_KEY}
        models:
          - claude-sonnet-4-5

节点 LLM 解析顺序：节点级 provider/model > 图级 provider/model > 配置文件默认
（``default`` 指向的 provider 及其首个模型）。只给 provider 不给模型时，
取该 provider 模型列表第一项。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from wdl.errors import WDLError
from wdl.paths import providers_config_path

_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ProviderConfig(BaseModel):
    """单个 provider 配置。"""

    type: str = "openai"  # openai | anthropic
    base_url: str
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    timeout: float = 120.0
    max_tokens: int = 8192

    def resolved_api_key(self) -> str:
        """api_key 支持 ${VAR} 引用环境变量；其它形式原样返回。"""
        m = _ENV_REF_RE.match(self.api_key.strip())
        if m:
            return os.environ.get(m.group(1), "")
        return self.api_key


class ToolSettings(BaseModel):
    """工具循环参数（providers.yaml 顶层 settings；WDL 图级 settings 覆盖同名字段）。"""

    max_tool_rounds: int = 0  # 0=未配置（默认 20，见 executor.DEFAULT_MAX_TOOL_ROUNDS）
    shell_timeout: float = 0.0  # 0=未配置（默认 120s，见 tools.DEFAULT_SHELL_TIMEOUT）


class ProvidersConfig(BaseModel):
    """providers.yaml 根配置。"""

    default: str = ""
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    settings: ToolSettings = Field(default_factory=ToolSettings)


def load_providers_config(path: Path | str | None = None) -> ProvidersConfig:
    """加载 providers.yaml；文件不存在返回空配置。"""
    p = Path(path) if path is not None else providers_config_path()
    if not p.exists():
        return ProvidersConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WDLError(f"providers.yaml 不是合法 YAML：{exc}") from exc
    if raw is None:
        return ProvidersConfig()
    if not isinstance(raw, dict):
        raise WDLError("providers.yaml 根节点必须是对象")
    providers_raw = raw.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise WDLError("providers.yaml 的 providers 必须是对象")
    providers: dict[str, ProviderConfig] = {}
    for name, item in providers_raw.items():
        if not isinstance(item, dict):
            raise WDLError(f"provider {name} 配置必须是对象")
        data: dict[str, Any] = dict(item)
        if not str(data.get("base_url") or "").strip():
            raise WDLError(f"provider {name} 缺少 base_url")
        providers[str(name)] = ProviderConfig(**data)
    settings_raw = raw.get("settings")
    if settings_raw is not None and not isinstance(settings_raw, dict):
        raise WDLError("providers.yaml 的 settings 必须是对象")
    return ProvidersConfig(
        default=str(raw.get("default") or "").strip(),
        providers=providers,
        settings=ToolSettings(**(settings_raw or {})),
    )


def merge_node_llm(
    node_provider: str | None,
    node_model: str | None,
    graph_provider: str | None,
    graph_model: str | None,
) -> tuple[str, str]:
    """合并节点级与图级 LLM 覆盖，返回显式 (provider, model)（可空串）。

    节点给了 provider 就只带节点自己的 model——图级 model 属于图级 provider，
    混搭必坏；节点只给 model 时挂到图级 provider 上。
    """
    np_ = (node_provider or "").strip()
    nm = (node_model or "").strip()
    if np_:
        return np_, nm
    return (graph_provider or "").strip(), nm or (graph_model or "").strip()


def resolve_node_llm(
    config: ProvidersConfig,
    provider: str | None,
    model: str | None,
) -> tuple[str, ProviderConfig, str]:
    """节点 LLM 最终解析：显式 > 配置文件默认。

    返回 (provider_name, provider_config, model)。provider 未配置、无可用
    模型或 api_key 缺失时抛 WDLError。
    """
    p = (provider or "").strip()
    m = (model or "").strip()
    if not p:
        p = config.default
    if not p:
        raise WDLError("未指定 provider 且 providers.yaml 未配置 default")
    cfg = config.providers.get(p)
    if cfg is None:
        raise WDLError(f"provider 未配置：{p}（见 providers.yaml）")
    if not m:
        if cfg.models:
            m = cfg.models[0]
        else:
            raise WDLError(f"provider {p} 未声明任何模型（models 列表为空）")
    if not cfg.resolved_api_key():
        raise WDLError(f"provider {p} 缺少 api_key（或引用的环境变量为空）")
    return p, cfg, m
