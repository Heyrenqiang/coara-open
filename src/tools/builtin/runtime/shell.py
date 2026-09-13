"""Shell execution tool — declaration-driven approval, light guards at execute time."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from src.core.subprocess_cleanup import terminate_subprocess_tree
from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.workspace_tool_base import WorkspaceBoundTool, WorkspaceBoundToolInvocation
from src.tools.builtin.runtime.shell_support import (
    decode_subprocess_output,
    is_agnes_video_poll_command,
    shell_lex_split,
)
from src.utils.log_noise import collapse_log_noise
from src.utils.win_proc import no_window_creationflags

_DEFAULT_TIMEOUT_SECONDS = 30
# 前台默认超时（毫秒）。模型预期更长的命令必须显式传 timeout_ms（上限 300s）或 0 转后台。
_MAX_TIMEOUT_SECONDS = 300
_DEFAULT_TIMEOUT_MS = 30_000

# Shell base commands that are highly destructive or irreversible.
# Windows 主力部署平台的真实危险命令：删除/覆盖/提权/下载执行/系统破坏。
# Unix 那套（sudo/mkfs/dd）在 Windows 上几乎不命中——补上 Windows 侧基名。
_EXEC_PROMPT_COMMANDS = frozenset(
    {
        "sudo",
        "su",
        "mkfs",
        "format",
        "fdisk",
        "parted",
        "dd",
        "mkswap",
        # Windows 删除/覆盖
        "rd",
        "rmdir",
        "del",
        "erase",
        "remove-item",
        # Windows 提权/系统操作
        "runas",
        "powershell",
        "pwsh",
        "cmd",
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        # Windows 下载执行/远程脚本
        "certutil",
        "bitsadmin",
        "mshta",
        "rundll32",
        "regsvr32",
        "wscript",
        "cscript",
        "msiexec",
        "installutil",
        "regasm",
        "regsvcs",
        "msbuild",
        "dotnet",
        # Windows 系统破坏/账户操作
        "vssadmin",
        "bcdedit",
        "reg",
        "net",
        "net1",
        "sc",
        "taskkill",
        "shutdown",
        "restart-computer",
        "stop-computer",
    }
)

# 整串危险模式（下载执行 / 远程脚本直跑），命中即需审批。
_DANGEROUS_PATTERNS = (
    re.compile(r"\b(curl|wget)\b[^|]*\|\s*(bash|sh|zsh|python|python3|perl|ruby|node|php)\b", re.IGNORECASE),
    re.compile(r"\b(iex|invoke-expression)\b", re.IGNORECASE),
    re.compile(r"\b(iwr|irm|invoke-webrequest)\b[^|]*\|\s*(iex|invoke-expression|powershell|pwsh)\b", re.IGNORECASE),
)

# 管道/链式分隔符：分段后逐段检查首 token（堵 pipe-downstream 绕过）。
_SEGMENT_SPLIT = re.compile(r"\|\||&&|\||;")


def _is_dangerous_shell(command: str) -> bool:
    """Return True if the command is destructive or downloads-and-executes remote content.

    Checks the leading command's base (sudo/mkfs/dd…) AND every pipe/chain
    segment's leading token (so ``curl … | bash`` / ``… && sudo …`` cannot hide a
    destructive or interpreter command downstream), plus whole-command
    download-execute patterns (``curl | bash``、PowerShell ``iex``）。
    """
    if not command:
        return False
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True
    for segment in _SEGMENT_SPLIT.split(command):
        parts = shell_lex_split(segment.strip())
        if not parts:
            continue
        base = parts[0].lower()
        if base in _EXEC_PROMPT_COMMANDS:
            return True
        # 解释器仅在「前面接远程下载器」时危险（curl|bash），已由 _DANGEROUS_PATTERNS
        # 覆盖；孤立的管道下游解释器（grep … | python -c …）是正常用法，不报。
    return False


def _extract_python_c_block(command: str) -> tuple[str, int, int] | None:
    match = re.search(r"(?i)\b(python(?:3)?|py)\s+-c\s+", command)
    if not match:
        return None
    pos = match.end()
    while pos < len(command) and command[pos] in " \t\r\n":
        pos += 1
    if pos >= len(command):
        return None
    quote = command[pos]
    if quote not in {"'", '"'}:
        return None
    pos += 1
    parts: list[str] = []
    while pos < len(command):
        ch = command[pos]
        if quote == '"':
            if ch == '"' and pos + 1 < len(command) and command[pos + 1] == '"':
                parts.append('"')
                pos += 2
                continue
            if ch == '"':
                return "".join(parts), match.start(), pos + 1
        elif ch == "'":
            return "".join(parts), match.start(), pos + 1
        parts.append(ch)
        pos += 1
    return None


def _materialize_multiline_python(
    command: str,
    *,
    workspace_root: Path | None,
    cwd: str | None,
) -> tuple[str, Path | None]:
    """On Windows, rewrite multiline ``python -c`` into a temp script for reliable execution."""
    if os.name != "nt" or "\n" not in command:
        return command, None
    extracted = _extract_python_c_block(command)
    if extracted is None:
        return command, None
    code, block_start, block_end = extracted
    if "\n" not in code or not code.strip():
        return command, None

    script_dir: Path | None = None
    if cwd:
        script_dir = Path(cwd)
    elif workspace_root is not None:
        script_dir = workspace_root

    if script_dir is not None:
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / f".coara_shell_{uuid.uuid4().hex}.py"
    else:
        script_path = Path(os.environ.get("TEMP", ".")) / f"coara_shell_{uuid.uuid4().hex}.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)

    script_path.write_text(code, encoding="utf-8")
    exe_match = re.search(r"(?i)\b(python(?:3)?|py)\s+-c\s+", command[block_start:block_end])
    exe = exe_match.group(1) if exe_match else "python"
    replacement = f'{exe} "{script_path}"'
    return command[:block_start] + replacement + command[block_end:], script_path


def _normalize_shell_timeout(raw: Any) -> int:
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SECONDS
    return max(1, min(timeout, _MAX_TIMEOUT_SECONDS))


def _resolve_shell_wait(params: dict[str, Any]) -> tuple[bool, int]:
    try:
        timeout_ms = int(params.get("timeout_ms", _DEFAULT_TIMEOUT_MS))
    except (TypeError, ValueError):
        timeout_ms = _DEFAULT_TIMEOUT_MS
    if timeout_ms == 0:
        # 显式后台：立即返回；timeout 秒数仅作占位（create_task 处 bg_timeout=None，
        # 后台兜底由 BashBackgroundRunner 的 background_task_timeout_seconds 承担）
        return True, _DEFAULT_TIMEOUT_MS // 1000
    return False, _normalize_shell_timeout(timeout_ms / 1000)


class ShellToolInvocation(WorkspaceBoundToolInvocation):
    def __init__(
        self,
        params: dict[str, Any],
        workspace_root: Path | None = None,
        parent_coara: Any | None = None,
    ):
        super().__init__(params, workspace_root)
        if "command" not in params:
            raise ValueError("缺少必填参数: command")
        self.command = params["command"]
        self.intent = str(params.get("description") or "").strip()
        self.background, self.timeout = _resolve_shell_wait(params)
        self.cwd = params.get("working_directory")
        self.notify_on_output = params.get("notify_on_output")
        self._parent_coara = parent_coara

    def get_description(self) -> str:
        if self.intent:
            return f"Shell: {self.intent}"
        return f"Shell: {self.command[:80]}"

    async def execute(self, signal=None) -> ToolResult:
        workspace_root = self._workspace_root

        from src.vault.guard import is_under_vault_open, vault_path_denied, vault_text_mentions_denied

        denied_cmd = vault_text_mentions_denied(self.command)
        if denied_cmd:
            return ToolResult.error(denied_cmd)

        exec_command = self.command

        if self.cwd is not None:
            cwd_path = Path(self.cwd).expanduser().resolve()
            denied_cwd = vault_path_denied(cwd_path)
            if denied_cwd:
                return ToolResult.error(denied_cwd)
            under_workspace = False
            try:
                cwd_path.relative_to(workspace_root)
                under_workspace = True
            except ValueError:
                under_workspace = False
            if not under_workspace and not is_under_vault_open(cwd_path):
                return ToolResult.error(f"工作目录不在 workspace 内: {cwd_path}")
            cwd = str(cwd_path)
        else:
            # Default to the bound workspace — never inherit process cwd.
            # Foreground ``os.chdir`` on workspace switch must not redirect
            # an in-flight turn that continues in the background.
            cwd = str(workspace_root) if workspace_root is not None else None

        auto_background_agnes_poll = not self.background and is_agnes_video_poll_command(exec_command)
        if auto_background_agnes_poll:
            self.background = True

        if self.background:
            from src.background.bash_runner import BashBackgroundRunner
            from src.coara.turn_source import current_turn_source

            description = self.command[:120] or "background shell"
            coara_id = getattr(getattr(self._parent_coara, "identity", None), "coara_id", None)
            session_id = getattr(self._parent_coara, "session_id", None)
            wm = getattr(self._parent_coara, "workspace_manager", None)
            coara_home = getattr(wm, "coara_home", None) if wm else None
            # 后台任务默认 2 小时兜底（background_task_timeout_seconds 可配，0 关闭）；
            # 更长的 bash 后台任务无 LLM 级 list/stop；视频取消用 media(action=cancel)
            bg_timeout = None
            task_id = await BashBackgroundRunner().create_task(
                exec_command,
                description,
                workspace_dir=workspace_root,
                timeout=bg_timeout,
                cwd=cwd,
                output_watch=self.notify_on_output,
                coara_id=coara_id,
                session_id=session_id,
                coara_home=coara_home,
                origin_source=current_turn_source(self._parent_coara),
            )
            lines = [f"后台任务已启动: {task_id}（完成后注入结果）"]
            if auto_background_agnes_poll:
                lines.append("（Agnes 视频生成已自动转入后台）")
            if self.notify_on_output:
                lines.append("已启用输出模式唤醒 (notify_on_output)")
            return ToolResult.success(
                content="\n".join(lines),
                metadata={"task_id": task_id, "background": True},
            )

        env = dict(os.environ)
        if getattr(self, "_trust_level", "owner") == "untrusted":
            from src.tools.sandbox import get_sandbox

            env = get_sandbox().sanitize_env(env)

        if os.name == "nt":
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")

        materialized_script: Path | None = None
        shell_command, materialized_script = _materialize_multiline_python(
            exec_command,
            workspace_root=workspace_root,
            cwd=cwd,
        )
        process = None
        stdout = b""
        stderr = b""
        terminated = False
        try:
            if os.name == "nt":
                from src.core.process import build_powershell_exec_cmd

                ps_exec = build_powershell_exec_cmd(shell_command)
                if ps_exec is None:
                    return ToolResult.error("Windows 上未找到 PowerShell（powershell.exe / pwsh.exe）")
                ps_path, ps_args = ps_exec
                process = await asyncio.create_subprocess_exec(
                    ps_path,
                    *ps_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    # 独立进程组（对齐后台任务）：超时/取消杀进程树时 killpg /
                    # taskkill /T 能覆盖整棵进程树，不把孙进程留给孤儿
                    creationflags=no_window_creationflags() | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    shell_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    # 独立会话：killpg 能按进程组杀全树，孙进程不泄漏
                    start_new_session=True,
                )
            # communicate() 不设内部超时：前台等待上限由 executor 外层 wait_for
            # 承担。超时时 wait_for 取消任务 → CancelledError 路径杀进程树，
            # 返回明确超时失败——模型据此决策重试或改用后台方式。
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            # 取消路径（用户打断 / 超时取消）：杀进程树后重抛。用局部
            # terminated 标志记录「已终止」——asyncio Process.returncode
            # 是只读属性，不能赋值标记；finally 靠该标志避免重复击杀并
            # 决定是否清理临时脚本。
            # 超时取消（executor 已标 _coara_timed_out）直接强杀，
            # 用户打断走优雅终止（SIGTERM/taskkill 温和 → grace → 强杀）
            if process is not None and process.returncode is None:
                timed_out = bool(getattr(asyncio.current_task(), "_coara_timed_out", False))
                await terminate_subprocess_tree(process, graceful=not timed_out)
                terminated = True
            raise
        except Exception as exc:
            return ToolResult.error(f"命令执行失败: {exc}")
        finally:
            if (
                signal is not None
                and signal.aborted
                and process is not None
                and process.returncode is None
                and not terminated
            ):
                # 用户主动打断（signal.aborted）：优雅终止给进程收尾机会
                await terminate_subprocess_tree(process, graceful=True)
            if materialized_script is not None and (terminated or process.returncode is not None):
                materialized_script.unlink(missing_ok=True)

        output_parts = []
        if stdout:
            # 注入侧噪音折叠（进度墙/刷屏行），退出码/[stderr] 标记保持原样
            output_parts.append(collapse_log_noise(decode_subprocess_output(stdout)))
        if stderr:
            output_parts.append(f"[stderr]\n{collapse_log_noise(decode_subprocess_output(stderr))}")

        output = "\n".join(output_parts) if output_parts else "(无输出)"
        return_code = process.returncode
        failed = return_code != 0
        if failed:
            output = f"[执行结束，退出码: {return_code}]\n{output}"
        return ToolResult.success(
            content=output,
            metadata={
                "return_code": return_code,
                "failed": failed,
                "command": self.command,
            },
        )


class ShellTool(WorkspaceBoundTool):
    name = "shell"
    description = """使用 PowerShell 执行命令并返回输出。读文件优先用 read，不用 Get-Content/cat；不要用 sleep / Start-Sleep 等待后台任务；多行脚本先落临时文件再运行。递归删除或移动前，必须先确认目标的绝对路径位于预期目录内；使用 PowerShell 原生命令，路径参数用 -LiteralPath。始终将相关操作合并到一条命令中执行，最小化工具调用次数"""
    display_name = "Shell"
    category = "shell"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 PowerShell 命令",
            },
            "description": {
                "type": "string",
                "description": "命令用途短述，5–10 个字",
            },
            "working_directory": {
                "type": "string",
                "description": "工作目录绝对路径；默认工作区根目录",
            },
            "timeout_ms": {
                "type": "number",
                "description": "最长运行毫秒数，上限 300000；0 表示后台执行",
                "default": 30000,
            },
            "notify_on_output": {
                "type": "object",
                "description": "后台输出监控；仅用户明确要求时使用，且需开启 shell_notify",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "匹配 stdout/stderr 的正则表达式",
                    },
                    "reason": {
                        "type": "string",
                        "description": "监控原因（5 字以内）",
                    },
                    "debounce_ms": {
                        "type": "number",
                        "description": "通知防抖时间（毫秒，最小 5000）",
                    },
                },
                "required": ["pattern", "reason"],
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace_root: Path | None = None, parent_coara: Any | None = None):
        super().__init__(workspace_root=workspace_root)
        self._parent_coara = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ShellToolInvocation:
        return ShellToolInvocation(params, workspace_root=self._workspace_root, parent_coara=self._parent_coara)

    invocation_class = ShellToolInvocation

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        """Prompt when the command starts with a destructive base (sudo/mkfs/dd…)."""
        command = args.get("command", "") if isinstance(args, dict) else ""
        return _is_dangerous_shell(str(command))

    def get_execution_timeout(self, default_timeout: float, args: dict[str, Any] | None = None) -> float | None:
        """前台超时声明（executor 统一 enforce）。

        按本次调用的 timeout_ms 声明等待上限（默认 30s，上限 300s）：到点
        executor 取消任务（CancelledError 路径杀进程树）并返回超时失败。
        显式后台（timeout_ms=0）不经此路径，返回 None（创建后台任务本身很快）。
        """
        background, _ = _resolve_shell_wait(args or {})
        if background:
            return None
        try:
            timeout_ms = int(args.get("timeout_ms", _DEFAULT_TIMEOUT_MS) if args else _DEFAULT_TIMEOUT_MS)
        except (TypeError, ValueError):
            timeout_ms = _DEFAULT_TIMEOUT_MS
        return _normalize_shell_timeout(timeout_ms / 1000)
