"""Attach CLI: 经 WS 连常驻内核 /ws/attach 的完整样子客户端。

纯客户端进程：不抢单实例锁、不起 RootCoara、不订阅 event_bus。
会话状态在内核；本模块驱动与主 CLI 对齐的终端 UI（StreamingBlock /
Thinking / 底栏），协议仍是 attach。

与服务端 /ws/attach 的协议约定（服务端由另一分支实现，握手字段为对齐点）:

    Client → Server:
      {"type":"attach","workspace":"<name-or-id>"}   # 连接后首条握手消息
      {"type":"chat","text":"..."}
      {"type":"interrupt"}

    Server → Client:
      {"type":"attached","workspace":"...","session":"..."}  # 握手确认（可选）
      {"type":"chunk","text":"..."}          # 流式输出
      {"type":"tool","text":"...","ok":true} # 工具摘要行
      {"type":"turn_end","reason":"..."}     # 一回合结束
      {"type":"error","message":"..."}       # 错误（含占用拒绝，reason=busy/occupied）

鉴权失败时服务端以 WS 关闭码 4001 关闭连接（与 /ws 一致）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

import aiohttp
from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError
from aiohttp.client_exceptions import ClientConnectorError, ClientResponseError

from src.core.logger import logger
from src.ui.web_link import (  # noqa: F401
    load_web_token,
    resolve_web_coara_home,
    resolve_web_host_port,
)

AUTH_CLOSE_CODE = 4001
CONNECT_TIMEOUT_SECONDS = 5.0

# host/port、home、token 解析已下沉到 src/ui/web_link.py（零重依赖，CLI 首启
# 向导等早期路径共用）；此处保留别名兼容既有 import。
resolve_attach_coara_home = resolve_web_coara_home
load_attach_token = load_web_token


class AttachError(Exception):
    """Base error for attach client failures (friendly message, non-zero exit)."""


class AttachAuthError(AttachError):
    """Dashboard token rejected by the server (WS close code 4001)."""


def build_attach_url(host: str, port: int, token: str) -> str:
    return f"ws://{host}:{port}/ws/attach?token={token}"


def build_chat_message(text: str) -> dict[str, str]:
    return {"type": "chat", "text": text}


def build_command_message(text: str) -> dict[str, str]:
    return {"type": "command", "text": text}


def is_slash_command(text: str) -> bool:
    """斜杠命令（/model 等）走 command 通道而非 chat——主进程的 process_message
    不解析斜杠，直接发 chat 会被当普通对话喂给 LLM。"""
    return text.startswith("/")


def build_handshake_message(workspace: str) -> dict[str, str]:
    # 与服务端 /ws/attach 的约定点：首条消息声明目标工作空间（name 或 id）。
    return {"type": "attach", "workspace": workspace}


def encode_message(payload: dict[str, Any]) -> str:
    from src.utils.text_utils import sanitize_json_payload

    return json.dumps(sanitize_json_payload(payload), ensure_ascii=False)


def decode_message(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise AttachError("服务端消息格式非法")
    return data





def _raise_for_close(ws: aiohttp.ClientWebSocketResponse) -> None:
    if ws.close_code == AUTH_CLOSE_CODE:
        raise AttachAuthError("鉴权失败：dashboard token 不匹配（与主进程不在同一 coara Home？）")
    raise AttachError(f"服务端关闭了连接（关闭码 {ws.close_code}）")


async def _receive_loop(ws: aiohttp.ClientWebSocketResponse, ui: Any, turn_done: asyncio.Event) -> None:
    """按帧类型驱动 UI 渲染；turn_end 收尾并置位；连接关闭抛错。"""
    while True:
        msg = await ws.receive()
        if msg.type == WSMsgType.TEXT:
            try:
                frame = decode_message(msg.data)
            except (json.JSONDecodeError, AttachError):
                continue
            kind = frame.get("type", "")
            if kind == "turn_start":
                ui.begin_turn()
            elif kind == "chunk":
                ui.print_chunk(str(frame.get("text", "")))
            elif kind == "tool":
                ui.print_tool(str(frame.get("text", "")), bool(frame.get("ok", True)))
            elif kind == "turn_end":
                ui.end_turn()
                turn_done.set()
            elif kind == "error":
                ui.print_error(
                    str(frame.get("message", "未知错误")),
                    occupied=str(frame.get("reason", "")) in ("busy", "occupied"),
                )
                turn_done.set()
            elif kind == "command_result":
                result = frame.get("result") or {}
                ui.print_command_result(str(result.get("output", "") or ""))
                # /model 等可能更新模型展示：若帧带 provider/model 则同步底栏
                if frame.get("provider"):
                    ui.provider = str(frame["provider"])
                if frame.get("model"):
                    ui.model = str(frame["model"])
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            _raise_for_close(ws)
        elif msg.type == WSMsgType.ERROR:
            raise AttachError(f"WebSocket 连接错误: {ws.exception()}")


async def _input_loop(ws: aiohttp.ClientWebSocketResponse, ui: Any, turn_done: asyncio.Event) -> None:
    """prompt_toolkit 读输入；斜杠走 command、对话走 chat。

    发送后立即重建输入框（不 await turn_done）——回合输出由 _receive_loop 经
    patch_stdout 异步渲染到输入框上方，输入区始终在场，与主 CLI 体验一致。
    回合进行中再发的消息由内核 per-workspace FIFO 队列串行处理（阶段3 D5）。
    不回显用户行：ptk 提交后「你：」+正文已留在终端，与主 CLI 一致。
    """
    session = ui.build_session()
    while True:
        text = await ui.prompt(session)
        if text is None or text in ("exit", "quit"):
            return
        if not text:
            continue
        if is_slash_command(text):
            await ws.send_str(encode_message(build_command_message(text)))
            continue
        await ws.send_str(encode_message(build_chat_message(text)))


async def _await_attached_frame(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    """握手后等服务端 attached 帧（含 空间/会话/模型，建 UI 用）；被拒或关闭抛错。"""
    while True:
        msg = await ws.receive()
        if msg.type == WSMsgType.TEXT:
            frame = decode_message(msg.data)
            kind = frame.get("type")
            if kind == "attached":
                return frame
            if kind == "error":
                reason = str(frame.get("reason", ""))
                if reason in ("busy", "occupied"):
                    raise AttachError(str(frame.get("message", "工作空间被占用")))
                raise AttachError(str(frame.get("message", "接入失败")))
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            _raise_for_close(ws)
        elif msg.type == WSMsgType.ERROR:
            raise AttachError(f"WebSocket 连接错误: {ws.exception()}")


async def attach_session(
    workspace: str,
    *,
    host: str,
    port: int,
    token: str,
    out: TextIO | None = None,
) -> None:
    """Connect to /ws/attach, handshake, and run the chat UI until exit."""
    out = out or sys.stdout
    url = build_attach_url(host, port, token)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_SECONDS)
    try:
        async with ClientSession(timeout=timeout) as session:
            try:
                async with session.ws_connect(url, heartbeat=30.0) as ws:
                    await ws.send_str(encode_message(build_handshake_message(workspace)))
                    attached = await _await_attached_frame(ws)
                    from src.cli.attach_ui import make_attach_ui

                    ui = make_attach_ui(attached)
                    turn_done = asyncio.Event()
                    receiver = asyncio.create_task(_receive_loop(ws, ui, turn_done))
                    sender = asyncio.create_task(_input_loop(ws, ui, turn_done))
                    done, pending = await asyncio.wait(
                        {receiver, sender}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    await ui.aclose()
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            raise exc
            except (ClientConnectorError, ConnectionError, OSError, TimeoutError) as exc:
                raise AttachError(f"无法连接主进程（{host}:{port}）：主进程未启动，请先运行 coara") from exc
            except WSServerHandshakeError as exc:
                if exc.status in (401, 403):
                    raise AttachAuthError(
                        "鉴权失败：dashboard token 不匹配（与主进程不在同一 coara Home？）"
                    ) from exc
                if exc.status == 404 or 200 <= exc.status < 300:
                    # 路由未注册时 SPA 兜底处理器会返回 index.html（200），与 404 同义。
                    raise AttachError("主进程不支持外挂接入（/ws/attach 不存在），请升级主进程") from exc
                raise AttachError(f"握手失败（HTTP {exc.status}）") from exc
            except ClientResponseError as exc:
                raise AttachError(f"连接失败（HTTP {exc.status}）") from exc
    except KeyboardInterrupt:
        print("\n已退出。", file=out)


def run_attach_command(
    workspace: str,
    *,
    workspace_dir: Path,
    host: str,
    port: int,
    token: str,
    out: TextIO | None = None,
) -> int:
    """Synchronous entry for the CLI subcommand; returns process exit code."""
    out = out or sys.stdout
    # 启动静默：attach 是纯客户端，不刷配置加载的 DEBUG/INFO。
    # 压掉 loguru 默认 handler，只留 WARNING 及以上；错误仍可见。
    try:
        from src.core.logger import logger as _logger

        _logger.remove()
        _logger.add(sys.stderr, level="WARNING", format="<level>{message}</level>")
    except Exception as exc:
        logger.debug(f"重配置 loguru 失败，沿用默认日志配置：{exc}")
    try:
        asyncio.run(attach_session(workspace, host=host, port=port, token=token, out=out))
    except AttachAuthError as exc:
        print(str(exc), file=out)
        return 2
    except AttachError as exc:
        print(str(exc), file=out)
        return 1
    return 0
