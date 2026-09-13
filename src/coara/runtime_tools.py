"""Runtime tool registration for Coara instances."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.coara.api_key_audit import available_media_providers, available_search_providers
from src.core.logger import logger
from src.tools.builtin.code_mode.ptc import PtcTool
from src.tools.builtin.delegate.delegate import DelegateTool
from src.tools.builtin.file_io.delete import DeleteTool
from src.tools.builtin.file_io.edit import EditTool
from src.tools.builtin.file_io.glob import GlobTool
from src.tools.builtin.file_io.grep import GrepTool
from src.tools.builtin.file_io.read import ReadTool
from src.tools.builtin.file_io.write import WriteTool
from src.tools.builtin.media import MediaTool
from src.tools.builtin.runtime.shell import ShellTool
from src.tools.builtin.todo.todo import TodoTool
from src.tools.builtin.web import WebSearchTool


def register_runtime_tools(coara: Any) -> None:
    """Register runtime-bound tools for a Coara instance."""
    workspace_root = Path(coara.workspace_dir).resolve()
    vfs_resolver = None
    workspace_manager = getattr(coara, "workspace_manager", None)
    if workspace_manager is not None:
        vfs_resolver = workspace_manager.vfs
    elif getattr(coara, "_delegate_vfs", None) is not None:
        vfs_resolver = coara._delegate_vfs

    coara.register_tool(
        ReadTool(
            read_state_store=coara._file_read_states,
            workspace_root=workspace_root,
            vfs_resolver=vfs_resolver,
            parent_coara=coara,
        ),
        replace=True,
    )
    coara.register_tool(
        WriteTool(
            read_state_store=coara._file_read_states,
            workspace_root=workspace_root,
            vfs_resolver=vfs_resolver,
        ),
        replace=True,
    )
    coara.register_tool(
        EditTool(
            read_state_store=coara._file_read_states,
            workspace_root=workspace_root,
            vfs_resolver=vfs_resolver,
        ),
        replace=True,
    )
    coara.register_tool(
        DeleteTool(
            read_state_store=coara._file_read_states,
            workspace_root=workspace_root,
            vfs_resolver=vfs_resolver,
        ),
        replace=True,
    )
    search_tools = (
        GlobTool(workspace_root=workspace_root, vfs_resolver=vfs_resolver),
        GrepTool(workspace_root=workspace_root, vfs_resolver=vfs_resolver),
    )
    for tool in search_tools:
        coara.register_tool(tool, replace=True)
    coara.register_tool(ShellTool(workspace_root=workspace_root, parent_coara=coara), replace=True)
    # web_search：baidu 免 key 恒可用，工具始终注册；描述列出当前可用的 provider。
    search_tool = WebSearchTool()
    try:
        _search_available = available_search_providers()
        search_tool.description = (
            search_tool.description
            + f"\n\n当前可用搜索 provider：{'、'.join(_search_available)}"
            + "（其余 provider 需在 system/.env 配置对应 API key）。"
        )
    except Exception:
        pass
    coara.register_tool(search_tool, replace=True)
    # media：无 AGNES_API_KEY 则不注册（生图/视频都走它）。
    try:
        _media_available = available_media_providers()
    except Exception:
        _media_available = []
    if _media_available:
        coara.register_tool(
            MediaTool(workspace_root=workspace_root, available_providers=set(_media_available)),
            replace=True,
        )
    else:
        logger.info("media 工具未注册：未配置 AGNES_API_KEY")

    if coara.delegate_depth == 0:
        coara.register_tool(DelegateTool(parent_coara=coara))

    coara.register_tool(TodoTool(parent_coara=coara))

    # 内置挂起工具（屏幕捕捉、打印、邮件）——注册进挂起池，
    # 经 tool(search/activate) 揭示后可用，不常驻工具面
    from src.tools.builtin.email.tools import EMAIL_TOOL_TYPES
    from src.tools.builtin.embody.tools import EmbodyTool
    from src.tools.builtin.printer.tools import PRINTER_TOOL_TYPES
    from src.tools.builtin.screenshot.tools import ScreenshotTool

    coara.register_tool(ScreenshotTool(workspace_root=workspace_root), replace=True)
    coara.register_tool(EmbodyTool(workspace_root=workspace_root), replace=True)
    for tool_type in (*PRINTER_TOOL_TYPES, *EMAIL_TOOL_TYPES):
        coara.register_tool(tool_type(), replace=True)

    # 磁盘工具包（用户级 + 工作空间级自建工具）：注册进挂起池，tool 网关可发现
    try:
        from src.tools.dynamic.loader import register_dynamic_tools

        register_dynamic_tools(coara)
    except Exception as exc:
        logger.warning(f"Dynamic tool package scan failed for {coara.identity.name}: {exc}")

    # ptc 最后注册：description 按当前可见工具现算（property），binding 亦按可见集强制
    coara.register_tool(PtcTool(workspace_root=workspace_root, parent_coara=coara), replace=True)
    registered = list(coara._tool_manager.tools.keys())
    logger.debug(f"Runtime-bound tools registered for {coara.identity.name}: {registered}")
    # Subagents (delegate_depth >= 1) intentionally omit DelegateTool — only warn at root.
    if coara.delegate_depth == 0 and "delegate" not in registered:
        logger.warning(f"Delegate tool NOT registered for {coara.identity.name}")
