"""Flow 现场节点 LLM 绑定 — 会话内编排（FlowCoordinator）的节点模型选择。

2026-09-08 剥离说明：持久化执行层已迁出（独立 WDL 软件自带 provider 体系），
原 ``workflow.node`` profile 与 WebUI 设置页随之删除。本模块只服务 flow 现场
模式：节点未显式指定 provider/model 时跟随父会话（返回空串 = 调用方不覆盖）。

解析链（先到先得）：显式节点级 provider/model > 图级 > 父会话继承。
"""

from __future__ import annotations


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


def resolve_workflow_node_llm(provider: str | None, model: str | None) -> tuple[str, str]:
    """Return (provider, model)：仅透传显式覆盖；空串 = 跟随父会话当前模型。

    引擎剥离后没有独立的「工作流节点模型」配置面，flow 现场节点的默认行为
    与 spawn 子智能体一致：不显式指定就继承父会话。
    """
    return (provider or "").strip(), (model or "").strip()
