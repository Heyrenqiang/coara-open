"""Base classes for tools with declarative invocation pattern."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from src.core.errors import ToolError


class ToolKind(StrEnum):
    """Tool category for permission defaults and UI display."""

    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    SEARCH = "search"
    EXECUTE = "execute"
    FETCH = "fetch"
    THINK = "think"
    OTHER = "other"


class ToolResult:
    """Result returned by a tool."""

    def __init__(
        self,
        content: str | list[dict[str, Any]],
        is_error: bool = False,
        metadata: dict[str, Any] | None = None,
        cancelled: bool = False,
        display: list[Any] | None = None,
    ):
        self.content = content
        self.is_error = is_error
        self.metadata = metadata or {}
        self.is_cancelled = cancelled
        # CLI-only display blocks (diff panels). Never injected into LLM history.
        self.display: list[Any] = list(display) if display else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "is_error": self.is_error,
            "metadata": self.metadata,
            "is_cancelled": self.is_cancelled,
        }

    @classmethod
    def success(
        cls,
        content: str | list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        *,
        display: list[Any] | None = None,
    ) -> ToolResult:
        return cls(content=content, is_error=False, metadata=metadata, display=display)

    @classmethod
    def error(cls, message: str, metadata: dict[str, Any] | None = None) -> ToolResult:
        return cls(content=message, is_error=True, metadata=metadata)

    @classmethod
    def cancelled(cls, message: str = "用户取消了操作。", metadata: dict[str, Any] | None = None) -> ToolResult:
        return cls(content=message, is_error=False, cancelled=True, metadata=metadata)


class ToolInvocation(ABC):
    """Represents a validated, executable tool call.

    Validation lives inside ``execute()`` — invalid params or paths should
    return ``ToolResult.error()`` there. Approval is decided by the tool
    class via ``requires_approval`` (see ``BaseTool``), not here.
    """

    def __init__(self, params: dict[str, Any]):
        self.params = params

    def bind_runtime_context(
        self,
        *,
        tool_call_id: str | None = None,
        session_id: str | None = None,
        coara_id: str = "",
        origin_source: str | None = None,
        workspace_dir: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """注入本轮执行的运行时上下文（executor 在 create_invocation 后调用一次）。

        ToolInvocation 是普通 ABC（非 frozen dataclass），可直接赋值；
        个别 invocation 子类以冻实例/槽位实现时，这里集中用 ``object.__setattr__``
        兜底，保证注入接口单点可控。工具据此完成 trace/UI 关联（如 delegate 的
        parent_tool_call_id）与完成结果回投端路由（origin_source）。

        ``workspace_dir`` / ``turn_id`` 是**发起者归属**：产出可能异步完成（媒体
        文件、投递卡片）的工具按它寻址，绝不能读「完成那一刻的端视图」——那会把
        别的空间的产出写进当前空间的线（实测事故：nx 请求的视频落到 v8 线）。
        """
        object.__setattr__(self, "tool_call_id", tool_call_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "coara_id", coara_id)
        object.__setattr__(self, "origin_source", origin_source)
        object.__setattr__(self, "workspace_dir", workspace_dir)
        object.__setattr__(self, "turn_id", turn_id)

    @abstractmethod
    def get_description(self) -> str:
        """Return a human-readable description of this tool call."""

    @abstractmethod
    async def execute(self, signal=None) -> ToolResult:
        """Execute the tool call and return the result.

        Args:
            signal: Optional AbortSignal that the implementation should check
                periodically. If ``signal.aborted`` is True, the tool should
                stop and return ``ToolResult.cancelled()``.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.get_description()})"


class BaseTool(ABC):
    """Abstract base class for tools using declarative invocation pattern."""

    name: str = ""
    description: str = ""
    summary: str = ""  # 前置简介（挂起池/列表给 LLM 看）；为空则回退 description 首行
    display_name: str = ""
    parameters_schema: dict[str, Any] = {}
    owner_only: bool = False
    category: str = "general"
    kind: ToolKind = ToolKind.OTHER
    should_defer: bool = False  # True = 延迟加载，初始不暴露给 LLM

    # Approval declaration. Static tools use a bool; dynamic tools (e.g. shell)
    # override with a @staticmethod that takes the call arguments and returns
    # a bool. The executor computes the final decision as:
    #   needs_approval = tool.requires_approval(args) OR llm.require_approval
    requires_approval: bool | staticmethod = False

    def __init__(self):
        if not self.name:
            raise ToolError(f"Tool must have a name. Class: {self.__class__.__name__}")
        if not self.description:
            raise ToolError(f"Tool '{self.name}' must have a description")
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()

    @property
    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "display_name": self.display_name,
            "parameters": self.parameters_schema,
            "owner_only": self.owner_only,
            "category": self.category,
            "kind": self.kind.value,
        }

    def get_execution_timeout(self, default_timeout: float, args: dict[str, Any] | None = None) -> float | None:
        """Return the executor-side timeout for this tool.

        Returning ``None`` disables the outer ``asyncio.wait_for`` wrapper so
        the tool controls its own lifetime. ``args`` 是本次调用参数，供工具
        按 per-call 声明超时（如 shell 的 timeout_ms）；返回有限秒数时，
        到点超时即失败：任务被取消（shell 类杀进程树）并返回明确超时错误。
        """
        return default_timeout

    def get_write_lock(self, args: dict[str, Any]) -> str | None:
        """Return a write-lock key for this call, or None if it has no shared mutable state.

        The executor并发地执行同一批 tool_calls 中锁键不同的调用、串行执行锁键相同的调用。
        返回 None 表示只读/无副作用，可与任何其它调用并发。

        Subclasses that mutate a shared resource (同一文件、同一会话状态、同一记录)
        should override this and return a stable string key identifying that resource.
        Default returns None — tools are assumed concurrent-safe unless they declare otherwise.
        """
        return None

    @abstractmethod
    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        """Create a validated ToolInvocation from raw parameters.

        Subclasses should validate parameters here and raise ValueError
        if validation fails.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, kind={self.kind.value})"
