"""Text search tool — ripgrep when available, Python fallback otherwise."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from src.core.subprocess_cleanup import terminate_subprocess_tree
from src.core.tool_base import ToolKind, ToolResult
from src.tools.builtin.file_io.search_support import (
    DEFAULT_WALK_MAX_DEPTH,
    GREP_TRUNCATION_HINT,
    MAX_GREP_FILE_BYTES,
    WALK_TRUNCATION_HINT,
    GrepParams,
    WalkStatus,
    append_truncation_footer,
    compile_grep_pattern,
    find_rg_executable,
    iter_budgeted_walk,
    path_matches_glob,
    relocate_missing_search_path,
    resolve_rg_type,
    resolve_workspace_search_root,
    validate_search_path,
)
from src.tools.builtin.file_io.workspace_tool_base import WorkspaceBoundTool, WorkspaceBoundToolInvocation
from src.utils.win_proc import no_window_creationflags


class GrepToolInvocation(WorkspaceBoundToolInvocation):
    """Invocation for text search."""

    def __init__(
        self,
        params: dict[str, Any],
        workspace_root: Path | None = None,
        vfs_resolver: Any | None = None,
    ):
        super().__init__(params, workspace_root)
        self._vfs_resolver = vfs_resolver
        self._grep = GrepParams.from_tool_params(params, workspace_root=self._workspace_root)

    def get_description(self) -> str:
        return f"Grep {self._grep.pattern} @ {self._grep.root}"

    async def execute(self, signal=None) -> ToolResult:
        root, path_error = resolve_workspace_search_root(
            self._grep.root,
            self._workspace_root,
            "Grep",
            vfs_resolver=self._vfs_resolver,
        )
        if path_error:
            return ToolResult.error(path_error)
        assert root is not None

        search_error = validate_search_path(root)
        relocated_note: str | None = None
        if search_error:
            # 自我纠偏：路径不存在时按末级名字在 workspace 内定位，
            # 唯一候选直接重试；多候选列出供选择（省一轮人工 glob）
            candidates = relocate_missing_search_path(root, self._workspace_root)
            if len(candidates) == 1:
                relocated_note = f"原路径不存在（{root}），已按名字自动定位到 {candidates[0]}"
                root = candidates[0]
                search_error = None
            elif candidates:
                listing = "\n".join(str(c) for c in candidates)
                return ToolResult.error(f"{search_error}\n按名字 {root.name} 在 workspace 找到这些候选：\n{listing}")
            else:
                return ToolResult.error(search_error)

        search_root, rg_target = self._resolve_search_scope(root)

        rg = find_rg_executable()
        if rg is not None:
            try:
                return await self._execute_rg(rg, search_root, rg_target, engine_note=relocated_note)
            except Exception as exc:
                return await self._execute_python(
                    search_root,
                    rg_target,
                    engine_note=f"ripgrep 失败（{exc}），已用 Python 回退"
                    + (f"；{relocated_note}" if relocated_note else ""),
                )

        return await self._execute_python(
            search_root,
            rg_target,
            engine_note="；".join(n for n in (relocated_note, "未找到 ripgrep，已用 Python 回退") if n),
        )

    @staticmethod
    def _resolve_search_scope(resolved: Path) -> tuple[Path, str]:
        """Return (search_root, ripgrep path target)."""
        if resolved.is_file():
            return resolved.parent, resolved.name
        return resolved, "."

    @staticmethod
    def _resolve_effective_glob(g: GrepParams) -> str | None:
        _rg_type, type_glob = resolve_rg_type(g.file_type)
        effective_glob = g.glob
        if type_glob and (not effective_glob or effective_glob == "**/*"):
            effective_glob = type_glob
        return effective_glob

    @staticmethod
    def _auto_fix_pattern(pattern: str) -> str:
        """自动修复常见正则错误（降级重试用）：不成对括号与字面 `{`/`}` 转义为字面量。

        只在原 pattern 编译失败后调用，把最可能造成解析错误的字符按字面量转义，
        让 rg/Python 都能跑起来返回结果，而不是每次报错逼 LLM 再改一轮。
        修复规则写死：字符类内不动；多出的右括号转义；未闭合的左括号转义；
        `{`/`}` 一律转义为字面量（ripgrep 最常见报错就是字面 { 未转义）。
        """
        if not pattern:
            return pattern
        out: list[str] = []
        i = 0
        n = len(pattern)
        escaped = False
        in_class = False
        open_parens: list[int] = []
        while i < n:
            ch = pattern[i]
            if escaped:
                out.append(ch)
                escaped = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                i += 1
                continue
            if ch == "[" and not in_class:
                in_class = True
                out.append(ch)
                i += 1
                continue
            if ch == "]" and in_class:
                in_class = False
                out.append(ch)
                i += 1
                continue
            if in_class:
                out.append(ch)
                i += 1
                continue
            if ch == "(":
                open_parens.append(len(out))
                out.append(ch)
                i += 1
                continue
            if ch == ")":
                if open_parens:
                    open_parens.pop()
                    out.append(ch)
                else:
                    # 不配对的右括号 → 转义为字面量
                    out.append("\\")
                    out.append(ch)
                i += 1
                continue
            if ch in "{}":
                # 字面 { / }：Rust 正则要求转义，是 ripgrep 最常见的 parse error 来源
                out.append("\\")
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        # 收尾：未闭合的左括号 → 转义为字面量
        for idx in reversed(open_parens):
            if idx < len(out) and out[idx] == "(":
                out[idx] = "\\("
        return "".join(out)

    def _build_rg_cmd(self, rg: str, root: Path, rg_target: str, pattern: str) -> list[str]:
        g = self._grep
        cmd = [rg, "--color=never", "--no-heading"]

        if g.output_mode == "content":
            cmd.append("--line-number")
        elif g.output_mode == "files_with_matches":
            cmd.append("-l")
        elif g.output_mode == "count":
            cmd.append("-c")

        if not g.case_sensitive:
            cmd.append("-i")
        if g.multiline:
            cmd.extend(["-U", "--multiline-dotall"])
        if g.context_lines > 0:
            cmd.extend(["-C", str(g.context_lines)])

        rg_type, _ = resolve_rg_type(g.file_type)
        if rg_type:
            cmd.extend(["-t", rg_type])

        effective_glob = self._resolve_effective_glob(g)
        if effective_glob and effective_glob not in ("**/*", "*"):
            cmd.extend(["-g", effective_glob])

        # 与 Python 回退的 WalkBudget.max_depth 对齐，避免 rg 在超深树无限下钻
        cmd.extend(["--max-depth", str(DEFAULT_WALK_MAX_DEPTH)])

        # 传绝对路径目标，rg 输出即绝对路径，可直接传给 read/edit
        abs_target = str(root) if rg_target == "." else str(root / rg_target)
        cmd.extend(["-e", pattern, abs_target])
        return cmd

    async def _run_rg_once(self, cmd: list[str], root: Path) -> tuple[list[str], bool, str | None]:
        """执行一次 ripgrep，返回 (raw_lines, truncated, error)。"""
        g = self._grep
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=no_window_creationflags(),
        )

        offset = g.limits.offset
        limit = g.limits.max_results
        max_read = offset + limit + 1
        raw_lines: list[str] = []
        truncated = False

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    raw_lines.append(line_str)
                    if len(raw_lines) >= max_read:
                        truncated = True
                        break
        except asyncio.CancelledError:
            # 工具被取消（用户打断/批次中止）时同步终止 rg 子进程，不留孤儿进程
            await terminate_subprocess_tree(proc)
            raise

        if truncated:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                proc.kill()
                # kill 后必须 reap，否则残留僵尸/未回收的传输句柄
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            return raw_lines, True, None

        await proc.wait()
        if proc.returncode not in (0, 1):
            stderr_data = await proc.stderr.read()
            message = stderr_data.decode("utf-8", errors="replace").strip() or "ripgrep 执行失败"
            return raw_lines, False, message
        return raw_lines, False, None

    async def _execute_rg(
        self,
        rg: str,
        root: Path,
        rg_target: str = ".",
        *,
        engine_note: str | None = None,
    ) -> ToolResult:
        g = self._grep
        original_cmd = self._build_rg_cmd(rg, root, rg_target, g.pattern)
        raw_lines, truncated, error = await self._run_rg_once(original_cmd, root)

        if error is not None:
            # 正则解析失败：自动修复后重试一次（转义不成对括号与字面量），
            # 避免每次方言错误都报错、逼 LLM 多花一轮改 pattern
            if "regex parse error" in error:
                fixed = self._auto_fix_pattern(g.pattern)
                if fixed != g.pattern:
                    retry_cmd = self._build_rg_cmd(rg, root, rg_target, fixed)
                    raw_lines, truncated, retry_error = await self._run_rg_once(retry_cmd, root)
                    if retry_error is None:
                        note = f"原正则 {g.pattern!r} 解析失败，已自动转义后重试"
                        if engine_note:
                            note = f"{engine_note}；{note}"
                        return self._format_results(
                            raw_lines,
                            root,
                            engine="ripgrep",
                            skipped_large=[],
                            truncated=truncated,
                            engine_note=note,
                        )
                # 修复后仍失败：回退 Python 引擎（语法更宽松，通常能编译）
                fallback_note = (
                    f"ripgrep 正则解析失败（原 {g.pattern!r}"
                    + (f" → 尝试 {fixed!r}" if fixed != g.pattern else "")
                    + "），已用 Python 回退"
                )
                if engine_note:
                    fallback_note = f"{engine_note}；{fallback_note}"
                return await self._execute_python(
                    root,
                    rg_target,
                    engine_note=fallback_note,
                )
            hint = ""
            if "literal" in error:
                hint = "（Rust 正则：字面 { 需转义 \\{）"
            return ToolResult.error(f"ripgrep 执行失败: {error}{hint}")

        return self._format_results(
            raw_lines,
            root,
            engine="ripgrep",
            skipped_large=[],
            truncated=truncated,
            engine_note=engine_note,
        )

    async def _execute_python(
        self,
        root: Path,
        rg_target: str = ".",
        *,
        engine_note: str | None = None,
    ) -> ToolResult:
        g = self._grep
        pattern = None
        try:
            pattern = compile_grep_pattern(
                g.pattern,
                case_sensitive=g.case_sensitive,
                multiline=g.multiline,
            )
        except re.error as exc:
            # 正则无效：自动修复（转义不成对括号与字面量）后重编译，
            # 仍失败才报错——避免每次语法错误都逼 LLM 再改一轮
            fixed = self._auto_fix_pattern(g.pattern)
            try:
                pattern = compile_grep_pattern(
                    fixed,
                    case_sensitive=g.case_sensitive,
                    multiline=g.multiline,
                )
                engine_note = (
                    engine_note + "；" if engine_note else ""
                ) + f"原正则 {g.pattern!r} 无效，已自动转义后重试"
            except re.error:
                return ToolResult.error(f"无效的正则表达式: {exc}")

        raw_lines, truncated, skipped_large, walk_truncated = await asyncio.to_thread(
            self._run_python_search, root, rg_target, pattern
        )

        return self._format_results(
            raw_lines,
            root,
            engine="python",
            engine_note=engine_note,
            skipped_large=skipped_large,
            truncated=truncated,
            walk_truncated=walk_truncated,
        )

    def _run_python_search(
        self,
        root: Path,
        rg_target: str,
        pattern: re.Pattern[str],
    ) -> tuple[list[str], bool, list[str], bool]:
        """Synchronous Python-fallback search (budgeted walk + file reads); run via asyncio.to_thread."""
        g = self._grep
        effective_glob = self._resolve_effective_glob(g)
        skipped_large: list[str] = []
        only_file = root / rg_target if rg_target != "." else None
        walk_status = WalkStatus()

        offset = g.limits.offset
        limit = g.limits.max_results
        max_read = offset + limit + 1
        raw_lines: list[str] = []
        truncated = False

        for path in self._iter_search_files(
            root, effective_glob, skipped_large, only_file=only_file, walk_status=walk_status
        ):
            disp = path.as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            if g.output_mode == "files_with_matches":
                if pattern.search(text):
                    raw_lines.append(disp)
                    if len(raw_lines) >= max_read:
                        truncated = True
                        break
            elif g.output_mode == "count":
                if g.multiline:
                    count = 1 if pattern.search(text) else 0
                else:
                    count = sum(1 for line in text.splitlines() if pattern.search(line))
                if count:
                    raw_lines.append(f"{disp}:{count}")
                    if len(raw_lines) >= max_read:
                        truncated = True
                        break
            else:
                if g.multiline:
                    if pattern.search(text):
                        raw_lines.append(f"{disp}:1: {text[:500].strip()}")
                        if len(raw_lines) >= max_read:
                            truncated = True
                            break
                    continue

                lines = text.splitlines()
                matched_lines = [i for i, line in enumerate(lines, start=1) if pattern.search(line)]
                if not matched_lines:
                    continue

                context = g.context_lines
                if context > 0:
                    matched_set = set(matched_lines)
                    printed: set[int] = set()
                    for line_no in matched_lines:
                        start = max(1, line_no - context)
                        end = min(len(lines), line_no + context)
                        for ctx_no in range(start, end + 1):
                            if ctx_no in printed:
                                continue
                            printed.add(ctx_no)
                            if ctx_no in matched_set:
                                raw_lines.append(f"{disp}:{ctx_no}: {lines[ctx_no - 1].strip()}")
                            else:
                                raw_lines.append(f"{disp}-{ctx_no}- {lines[ctx_no - 1].strip()}")
                            if len(raw_lines) >= max_read:
                                truncated = True
                                break
                        if truncated:
                            break
                else:
                    for line_no in matched_lines:
                        raw_lines.append(f"{disp}:{line_no}: {lines[line_no - 1].strip()}")
                        if len(raw_lines) >= max_read:
                            truncated = True
                            break

                if truncated:
                    break

        return raw_lines, truncated, skipped_large, walk_status.truncated

    def _iter_search_files(
        self,
        root: Path,
        glob_pattern: str | None,
        skipped_large: list[str],
        *,
        only_file: Path | None = None,
        walk_status: WalkStatus | None = None,
    ):
        if only_file is not None:
            if only_file.is_file():
                try:
                    size = only_file.stat().st_size
                except OSError:
                    return
                if size > MAX_GREP_FILE_BYTES:
                    skipped_large.append(only_file.as_posix())
                    return
                yield only_file
            return

        status = walk_status if walk_status is not None else WalkStatus()
        for current_root, _dirnames, filenames in iter_budgeted_walk(root, status=status):
            for filename in filenames:
                path = Path(current_root) / filename
                rel = path.relative_to(root).as_posix()
                if not path_matches_glob(rel, glob_pattern):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > MAX_GREP_FILE_BYTES:
                    skipped_large.append(path.as_posix())
                    continue
                yield path

    def _format_results(
        self,
        raw_lines: list[str],
        root: Path,
        *,
        engine: str,
        skipped_large: list[str],
        truncated: bool,
        engine_note: str | None = None,
        walk_truncated: bool = False,
    ) -> ToolResult:
        g = self._grep
        offset = g.limits.offset
        limit = g.limits.max_results

        if not truncated and len(raw_lines) > offset + limit + 1:
            raw_lines = raw_lines[: offset + limit + 1]
            truncated = True

        shown = raw_lines[offset : offset + limit]
        any_truncated = truncated or walk_truncated

        if not shown:
            content = "未找到匹配"
            if skipped_large:
                content += f"\n已跳过 {len(skipped_large)} 个超过 {MAX_GREP_FILE_BYTES} 字节的文件"
            if walk_truncated:
                content += f"\n{WALK_TRUNCATION_HINT}"
            metadata: dict[str, Any] = {
                "root": str(root),
                "count": 0,
                "engine": engine,
                "output_mode": g.output_mode,
                "truncated": bool(walk_truncated),
                "walk_truncated": walk_truncated,
            }
            if skipped_large:
                metadata["skipped_large_files"] = skipped_large[:20]
            if engine_note:
                metadata["note"] = engine_note
            return ToolResult.success(content, metadata=metadata)

        lines = list(shown)
        if any_truncated:
            hint = WALK_TRUNCATION_HINT if walk_truncated and not truncated else GREP_TRUNCATION_HINT
            append_truncation_footer(
                lines,
                shown=len(shown),
                total=None,
                offset=offset,
                hint=hint,
            )

        metadata = {
            "root": str(root),
            "count": len(shown),
            "engine": engine,
            "output_mode": g.output_mode,
            "truncated": any_truncated,
            "walk_truncated": walk_truncated,
            "offset": offset,
        }
        if skipped_large:
            metadata["skipped_large_files"] = skipped_large[:20]
        if engine_note:
            metadata["note"] = engine_note

        return ToolResult.success("\n".join(lines), metadata=metadata)


class GrepTool(WorkspaceBoundTool):
    """Search file contents with a regular expression."""

    name = "grep"
    description = """用 ripgrep 搜索文件内容并返回绝对路径；跳过 .git、node_modules、点目录和大于 2MB 的文件，被跳过者见结果 metadata 的 skipped_large_files。优先 files_with_matches 定位后再 read；仅需上下文时用 content。结果截断时收窄 pattern 或用 offset。字面 { 需转义；跨行匹配用 multiline=true"""  # noqa: E501
    display_name = "GrepFiles"
    category = "filesystem"
    kind = ToolKind.SEARCH
    invocation_class = GrepToolInvocation
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    r"Rust 正则；字面 { 要写 \{；不可含换行，跨行时用 multiline=true"
                ),
            },
            "path": {
                "type": "string",
                "description": "目录或文件绝对路径；省略时搜索整个工作区",
            },
            "glob": {
                "type": "string",
                "description": "文件名过滤，如 *.py、*.{ts,tsx}；与 type 二选一",
            },
            "type": {
                "type": "string",
                "description": "文件类型，如 py、ts、md；与 glob 二选一",
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": (
                    "files_with_matches=仅路径；content=匹配行；count=命中数"
                ),
                "default": "files_with_matches",
            },
            "context_lines": {
                "type": "integer",
                "description": "content 模式下显示匹配行前后各 N 行",
                "default": 0,
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "是否区分大小写",
                "default": False,
            },
            "multiline": {
                "type": "boolean",
                "description": "允许跨行正则",
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "最大结果数，上限 500",
                "default": 50,
            },
            "offset": {
                "type": "integer",
                "description": "跳过前 N 条结果，用于继续查看",
                "default": 0,
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace_root: Path | None = None, vfs_resolver: Any | None = None):
        super().__init__(workspace_root)
        self._vfs_resolver = vfs_resolver

    def get_invocation_args(self) -> tuple[Any, ...]:
        return (self._workspace_root, self._vfs_resolver)
