"""Code Mode: ``ptc`` tool — model-written Python programs that orchestrate tool calls.

Framework-side PTC (Programmatic Tool Calling).
The model writes an async Python function body; the program runs in
a restricted in-process namespace with a ``tools`` object bound to the calling
agent's visible tools. Each ``await tools.<name>(...)`` re-enters the normal
tool execution pipeline (policy / approval / sandbox / spill / trace), so
existing tool policy applies unchanged — no tool ecosystem change, this is
just one more tool. Only the outer program output (logs + return value) reaches
the model context; intermediate tool results stay execution-local.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import textwrap
import uuid
from pathlib import Path
from typing import Any

from src.agent.executor import ToolExecutor
from src.core.tool_base import ToolKind, ToolResult
from src.core.types import ToolCall
from src.tools.builtin.file_io.workspace_tool_base import WorkspaceBoundTool, WorkspaceBoundToolInvocation

# ── 程序执行预算 ──
PTC_NAME = "ptc"
# 与 shell 单次上限齐平（300s）：ptc 里可以编排 shell 子调用，总闸门不该比子调用
# 自己的上限更紧。需要人工审批的调用、长时间执行的任务不要放进 ptc（见 description）。
_MAX_PROGRAM_SECONDS = 300
_MAX_OUTPUT_CHARS = 20_000
_MAX_CONCURRENT_SUB_CALLS = 4

# 程序内可用的受限标准库（不提供 import，模型无法绕过工具审计路径）
_ALLOWED_MODULES: tuple[str, ...] = ("asyncio", "json", "math", "re", "pathlib", "datetime")

_FORBIDDEN_CALL_HINTS = {
    "open": "读文件用 tools.read；写文件用 tools.write",
    "exec": "不支持动态执行",
    "eval": "不支持动态求值",
    "input": "无交互输入",
    "compile": "不支持动态编译",
}


class ToolCallError(Exception):
    """工具调用失败时抛给程序的异常（对应 DeepSeek 的 ToolCallError 语义）"""

    def __init__(self, tool_name: str, message: str):
        super().__init__(f"tool {tool_name} failed: {message}")
        self.tool_name = tool_name
        self.message = message

    @property
    def toolName(self) -> str:  # noqa: N802 — DeepSeek 兼容别名，程序里 e.toolName / e.tool_name 都可用
        return self.tool_name


def _forbidden_import(*_args: Any, **_kwargs: Any) -> None:
    """拦截程序内 import 语句，给出清晰引导而非暴露裁剪细节的 __import__ not found。"""
    raise ImportError(
        "ptc 程序内禁止 import 语句；已内置 asyncio / json / math / re / pathlib / datetime "
        "可直接使用，其余能力请通过 tools.<工具名> 调用（如 tools.read / tools.grep / tools.shell）"
    )


def _precheck_code(code: str) -> str | None:
    """静态预检：import / open / exec / eval 在提交时即报错（带行号与替代方案）。

    早失败省一次「跑到一半才炸」的执行往返（前面的子调用结果全部作废）。
    解析失败（缩进等）返回 None，交给 exec 阶段按语法错误报。
    """
    import ast

    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            problems.append(
                f"第 {node.lineno} 行：禁止 import —— 内置 asyncio / json / math / re / "
                "pathlib / datetime 可直接用；其余能力经 tools.<工具名>（read / grep / shell …）"
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALL_HINTS:
            hint = _FORBIDDEN_CALL_HINTS[node.func.id]
            problems.append(f"第 {node.lineno} 行：禁止 {node.func.id}() —— {hint}")
    return "\n".join(problems) if problems else None


def _restricted_builtins() -> dict[str, Any]:
    """裁剪 __builtins__：不含 open/exec/eval/compile 等逃逸入口，import 走清晰报错。

    安全姿态：ptc 只执行模型生成的代码（模型可信，且模型本就有 shell 全
    能力），此裁剪不是强隔离，而是把能力引导到 tools 命名空间，让每次副作用
    都经过现有审批/沙箱/审计路径。
    """
    safe_names = {
        "abs",
        "all",
        "any",
        "bool",
        "bytes",
        "dict",
        "dir",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "getattr",
        "hasattr",
        "hash",
        "id",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "object",
        "oct",
        "ord",
        "pow",
        "print",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "type",
        "zip",
        "chr",
        "divmod",
        "callable",
        "True",
        "False",
        "None",
        # 常用异常类型（程序需要 raise / try / except 时使用）
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "EOFError",
        "Exception",
        "ImportError",
        "IndexError",
        "KeyError",
        "LookupError",
        "MemoryError",
        "NameError",
        "NotImplementedError",
        "OSError",
        "OverflowError",
        "ReferenceError",
        "RuntimeError",
        "StopAsyncIteration",
        "StopIteration",
        "SyntaxError",
        "SystemError",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
        "CancelledError",
        "TimeoutError",
        "BaseException",
    }
    safe = {name: getattr(builtins, name) for name in safe_names if hasattr(builtins, name)}
    safe["__import__"] = _forbidden_import
    return safe


def _load_allowed_modules() -> dict[str, Any]:
    import importlib

    return {name: importlib.import_module(name) for name in _ALLOWED_MODULES}


class _ProgramTools:
    """程序可见的 tools 命名空间：只暴露当前 agent 的可见工具。

    每次 ``await tools.<name>(...)`` 构造一个 ToolCall 并复用
    ``ToolExecutor.execute`` 走完整流水线——hooks、审批、沙箱、spill、trace、
    循环检测全部生效。只读工具并发（信号量上限），写类工具全局串行。
    """

    def __init__(self, coara: Any, is_owner: bool, signal: Any):
        self._coara = coara
        self._is_owner = is_owner
        self._signal = signal
        self._executor = ToolExecutor()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUB_CALLS)
        self._write_lock = asyncio.Lock()
        # 可见工具集排除 ptc 自身：禁止程序内自递归（嵌套 PTC 超时叠乘）
        self._visible = set(coara._tool_manager.get_visible_tool_names(is_owner)) - {PTC_NAME}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._visible:
            raise ToolCallError(name, "工具不可见或不存在（不在当前 agent 的白名单内）")
        return self._make_binding(name)

    def _make_binding(self, name: str) -> Any:
        async def binding(**kwargs: Any) -> Any:
            return await self._dispatch(name, kwargs)

        return binding

    def _is_write_call(self, name: str, arguments: dict[str, Any]) -> bool:
        if name == "shell":
            # shell 可执行任意命令（含写盘副作用），强制串行，防止并发踩竞态
            return True
        tool = self._coara._tool_manager.tools.get(name)
        if tool is None:
            return False
        try:
            return tool.get_write_lock(arguments) is not None
        except Exception:
            return True

    async def _dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        is_write = self._is_write_call(name, arguments)
        async with self._semaphore:
            if is_write:
                async with self._write_lock:
                    return await self._execute_call(name, arguments)
            return await self._execute_call(name, arguments)

    async def _execute_call(self, name: str, arguments: dict[str, Any]) -> Any:
        call = ToolCall(id=f"code:{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)
        executions = await self._executor.execute(
            self._coara,
            [call],
            self._is_owner,
            signal=self._signal,
        )
        result = executions[0].result
        if result.is_error:
            raise ToolCallError(name, str(result.content))
        content = result.content
        return content if isinstance(content, str) else str(content)


class PtcToolInvocation(WorkspaceBoundToolInvocation):
    def __init__(
        self,
        params: dict[str, Any],
        workspace_root: Path | None = None,
        parent_coara: Any | None = None,
    ):
        super().__init__(params, workspace_root)
        self._parent_coara = parent_coara

    def get_description(self) -> str:
        desc = str(self.params.get("description") or "").strip()
        return f"ptc: {desc}" if desc else "ptc: 编排子工具"

    async def execute(self, signal=None) -> ToolResult:
        code = str(self.params.get("code") or "")
        description = str(self.params.get("description") or "").strip()
        if not code.strip():
            return ToolResult.error("ptc 缺少必填参数: code")
        problems = _precheck_code(code)
        if problems:
            return ToolResult.error(f"程序未执行（静态预检不通过）:\n{problems}")
        coara = self._parent_coara
        if coara is None:
            return ToolResult.error("ptc 需要绑定运行时")

        is_owner = getattr(coara, "_current_trust_level", "owner") == "owner"
        ns: dict[str, Any] = {
            "__builtins__": _restricted_builtins(),
            "tools": _ProgramTools(coara, is_owner, signal),
            "ToolCallError": ToolCallError,
        }
        ns.update(_load_allowed_modules())

        wrapped = "async def __ptc_program__():\n" + textwrap.indent(code, "    ")
        try:
            exec(wrapped, ns)  # noqa: S102 — 模型可信的代码，受限命名空间
        except SyntaxError as exc:
            return ToolResult.error(f"程序语法错误: {exc}")
        program = ns["__ptc_program__"]

        buffer = io.StringIO()
        result: Any = None
        try:
            with contextlib.redirect_stdout(buffer):
                result = await asyncio.wait_for(program(), timeout=_MAX_PROGRAM_SECONDS)
        except TimeoutError:
            return ToolResult.error(
                f"程序执行超时：{_MAX_PROGRAM_SECONDS}s 内未结束（可能死循环，或程序里放了长命令/等审批的调用）。"
                "请减小任务规模或拆分为多次 ptc；长命令与需要审批的调用请移出 ptc（改用 shell 或后台任务），不要原样重试"
            )
        except ToolCallError as exc:
            logs = buffer.getvalue()
            captured = f"\nCaptured output:\n{logs}" if logs.strip() else ""
            return ToolResult.error(
                f"程序中的工具调用失败（程序未捕获）: {exc.tool_name}: {exc.message}{captured}\n"
                "建议：对可能失败的子调用用 try/except ToolCallError 包裹"
                "（e.tool_name / e.message 可读），自行决定跳过、重试或提前 return"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logs = buffer.getvalue()
            captured = f"\nCaptured output:\n{logs}" if logs.strip() else ""
            return ToolResult.error(f"程序异常（{type(exc).__name__}）: {exc}{captured}。请修复程序后重试")
        logs = buffer.getvalue()

        parts: list[str] = []
        if logs.strip():
            parts.append(logs.rstrip())
        if result is not None:
            parts.append(result if isinstance(result, str) else repr(result))
        output = "\n".join(parts) if parts else "(ptc 无输出)"
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n…(输出截断，超过 {_MAX_OUTPUT_CHARS} 字符)"
        return ToolResult.success(
            content=output,
            metadata={"code_mode": True, "description": description},
        )


# 类级短描述：BaseTool 初始化校验 + 尚无 parent 时的兜底（句末不加 。/.）
_PTC_FALLBACK_DESCRIPTION = (
    "用一段 async Python 在单次工具调用里编排多轮子工具，"
    "适合批量、循环、条件与中间聚合，子调用结果不进对话，只有 print/return 的提炼进入上下文，"
    "完整契约与可见工具清单在绑定运行时后注入"
)

# 完整契约：每次读 description / definition 时按当前可见工具集现算（避免挂起工具揭示后清单过期）
_PTC_DESCRIPTION_TEMPLATE = """用一段 async Python 在**一次** ptc 调用里编排多轮子工具

