from __future__ import annotations

from aiohttp import web

from src.ui.handler_contract import HandlerMixinBase


class CommandsHandlers(HandlerMixinBase):
    async def _handle_command_list(self, request: web.Request) -> web.Response:
        """Return slash commands + CLI-parity picker menus for Web autocomplete."""
        self._check_token(request)
        from src.cli.slash_pickers import PICKER_COMMANDS, pickers_payload_for_root

        # Top-level stems only (secondary args come from ``pickers``, like CLI).
        # Web 端砍掉有图形等价的命令（与 web_server._WEB_COMMAND_BLOCKLIST 对齐）：
        # exit/login/status/ws/new/model——点鼠标即可完成，不出现在斜杠补全里。
        commands = [
            {"name": "/help", "description": "显示可用命令", "category": "会话"},
            {"name": "/compact", "description": "手动压缩当前会话历史", "category": "会话"},
            {"name": "/report", "description": "向开发者提交问题报告（附带本轮会话）", "category": "会话"},
            {"name": "/thinking", "description": "LLM 思考 / reasoning 模式", "category": "模型"},
            {"name": "/sandbox", "description": "切换执行沙箱开关", "category": "安全"},
            {"name": "/tools", "description": "查看可用工具及可见性", "category": "工具"},
            {"name": "/events", "description": "查看外部事件源", "category": "工具"},
            {"name": "/vault", "description": "保险柜状态 / 锁定", "category": "资产"},
        ]
        names: list[str] = []
        manager = self.root.workspace_manager
        if manager:
            names = [e.name for e in manager.list_workspaces()]
        pickers = pickers_payload_for_root(self.root)
        # /ws、/model 在 Web 端被拦（侧栏空间切换 / 顶栏模型选择是图形等价）：
        # 剔除其 picker，但不动共享的 PICKER_COMMANDS（CLI 仍需要）。
        pickers.pop("/ws", None)
        pickers.pop("/model", None)
        picker_commands = sorted(c for c in PICKER_COMMANDS if c not in ("/ws", "/model"))
        return web.json_response(
            {
                "commands": commands,
                "workspaces": names,
                "pickers": pickers,
                "picker_commands": picker_commands,
            }
        )
