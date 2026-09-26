"""内核端注册表（EndRegistry）——按端查询当前活跃出站通道"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.core.logger import logger

# 端出站投递函数签名：(frame: dict) -> awaitable | None
SendText = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class RouteResult:
    """一次路由尝试的结果：``hit`` 是「命中了通道」，``value`` 是 sender 的返回值"""

    hit: bool
    value: Any = None


class EndRegistry:
    """(source, session_id) -> 当前活跃出站投递函数 的注册表"""

    #: 通道类型：``turn`` = 回合主通道；``followup`` = 跟话通道（新跟话可顶替，回合结束该收回）。
    CHANNEL_KINDS = ("turn", "followup")

    #: 历史标记名（三端过去各用各的属性名）——不传 kind 时按它推导，兼容旧调用点。
    _FOLLOWUP_ATTRS = ("_end_followup", "_end_matrix_followup", "_end_followup_channel")

    def __init__(self) -> None:
        self._senders: dict[tuple[str, str], SendText] = {}
        # 槽位的通道类型：(source, session_id) -> "turn" / "followup"
        self._kinds: dict[tuple[str, str], str] = {}
        # 无人接收的帧的兜底落带器（装配层注入；内核只持抽象回调，不 import 显示端）
        self._tape_sink: Any | None = None
        # 本索引保留同端同会话的全部连接，多 attach 并存时各回各家。
        self._channel_senders: dict[tuple[str, str, str], SendText] = {}

    @classmethod
    def _kind_from_sender(cls, send_text: Any) -> str:
        """不传 kind 时按 sender 上的历史标记推导通道类型。"""
        for attr in cls._FOLLOWUP_ATTRS:
            if getattr(send_text, attr, False):
                return "followup"
        return "turn"

    def register(self, source: str, send_text: SendText, session_id: str = "", *, kind: str = "") -> None:
        """登记/覆盖某端在某会话的当前出站通道。session_id 缺省为全局槽位。

        ``kind``：``"turn"``（回合主通道，默认）或 ``"followup"``（跟话通道）。
        不传时按 sender 上的历史标记推导——三端过去各用各的属性名，这里统一识别；
        调用方可以改成显式传参，判据（``is_followup``）随之统一到本注册表。
        sender 带 ``_end_channel_id`` 标（attach 连接打标）时同步进连接索引。
        """
        if not source:
            return
        resolved = kind or self._kind_from_sender(send_text)
        self._senders[(source, session_id or "")] = send_text
        self._kinds[(source, session_id or "")] = resolved
        channel_id = str(getattr(send_text, "_end_channel_id", "") or "")
        if channel_id:
            self._channel_senders[(source, session_id or "", channel_id)] = send_text
        # 排障取证：注册侧与路由侧的键若不同源，日志里一眼能对上（默认静默）。
        scope = "connection" if getattr(send_text, "_end_connection_scope", False) else "turn"
        logger.debug(f"[EndRegistry] register {source}/{scope} session='{session_id or ''}' channel='{channel_id}'")

    def unregister(self, source: str, send_text: SendText | None = None, session_id: str = "") -> None:
        """注销通道"""
        if getattr(send_text, "_end_connection_scope", False):
            logger.debug(f"[EndRegistry] unregister skipped (connection scope) source='{source}'")
            return
        key = (source, session_id or "")
        matched = send_text is None or self._senders.get(key) is send_text
        logger.debug(
            f"[EndRegistry] unregister source='{source}' session='{session_id or ''}' identity_matched={matched}"
        )
        if send_text is None or self._senders.get(key) is send_text:
            self._senders.pop(key, None)
            self._kinds.pop(key, None)
        # 同步清连接索引：按身份删，防误删同会话其它连接的通道。
        for ch_key, cand in list(self._channel_senders.items()):
            if ch_key[0] == source and ch_key[1] == (session_id or "") and (send_text is None or cand is send_text):
                self._channel_senders.pop(ch_key, None)

    def sender_for(self, source: str, session_id: str = "") -> SendText | None:
        """按端+会话查当前出站投递函数：先精确命中会话槽位，回退全局槽位。"""
        if session_id:
            sender = self._senders.get((source, session_id))
            if sender is not None:
                return sender
        return self._senders.get((source, ""))

    def sender_for_channel(self, source: str, session_id: str, channel_id: str = "") -> SendText | None:
        """按端+会话+端内连接查通道：同端多连接（多条 attach）并存时按 channel_id 精确归位。

        channel_id 过期（断连重连 / resume / 段上残留旧 conn）时不得静默丢帧：
        仅一条存活连接则投它；多条则回退会话槽（最近 register），总好过整段空白。
        """
        if not channel_id:
            return self.sender_for(source, session_id)
        # 连接索引按 (source, session, channel_id) 精确命中；注册时已打标，
        # 不受会话单槽覆盖影响（同会话多条 attach 各自留存）。
        sender = self._channel_senders.get((source, session_id, channel_id))
        if sender is not None:
            return sender
        matches = [s for (src, sess, _ch), s in self._channel_senders.items() if src == source and sess == session_id]
        if len(matches) == 1:
            return matches[0]
        return self.sender_for(source, session_id)

    def has(self, source: str, session_id: str = "") -> bool:
        return self.sender_for(source, session_id) is not None

    def channel_kind(self, source: str, session_id: str = "") -> str:
        """该槽位当前的通道类型：``"turn"`` / ``"followup"``；没有通道时返回空串。

        槽位解析与 ``sender_for`` 同序（先精确会话槽、再全局槽）。
        """
        if session_id and (source, session_id) in self._senders:
            return self._kinds.get((source, session_id), "turn")
        if (source, "") in self._senders:
            return self._kinds.get((source, ""), "turn")
        return ""

    def is_followup(self, source: str, session_id: str = "") -> bool:
        """该槽位是不是跟话通道——跟话可以顶替跟话，但不得顶替在跑的回合主通道。"""
        return self.channel_kind(source, session_id) == "followup"

    def set_tape_sink(self, sink: Any | None) -> None:
        """注入「无人接收的帧」的兜底落带器（装配层提供，内核只持抽象回调）"""
        self._tape_sink = sink

    def _tape_orphan(self, frame: dict[str, Any], *, source: str, session_id: str) -> None:
        """没有端接这一帧时兜底落带：没人看 ≠ 不该记，丢了就是丢数据。"""
        sink = self._tape_sink
        if sink is None or not isinstance(frame, dict):
            return
        enriched = {
            **frame,
            "session_id": str(frame.get("session_id") or session_id or ""),
            "source": str(frame.get("source") or source or ""),
        }
        try:
            sink(enriched)
        except Exception:  # noqa: BLE001 — 兜底落带失败绝不打断投递路径
            logger.debug("[EndRegistry] tape sink failed", exc_info=True)

    def deliver(self, source: str, session_id: str, frame: dict[str, Any], channel_id: str = "") -> RouteResult:
        """投递一帧并按「命中通道」返回 ``RouteResult``（命中判据与执行结果分离）"""
        sender = (
            self.sender_for_channel(source, session_id, channel_id)
            if channel_id
            else self.sender_for(source, session_id)
        )
        if sender is None:
            # 取证：查找用的键 + 表里现有的键。是「没人注册」还是「注册键与路由键
            # 不同源」，这一行直接写清楚，不再靠猜。
            logger.warning(
                f"[EndRegistry] route miss source='{source}' session='{session_id}' "
                f"channel='{channel_id}' keys={sorted(str(k) for k in self._senders)}"
            )
            self._tape_orphan(frame, source=source, session_id=session_id)
            return RouteResult(hit=False)
        return RouteResult(hit=True, value=sender(frame))
