"""
Coara v8 - 异常定义

定义系统中所有自定义异常类型。
"""


class CoaraError(Exception):
    """Coara 基础异常"""

    pass


# ============================================================================
# 配置异常
# ============================================================================


class ConfigError(CoaraError):
    """配置错误"""

    pass


class ProviderNotFoundError(ConfigError):
    """Provider 未找到"""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        super().__init__(f"Provider '{provider_name}' not found")


# ============================================================================
# LLM 异常
# ============================================================================


class LLMError(CoaraError):
    """LLM 调用错误

    ``status_code``：来自 HTTP 状态的可选透传（driver 包装底层 APIStatusError 时
    带上）。retry 分类与降级决策优先走这个结构化字段，
    而不是靠错误消息文案猜。
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EmptyResponseError(LLMError):
    """正常收尾但零内容（无文本/工具/思考）的空响应。

    单独成类：调用方（如上下文压缩）需要把「模型给了空内容」与真正的
    provider 故障区分开，走不同的兜底语义。
    """

    pass


class APIKeyError(LLMError):
    """API 密钥错误"""

    pass


class ContextWindowExceededError(LLMError):
    """上下文窗口超出"""

    pass


class ProviderContentPolicyError(LLMError):
    """LLM provider rejected input/output (e.g. MiniMax 1026/1027 content moderation)."""

    pass


class ToolCallError(LLMError):
    """工具调用错误"""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"Tool '{tool_name}' error: {message}")


# ============================================================================
# Agent 异常
# ============================================================================


class AgentError(CoaraError):
    """Agent 错误"""

    pass


# ============================================================================
# Skill 异常
# ============================================================================


class SkillError(CoaraError):
    """Skill 错误"""

    pass


class SkillNotFoundError(SkillError):
    """Skill 未找到"""

    def __init__(self, skill_name: str):
        self.skill_name = skill_name
        super().__init__(f"Skill '{skill_name}' not found")


# ============================================================================
# Tool 异常
# ============================================================================


class ToolError(CoaraError):
    """Tool 错误"""

    pass


class ToolNotFoundError(ToolError):
    """Tool 未找到"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' not found")


