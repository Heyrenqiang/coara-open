"""磁盘工具包（dynamic tools）— 可插拔、可自创建的工具形态。

一个工具包 = 一个目录：

    my_tool/
    ├── TOOL.md   # YAML 头：name / description / parameters（可选 entry，默认 main.py）；
    │             # 正文可选，activate 后并入工具描述尾部
    └── main.py   # 入口：def run(**params)（可 async；返回 str 或 dict/list）

发现路径（对齐 skills）：
- 用户级 ``<coara_home>/users/default/tools/``
- 工作空间级 ``<workspace>/.coara/tools/``

加载后注册进挂起池（should_defer=True），LLM 经 tool(search/activate) 揭示即可调用。
注册在会话初始化与 tool 网关 search/activate 时各扫一次（去重按名字），
会话中新创建的工具包无需 /new 即可被发现。

安全闸：自建工具默认每次调用都需用户审批；``config.yaml`` 的
``tools.auto_approve`` 名单内的工具免审批。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

_TOOL_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,63}")


class DynamicToolInvocation(ToolInvocation):
    """执行工具包入口 run(**params)。"""

    def __init__(self, params: dict[str, Any], package: DynamicPackageTool) -> None:
        super().__init__(params)
        self._package = package

    def get_description(self) -> str:
        return f"{self._package.name}（自建工具包）"

    async def execute(self, signal=None) -> ToolResult:
        try:
            run = self._package.load_run()
            params = dict(self.params)
            if asyncio.iscoroutinefunction(run):
                result = await run(**params)
            else:
                result = await asyncio.to_thread(run, **params)
        except Exception as exc:
            return ToolResult.error(f"工具包 {self._package.name} 执行失败: {exc}")
        if isinstance(result, str):
            return ToolResult.success(result)
        if isinstance(result, (dict, list)):
            return ToolResult.success(json.dumps(result, ensure_ascii=False, indent=2))
        return ToolResult.success(str(result))


class DynamicPackageTool(BaseTool):
    """磁盘工具包的运行时形态（统一挂起，activate 揭示）。"""

    kind = ToolKind.EXECUTE
    category = "dynamic"
    should_defer = True
    owner_only = False

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters_schema: dict[str, Any],
        entry: Path,
    ) -> None:
        self.name = name
        self.description = description
        self.display_name = name
        self.parameters_schema = parameters_schema
        self._entry = entry
        self._run_cache: Any = None
        # 审批闸挂到实例上（tool_policy 读 callable 并传入调用参数）
        self.requires_approval = self._requires_approval
        super().__init__()

    def _requires_approval(self, args: dict[str, Any]) -> bool:
        """自建工具默认每次调用都审批；tools.auto_approve 名单内放行。"""
        try:
            from src.core.config import config_manager

            tools_cfg = getattr(config_manager.config, "tools", None)
            auto = getattr(tools_cfg, "auto_approve", None) or []
            return self.name not in auto
        except Exception:  # noqa: BLE001 — 配置未加载等场景一律从严（要审批）
            return True

    def load_run(self) -> Any:
        """导入入口模块并返回 run 可调用（带缓存；导入失败抛错由调用方转 error）。"""
        if self._run_cache is not None:
            return self._run_cache
        module_name = "coara_dyn_" + hashlib.md5(str(self._entry).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(module_name, self._entry)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载入口文件: {self._entry}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run = getattr(module, "run", None)
        if not callable(run):
            raise RuntimeError(f"{self._entry} 缺少入口函数 run(**params)")
        self._run_cache = run
        return run

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return DynamicToolInvocation(params, self)


def load_tool_package(package_dir: Path) -> DynamicPackageTool | None:
    """加载一个工具包目录；不合法时记 warning 并返回 None（不拖垮整个扫描）。"""
    tool_md = package_dir / "TOOL.md"
    if not tool_md.is_file():
        return None
    try:
        import frontmatter

        post = frontmatter.load(str(tool_md))
    except Exception as exc:
        logger.warning(f"工具包 {package_dir.name} 的 TOOL.md 解析失败: {exc}")
        return None

    name = str(post.get("name") or "").strip()
    if not _TOOL_NAME_RE.fullmatch(name):
        logger.warning(f"工具包 {package_dir.name} 的 name 非法（需字母开头的标识符）: {name!r}")
        return None
    description = str(post.get("description") or "").strip()
    if not description:
        logger.warning(f"工具包 {name} 缺少 description")
        return None
    body = (post.content or "").strip()
    if body:
        description = f"{description}\n\n{body}"

    parameters = post.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, dict) or parameters.get("type", "object") != "object":
        logger.warning(f"工具包 {name} 的 parameters 需为 object 型 JSON Schema")
        return None
    parameters.setdefault("type", "object")
    parameters.setdefault("properties", {})

    entry = package_dir / str(post.get("entry") or "main.py")
    if not entry.is_file():
        logger.warning(f"工具包 {name} 缺少入口文件: {entry.name}")
        return None

    tool = DynamicPackageTool(
        name=name,
        description=description,
        parameters_schema=parameters,
        entry=entry,
    )
    try:
        tool.load_run()  # 加载期预检：语法错误/缺 run 早发现
    except Exception as exc:
        logger.warning(f"工具包 {name} 入口预检失败: {exc}")
        return None
    return tool


def discover_tool_package_dirs(workspace_dir: Path | None, coara_home: Path | None) -> list[Path]:
    """列出工具包目录：用户级 + 工作空间级（存在才扫）。"""
    roots: list[Path] = []
    if coara_home is not None:
        from src.core.coara_home import user_paths

        roots.append(user_paths(Path(coara_home)).tools_dir)
    if workspace_dir is not None:
        roots.append(Path(workspace_dir) / ".coara" / "tools")
    dirs: list[Path] = []
    for root in roots:
        try:
            if root.is_dir():
                dirs.extend(d for d in sorted(root.iterdir()) if d.is_dir() and (d / "TOOL.md").is_file())
        except OSError:
            continue
    return dirs


def register_dynamic_tools(coara: Any) -> int:
    """扫描并把工具包注册进 coara 的挂起池；返回新注册数量（幂等，按名字去重）。

    会话初始化与 tool 网关 search/activate 时各调一次——会话中新建的
    工具包下一次 search 即可见。
    """
    workspace_dir = getattr(coara, "workspace_dir", None)
    try:
        from src.core.coara_home import resolve_coara_home

        coara_home = resolve_coara_home(workspace_dir) if workspace_dir else None
    except Exception:  # noqa: BLE001
        coara_home = None
    registered = 0
    existing = coara._tool_manager.tools
    for package_dir in discover_tool_package_dirs(Path(workspace_dir) if workspace_dir else None, coara_home):
        tool = load_tool_package(package_dir)
        if tool is None or tool.name in existing:
            continue
        coara.register_tool(tool)
        registered += 1
    return registered
