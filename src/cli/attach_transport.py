"""Attach transport: 客户端↔内核的窄传输抽象 + WebSocket 实现"""

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
    """RemoteEventBus / RootShim 依赖的窄传输接口"""

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
    """aiohttp WebSocket 实现的 :class:`AttachTransport`，连内核 /ws/attach"""

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