**优先多用本工具**：凡涉及多次工具调用（批量读、循环、条件、聚合、并发扇出），都应包进一次 ptc——子调用的原始结果不进对话历史，只有最终 print+return 的提炼进上下文，能显著节省词元与往返；只有单次简单调用才不必包。

这不是一般脚本沙箱，程序里只能通过 tools.<名>(...) 调本 agent 已可见的工具，每次子调用仍走审批/沙箱/审计
**绝对禁止 import**：写 import 会被静态预检整段拒掉、程序零执行，asyncio/json/math/re/pathlib/datetime 已注入直接用；open/exec/eval 同样被禁，不要原样重试。
子调用的原始结果**不**写入对话历史，只有本工具最终的 print + return 文本进入上下文（省往返与 token）

## 何时用
- 批量只读 多文件 read / 多路 grep / glob 后聚合
- 程序控制流 循环、分支、失败重试、中间变量传递
- 只读并发 asyncio.gather 扇出互不依赖的读调用

## 何时不要用，改用普通逐条工具调用
- 需要人工审批的调用（write/edit/delete/危险 shell、越界写）
- 长时间执行的任务（构建、测试、安装、大批量处理、生成媒体）
- 要把大段原始工具输出展示给用户 先在程序内提炼 或直接调该工具
- 单次简单调用 不必包一层 ptc

