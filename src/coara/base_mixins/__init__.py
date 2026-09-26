"""CoaraBase 职责组 mixin 包。

宿主为 CoaraBase（src/coara/base.py），属性在宿主 __init__ 初始化。
"""

from src.coara.base_mixins.continuation import ContinuationMixin
from src.coara.base_mixins.foreground_delegates import ForegroundDelegateMixin
from src.coara.base_mixins.persistence import PersistenceMixin
from src.coara.base_mixins.prompt_skills import PromptSkillsMixin
from src.coara.base_mixins.tools_registry import ToolsRegistryMixin
from src.coara.base_mixins.trace import TraceMixin

__all__ = [
    "ContinuationMixin",
    "ForegroundDelegateMixin",
    "PersistenceMixin",
    "PromptSkillsMixin",
    "ToolsRegistryMixin",
    "TraceMixin",
]
