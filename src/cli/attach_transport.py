"""Attach transport: 客户端↔内核的窄传输抽象 + WebSocket 实现。

内核化「原 CLI 界面召回」的客户端数据接入层底层。RemoteEventBus 与
RootShim 只依赖 :class:`AttachTransport` 协议（send / register_handler /
close / connected），不感知 WS 细节——服务端 /ws/attach 扩展的帧格式微调
时，核心逻辑零改动。

帧协议（已对齐服务端 src/ui/web_server.py /ws/attach 实现——客户端适配服务端，
web_server.py 为基准不动）：

    Client → Server:
      {"type":"attach","workspace":"<name-or-id>"}          # 握手首帧
      {"type":"chat","text":"...","image_blocks":[...]}      # 发起流式回合
        # 服务端生成 turn_id 并在回合帧回显（TurnStream._record），source 固定
        # "cli-attached"；客户端本地 turn_id 仅用于发起方归属，不回发。
      {"type":"interrupt","reason":"..."}                    # 服务端固定 attach_client
      {"type":"continuation","text":"...","image_blocks":[...]}
      {"type":"drain_continuation"}
      {"type":"cancel_continuation"}                         # 撤回队尾跟话
      {"type":"command","text":"/model ..."}                 # 无 request_id
      {"type":"pending_report","text":"..."}
      {"type":"ping"}
      # 注：服务端收到 chat 时自行 record_user_activity；无 user_activity /
      # context_window 消息类型——客户端本地实现（no-op / 用 attached 快照值）。

    Server → Client:
      {"type":"attached", ...扁平快照字段...}                # 握手快照
        # 扁平字段（非嵌套 identity/foreground/workspace_manager）：
        # workspace/workspace_id/session/provider/model + session_id/
        # workspace_dir/coara_id/agent_name/provider_name/model_name/
        # is_plan_mode/tools_count/skills_count/active_name/
        # foreground_session_id/workspaces[{id,name,path}]/usage/
        # context_window/turn_source（详见 _build_attach_attached_frame）。
        # RootShim 侧经 _normalize_snapshot 归一成嵌套结构再应用。
      {"type":"trace_batch","events":[...]}                  # 事件微批（100ms flush）
        # 事件帧：type(=event_type) + 事件属性/payload 全量 + coara_id
        # + 可选 detached/workspace_name（详见 _serialize_trace_event）。
      {"type":"chunk","text":"...","turn_id":"...","seq":N,"source":"cli-attached"}
        # 主会话正文。子智能体的旁白/正文/最终报告**不走这一帧型**（走了会被
        # 当主会话正文打进滚动区，与结果回显行重复）——见下面两条。
      {"type":"subagent_chunk","text":"...","agent_kind":"subagent","tool_call_id":"...",
       "coara_id":"...","turn_id":"...","seq":N,"source":"cli-attached"}
      {"type":"subagent_result","text":"...","agent_kind":"subagent","tool_call_id":"...",
       "coara_id":"...","turn_id":"...","seq":N,"source":"cli-attached"}
        # 子智能体过程正文 / 最终报告（内核 kind 原样保留）。客户端一律不把它们
        # 当前台正文渲染：CLI 侧只由活动树与 [<类型>子智能体] 回显行呈现。
        # 旧客户端忽略未知帧型（正是「不再重复」的期望行为），不改既有语义。
      {"type":"diff","display_blocks":[...],"tool_name":"...","agent_kind":"main|subagent",
       "parent_tool_call_id":"...","turn_id":"...","seq":N}
        # agent_kind / parent_tool_call_id 为新增归属字段，端侧可忽略；
        # 子智能体改动照旧渲染（工具行层显示不变）。
      {"type":"tool","text":"✓...","ok":true,"turn_id":"...","seq":N,...}
      {"type":"turn_start"/"turn_queued"/"error","turn_id":"...","seq":N,...}
      {"type":"turn_end","turn_id":"...","seq":N,"reason":"complete|interrupted|error"}
      {"type":"command_result","result":{"output":"...","action":"...","data":{...},
       "exit_session":false}}                                # 无 request_id 回显
      {"type":"continuation_accepted"}
      {"type":"continuation_drained","items":[{"text":"...","image_blocks":[...],
       "source":"..."}]}
      {"type":"continuation_cancelled","cancelled":true}
      {"type":"pending_report_result","consumed":false,"result":{...}?}
      {"type":"pong"}
      {"type":"error","message":"...","reason":"occupied|..."}
      # connection_state 帧由本 transport 本地合成（服务端不发）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from src.core.logger import logger

FrameHandler = Callable[[dict[str, Any]], None]


@runtime_checkable
class AttachTransport(Protocol):
    """RemoteEventBus / RootShim 依赖的窄传输接口。

    实现负责连接管理（重连可选）；帧一律为 JSON dict。
    ``register_handler`` 的回调可能在非事件循环线程被调用（WS 接收任务），
    消费方（RemoteEventBus）须自行做线程安全分发。
    """

    async def send(self, frame: dict[str, Any]) -> None:
        """发送一帧；连接未就绪时应抛出或排队（实现定义）。"""
        ...

    def register_handler(self, handler: FrameHandler) -> None:
        """注册入站帧回调（重复注册同一 handler 无效）。"""
        ...

    async def close(self) -> None:
        """关闭连接并释放资源；幂等。"""
        ...

    @property
    def connected(self) -> bool:
        """当前是否已连接。"""
        ...


class AttachTransportClosedError(Exception):
    """send 时连接已关闭。"""


class WsAttachTransport:
    """aiohttp WebSocket 实现的 :class:`AttachTransport`，连内核 /ws/attach。

    只负责连接/收发/自动重连，不含任何 UI 逻辑。重连成功后通过
    ``_on_reconnect`` 钩子通知（RootShim 据此重新握手拉快照）。
    """

    def __init__(
        self,
        url: str,
        *,
        handshake_frame: dict[str, Any] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        heartbeat: float = 30.0,
        connect_timeout: float = 5.0,
        reconnect: bool = True,
        reconnect_delay: float = 2.0,
        session: Any | None = None,
    ) -> None:
        self._url = url
        self._handshake_frame = handshake_frame
        self._on_reconnect = on_reconnect
        self._heartbeat = heartbeat
        self._connect_timeout = connect_timeout
        self._reconnect_enabled = reconnect
        self._reconnect_delay = reconnect_delay
        self._session = session  # 外部传入的 aiohttp.ClientSession（测试/共享用）
        self._owns_session = session is None

        self._handlers: list[FrameHandler] = []
        self._ws: Any | None = None
        self._connected = False
        self._closed = False
        self._recv_task: asyncio.Task[Any] | None = None
        self._send_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._first_connect = True

    @property
    def connected(self) -> bool:
        return self._connected

    def register_handler(self, handler: FrameHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def _emit(self, frame: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            try:
                handler(frame)
            except Exception as exc:
                logger.warning(f"WsAttachTransport: frame handler failed: {exc}")

    def _emit_connection_state(self, connected: bool, reason: str = "") -> None:
        self._emit({"type": "connection_state", "connected": connected, "reason": reason})

    async def send(self, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            ws = self._ws
            if ws is None or not self._connected or self._closed:
                raise AttachTransportClosedError("attach transport 未连接")
            # Windows CLI 常把 emoji 收成 UTF-16 代理对；不清洗则 utf-8 encode 直接炸。
            from src.utils.text_utils import sanitize_json_payload

            safe = sanitize_json_payload(frame)
            await ws.send_str(json.dumps(safe, ensure_ascii=False))

    async def connect(self) -> None:
        """建立连接并启动接收循环；失败抛错。幂等（已连接时直接返回）。"""
        if self._connected:
            return
        if self._closed:
            raise AttachTransportClosedError("attach transport 已关闭")
        await self._open_once()
        self._recv_task = asyncio.create_task(self._receive_loop())

    async def _open_once(self) -> None:
        import aiohttp

        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=self._connect_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        self._ws = await self._session.ws_connect(self._url, heartbeat=self._heartbeat)
        self._connected = True
        self._connected_event.set()
        self._emit_connection_state(True, reason="connected" if self._first_connect else "reconnected")
        if self._handshake_frame is not None:
            await self.send(dict(self._handshake_frame))

    async def _receive_loop(self) -> None:
        from aiohttp import WSMsgType

        assert self._ws is not None
        ws = self._ws
        try:
            while not self._closed:
                msg = await ws.receive()
                if msg.type == WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(frame, dict):
                        self._emit(frame)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED) or msg.type == WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"WsAttachTransport: receive loop error: {exc}")
        finally:
            self._connected = False
            self._connected_event.clear()
            self._ws = None
            if not self._closed:
                self._emit_connection_state(False, reason="disconnected")

        if not self._closed and self._reconnect_enabled:
            await self._reconnect_loop()

    async def _reconnect_loop(self) -> None:
        while not self._closed and not self._connected:
            await asyncio.sleep(self._reconnect_delay)
            if self._closed:
                return
            try:
                await self._open_once()
            except Exception as exc:
                logger.debug(f"WsAttachTransport: reconnect failed: {exc}")
                continue
            # 重连成功：通知上层重新握手/拉快照
            self._first_connect = False
            if self._on_reconnect is not None:
                try:
                    self._on_reconnect()
                except Exception as exc:
                    logger.warning(f"WsAttachTransport: on_reconnect failed: {exc}")
            # 重启接收循环（当前 task 即将退出）
            self._recv_task = asyncio.create_task(self._receive_loop())
            return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._recv_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ws = self._ws
        self._ws = None
        self._connected = False
        self._connected_event.clear()
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._owns_session and self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None