## code 写法
- 提交**函数体**（可直接 await / return） 不要写 async def 入口包装
- 调工具 `await tools.<名>(参数=...)` 返回值恒为 **str**（路径列表、匹配文本等） 结构化请自行 splitlines/json/正则
- 失败抛 ToolCallError（toolName / message） 用 try/except 处理
- 写类（write/edit/delete/shell 等）自动串行 只读可 gather 并发
- 已注入 asyncio / json / math / re / pathlib / datetime 直接用 **禁止 import**（写 import 会被静态预检整段拒掉、程序零执行） 禁止 open/exec/eval 不可再调 tools.ptc
- import / open / exec / eval 会被提交时静态拦截（带行号） 不要原样重试
- 输出 print 与/或 return 超时 {max_seconds}s 最终输出上限 {max_chars} 字符

## code 示例
    contents = await asyncio.gather(*(tools.read(path=p) for p in paths))
    return {{"file_count": len(paths), "total_lines": sum(len(c.splitlines()) for c in contents)}}

当前可调工具为 {tools}"""


def build_ptc_description(visible_tools: list[str]) -> str:
    """按当前 agent 可见工具集构建 ptc 描述"""
    names = sorted(n for n in visible_tools if n != PTC_NAME)
    tools_text = "、".join(names) if names else "（无）"
    return _PTC_DESCRIPTION_TEMPLATE.format(
        max_seconds=_MAX_PROGRAM_SECONDS,
        max_chars=_MAX_OUTPUT_CHARS,
        tools=tools_text,
    )


class PtcTool(WorkspaceBoundTool):
    name = "ptc"
    # description 由下方 property 提供（活清单）；此处不设类属性以免与 property 打架
    display_name = "ptc"
    category = "code"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "要执行的 async 函数体（不是完整文件、不要外包 async def），"
                    "体内可直接 await tools.xxx(...) 与 return，"
                    "子工具返回 str，用 print/return 产出最终给对话的文本"
                ),
            },
            "description": {
                "type": "string",
                "description": "5–10 个词的程序摘要，供 CLI/活动树展示，不是给程序用的参数",
            },
        },
        "required": ["code", "description"],
    }

    def __init__(self, workspace_root: Path | None = None, parent_coara: Any | None = None):
        # 先挂 parent，再走 BaseTool 校验，使 description 属性可立刻用活清单
        self._parent_coara = parent_coara
        super().__init__(workspace_root=workspace_root)

    @property
    def description(self) -> str:  # type: ignore[override]
        """LLM 可见描述：按当前可见工具现算，挂起工具揭示后清单自动更新"""
        coara = self._parent_coara
        if coara is None:
            return _PTC_FALLBACK_DESCRIPTION
        try:
            is_owner = bool(getattr(coara.identity, "is_owner_context", False))
            visible = coara._tool_manager.get_visible_tool_names(is_owner)
            return build_ptc_description(visible)
        except Exception:
            return _PTC_FALLBACK_DESCRIPTION

    def create_invocation(self, params: dict[str, Any]) -> PtcToolInvocation:
        return PtcToolInvocation(params, workspace_root=self._workspace_root, parent_coara=self._parent_coara)

    invocation_class = PtcToolInvocation

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        # 内部自己管超时（_MAX_PROGRAM_SECONDS），不套用执行器默认超时
        return None
