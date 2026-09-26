"""
Coara v8 - Core Module

核心抽象层：配置、日志、类型、异常。
"""

from src.core.config import ConfigManager, config_manager, get_config
from src.core.errors import (
    AgentError,
    CoaraError,
    ConfigError,
    LLMError,
    SkillError,
    ToolError,
)
from src.core.logger import logger, setup_logger
from src.core.types import (
    CoaraConfig,
    CoaraIdentity,
    CoaraPersona,
    CoaraStatus,
    LLMProviderConfig,
    Message,
    SkillDefinition,
    ToolCall,
    ToolDefinition,
    UnifiedMessage,
)

__all__ = [
    # Types
    "CoaraConfig",
    "LLMProviderConfig",
    "CoaraStatus",
    "CoaraPersona",
    "CoaraIdentity",
    "Message",
    "ToolCall",
    "SkillDefinition",
    "ToolDefinition",
    "UnifiedMessage",
    # Errors
    "CoaraError",
    "ConfigError",
    "LLMError",
    "AgentError",
    "ToolError",
    "SkillError",
    # Logger
    "logger",
    "setup_logger",
    # Config
    "ConfigManager",
    "config_manager",
    "get_config",
]
