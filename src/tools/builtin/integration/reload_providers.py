"""reload_providers 工具 — 配置助手改完 providers.yaml 后触发热重载。

仅挂在配置模块会话（module_root 按模块挂载），不进主会话工具面。
"""

from __future__ import annotations

from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult


class ReloadProvidersTool(BaseTool):
    name = "reload_providers"
    summary = "重读 providers.yaml 并热重载模型服务商，立即生效"
    display_name = "Reload Providers"
    description = """重读 providers.yaml 并热重载模型服务商（provider），改完立即生效，无需重启

适用
- 刚用 edit / write 修改了 providers.yaml（新增、修改、停用、删除 provider）之后调用一次
- 新增/变更的 provider 会重建连接，删除的会移除，未变的保留；正在进行的对话不受影响

不适用
- 修改的是 config.yaml 其它项（如 records.enabled）——那些仍需重启内核
- 没改过 providers.yaml 时不必调用"""
    kind = ToolKind.EXECUTE
    category = "system"
    owner_only = True
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _ReloadProvidersInvocation(params)


class _ReloadProvidersInvocation(ToolInvocation):
    async def execute(self, signal=None) -> ToolResult:
        from src.core.config import config_manager
        from src.llm.registry import reload_providers

        try:
            await config_manager.reload()
            result = await reload_providers(config_manager)
        except Exception as exc:
            return ToolResult.error(f"热重载失败：{exc}。配置本身已写入磁盘，重启内核后生效")
        parts = []
        if result["added"]:
            parts.append(f"新增 {', '.join(result['added'])}")
        if result["replaced"]:
            parts.append(f"更新 {', '.join(result['replaced'])}")
        if result["removed"]:
            parts.append(f"移除 {', '.join(result['removed'])}")
        summary = "；".join(parts) if parts else "无变更"
        return ToolResult.success(f"已热重载：{summary}。模型选择器现在即可用")

    def get_description(self) -> str:
        return "热重载模型服务商配置"
