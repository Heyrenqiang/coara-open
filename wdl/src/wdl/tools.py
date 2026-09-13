"""wdl 内置最小工具集 — 节点 agentic 工具循环用的本机工具。

工具：read_file / write_file / edit_file / shell / list_files / grep。
全部以工作目录为根做路径约束（拒绝对根外写入/读取/执行），
shell 设超时（默认 120s）。schema 用标准 JSON Schema，openai 与
anthropic 两类协议共用。

定位是开发者本机工具：**无审批门、无沙箱**——shell 可以 cd 到根外执行，
路径约束只管文件类工具。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SHELL_TIMEOUT = 120.0

_READ_MAX_CHARS = 100_000
_LIST_MAX_ENTRIES = 2000
_GREP_MAX_MATCHES = 200
_SHELL_OUTPUT_MAX_CHARS = 50_000

# 工具名 → JSON Schema（openai function.parameters / anthropic input_schema 共用）
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
        },
        "required": ["path"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "content": {"type": "string", "description": "完整文件内容（覆盖写入）"},
        },
        "required": ["path", "content"],
    },
    "edit_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的确切原文"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    "shell": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要在工作目录下执行的命令"},
        },
        "required": ["command"],
    },
    "list_files": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作目录的目录路径（省略为根）"},
            "recursive": {"type": "boolean", "description": "是否递归（默认 false）"},
        },
        "required": [],
    },
    "grep": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "相对工作目录的文件或目录路径（省略为全工作目录）"},
        },
        "required": ["pattern"],
    },
}

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class ToolSpec:
    """一个内置工具：名字 + JSON Schema + 异步处理器。"""

    name: str
    schema: dict[str, Any]
    handler: ToolHandler


class ToolError(Exception):
    """工具执行失败（消息会回注给模型，不抛穿工具循环）。"""


def _resolve_under_root(root: Path, rel: Any, *, field: str = "path") -> Path:
    """把相对路径解析到 root 下；越界（绝对路径/UNC/../逃逸）抛 ToolError。"""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError(f"{field} 必须是非空字符串（相对工作目录）")
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise ToolError(f"路径越出工作目录，已拒绝：{rel!r}")
    return candidate


def _make_handlers(root: Path, shell_timeout: float) -> dict[str, ToolHandler]:
    async def read_file(args: dict[str, Any]) -> str:
        p = _resolve_under_root(root, args.get("path"))
        if not p.is_file():
            raise ToolError(f"文件不存在：{args.get('path')!r}")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
        if len(text) > _READ_MAX_CHARS:
            text = text[:_READ_MAX_CHARS] + f"\n[截断：共 {len(text)} 字符]"
        return text

    async def write_file(args: dict[str, Any]) -> str:
        p = _resolve_under_root(root, args.get("path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        return f"已写入 {p.name}（{len(content)} 字符）"

    async def edit_file(args: dict[str, Any]) -> str:
        p = _resolve_under_root(root, args.get("path"))
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not old:
            raise ToolError("old_string 必须是非空字符串")
        if not isinstance(new, str):
            raise ToolError("new_string 必须是字符串")
        if old == new:
            raise ToolError("old_string 与 new_string 相同，无需替换")
        if not p.is_file():
            raise ToolError(f"文件不存在：{args.get('path')!r}")
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ToolError("未找到 old_string，未做任何修改")
        if count > 1:
            raise ToolError(f"old_string 匹配 {count} 处，请提供更多上下文保证唯一")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"已编辑 {p.name}"

    async def shell(args: dict[str, Any]) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command 必须是非空字符串")
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=shell_timeout)
        except TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:  # 进程恰好已退出
                pass
            raise ToolError(f"shell 超时（{shell_timeout:g}s），已终止") from None
        out = stdout.decode("utf-8", errors="replace")
        if len(out) > _SHELL_OUTPUT_MAX_CHARS:
            out = out[:_SHELL_OUTPUT_MAX_CHARS] + "\n[截断]"
        return f"[exit={proc.returncode}]\n{out}"

    async def list_files(args: dict[str, Any]) -> str:
        base = _resolve_under_root(root, args.get("path") or ".")
        if not base.is_dir():
            raise ToolError(f"目录不存在：{args.get('path') or '.'!r}")
        recursive = bool(args.get("recursive"))
        iterator = base.rglob("*") if recursive else base.glob("*")
        entries: list[str] = []
        for child in sorted(iterator):
            rel = child.relative_to(root).as_posix()
            entries.append(rel + "/" if child.is_dir() else rel)
            if len(entries) >= _LIST_MAX_ENTRIES:
                entries.append(f"[截断：超过 {_LIST_MAX_ENTRIES} 条]")
                break
        return "\n".join(entries) or "[空目录]"

    async def grep(args: dict[str, Any]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern 必须是非空字符串")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"正则非法：{exc}") from exc
        base = _resolve_under_root(root, args.get("path") or ".")
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = sorted(p for p in base.rglob("*") if p.is_file())
        else:
            raise ToolError(f"路径不存在：{args.get('path') or '.'!r}")
        matches: list[str] = []
        for f in files:
            try:
                for lineno, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        matches.append(f"{f.relative_to(root).as_posix()}:{lineno}: {line.strip()[:200]}")
                        if len(matches) >= _GREP_MAX_MATCHES:
                            break
            except OSError:
                continue
            if len(matches) >= _GREP_MAX_MATCHES:
                matches.append(f"[截断：超过 {_GREP_MAX_MATCHES} 条]")
                break
        return "\n".join(matches) or "[无匹配]"

    return {
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "shell": shell,
        "list_files": list_files,
        "grep": grep,
    }


def build_tools(
    root: Path, shell_timeout: float = DEFAULT_SHELL_TIMEOUT, names: list[str] | None = None
) -> list[ToolSpec]:
    """构建工具规格列表。names 为 None = 全量内置；白名单含未知名抛 ToolError。"""
    root = root.resolve()
    handlers = _make_handlers(root, shell_timeout)
    selected = list(TOOL_SCHEMAS) if names is None else list(names)
    specs: list[ToolSpec] = []
    for name in selected:
        if name not in TOOL_SCHEMAS:
            raise ToolError(f"未知工具：{name!r}（内置：{'、'.join(TOOL_SCHEMAS)}）")
        specs.append(ToolSpec(name=name, schema=TOOL_SCHEMAS[name], handler=handlers[name]))
    return specs


async def run_tool(spec: ToolSpec, arguments: dict[str, Any]) -> tuple[str, bool]:
    """执行工具。返回 (结果文本, 是否成功)；ToolError 与意外异常都转成文本回注。"""
    try:
        if not isinstance(arguments, dict):
            arguments = {}
        return await spec.handler(arguments), True
    except ToolError as exc:
        return f"[工具错误] {exc}", False
    except Exception as exc:  # noqa: BLE001 — 工具异常回注模型，不炸穿循环
        return f"[工具异常] {type(exc).__name__}: {exc}", False
