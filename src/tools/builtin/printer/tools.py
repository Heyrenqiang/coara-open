"""本机打印工具（内置挂起，tool(activate) 揭示后可用）。

能力源自退役的 MCP printer server，语义保持一致。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

from . import printer


class _Invocation(ToolInvocation):
    def get_description(self) -> str:
        action = str(self.params.get("action") or "")
        if action == "print":
            return f"打印文件 {self.params.get('file_path', '')}"
        return "列出本机打印机"

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action") or "")
        try:
            if action == "list":
                printers = await asyncio.to_thread(printer.list_printers)
                if not printers:
                    return ToolResult.success("本机没有可用打印机")
                return ToolResult.success(
                    json.dumps(printers, ensure_ascii=False, indent=2),
                    metadata={"count": len(printers)},
                )
            if action == "print":
                result = await asyncio.to_thread(
                    printer.print_file,
                    file_path=str(self.params.get("file_path") or ""),
                    printer_name=str(self.params.get("printer_name") or ""),
                    copies=int(self.params.get("copies") or 1),
                    pages=str(self.params.get("pages") or ""),
                )
                return ToolResult.success(result)
        except Exception as exc:
            return ToolResult.error(str(exc))
        return ToolResult.error(f"未知 action：{action!r}，可用 ['list', 'print']")


class PrinterTool(BaseTool):
    name = "printer"
    summary = "本机打印"
    description = (
        "本机打印。\n\n"
        "| action | 用途 |\n"
        "|--------|------|\n"
        "| `list` | 列出本机打印机（name / is_default / status），默认打印机见 is_default=true 项 |\n"
        "| `print` | 打印本地 PDF 或图片文件，不支持 txt/md/Office，Office 请先转 PDF，走审批 |"
    )
    display_name = "Printer"
    category = "system"
    kind = ToolKind.EXECUTE
    should_defer = True
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "print"],
                "description": "list=列出打印机；print=打印文件",
            },
            "file_path": {"type": "string", "description": "print 要打印文件的绝对路径"},
            "printer_name": {"type": "string", "description": "print 的打印机名，留空用默认打印机", "default": ""},
            "copies": {"type": "integer", "description": "print 的份数 1-20，默认 1", "default": 1},
            "pages": {"type": "string", "description": "print 的页码范围，仅 PDF，如 1-3 或 1,3-5", "default": ""},
        },
        "required": ["action"],
    }

    @staticmethod
    def requires_approval(args: dict[str, Any]) -> bool:
        # 打印产生实体消耗（纸张/墨），与用户确认一次；列打印机不审批
        return args.get("action") == "print"

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _Invocation(params)


PRINTER_TOOL_TYPES = (PrinterTool,)
