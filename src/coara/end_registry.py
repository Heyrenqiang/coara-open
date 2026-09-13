"""内核端注册表（EndRegistry）——按端查询当前活跃出站通道。

输出路由动态化的地基：三端（web 活跃 WS / attach 当前连接 / matrix room）的
出站能力统一登记在这里。内核输出（正文 chunk / 工具 diff）统一为**输出帧**，
按当前注入段的 ``source`` 查表取通道投递（``deliver``），替代各端在回合启动时
捕获死目标的消费闭包、以及各端各自订阅事件过滤 diff 的三套实现。投递的**唯一**
入口就是 ``deliver``：旧的 ``route()``（只把 sender 返回值透传给调用方、命中信息
丢失）已在本轮收敛删除——所有调用方按 ``RouteResult.hit`` 判据行事。

帧协议::

    {"kind": "chunk", "text": "..."}                     # 正文
    {"kind": "diff", "display_blocks": [...], ...}       # 工具改动

端通道 sender 收帧后自行决定渲染（文本气泡 / diff 卡片）。

命中判据与执行结果分离（2026-09-11）：sender 的返回值是端自己的事（web 端为
``True``，attach/matrix 端可能返回 ``None`` 或协程），**不能**用 ``sender 结果
is None`` 反推「没命中通道」——那会让同步 sender 的每一次投递都被判成失败。
需要命中判据的调用方一律走 ``deliver()``，它把两件事分开返回。

性能约束：注册/查询都是 O(1) dict 操作；在线直推路径不引入锁——各端出站函数
自身已处理并发（WS send / matrix HTTP），注册表只做引用存取。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.core.logger import logger

# 端出站投递函数签名：(frame: dict) -> awaitable | None
SendText = Callable[[dict[str, Any]], Any]


@dataclass(slots=True)
class RouteResult:
    """一次路由尝试的结果：``hit`` 是「命中了通道」，``value`` 是 sender 的返回值。

    ``hit=False`` 时 ``value`` 恒为 None（没有任何 sender 被调用）。
    ``hit=True`` 时 ``value`` 可能仍是 None（同步 sender 无返回值）——这正是
    不能拿 ``value is None`` 当命中判据的原因。
    """

    hit: bool
    value: Any = None


class EndRegistry:
    """(source, session_id) -> 当前活跃出站投递函数 的注册表。

    显示通道按会话作用域登记：各端的回合消费者在回合启动时注册本回合的
    显示通道（sender 闭包内捕获 room/连接/detach 上下文），路由按
    ``(seg_source, session_id)`` 精确命中——attach 多连接、matrix 房间、
    web 多会话互不覆盖。session_id 为空串的槽位是全局兜底（某端整体一个
    活跃通道时用）。注册/更新/注销由各端回合生命周期驱动；内核输出路由只读。
    """

    def __init__(self) -> None:
        self._senders: dict[tuple[str, str], SendText] = {}
        # 无人接收的帧的兜底落带器（装配层注入；内核只持抽象回调，不 import 显示端）
        self._tape_sink: Any | None = None
        # 端内连接索引：(source, session_id, channel_id) -> sender。
        # 与 _senders 同步维护——_senders 单槽只留最新（旧语义/全局兜底），
        # 本索引保留同端同会话的全部连接，多 attach 并存时各回各家。
        self._channel_senders: dict[tuple[str, str, str], SendText] = {}

    def register(self, source: str, send_text: SendText, session_id: str = "") -> None:
        """登记/覆盖某端在某会话的当前出站通道。session_id 缺省为全局槽位。

        sender 带 ``_end_channel_id`` 标（attach 连接打标）时同步进连接索引。
        """
        if not source:
            return
        self._senders[(source, session_id or "")] = send_text
        channel_id = str(getattr(send_text, "_end_channel_id", "") or "")
        if channel_id:
            self._channel_senders[(source, session_id or "", channel_id)] = send_text
        # 排障取证：注册侧与路由侧的键若不同源，日志里一眼能对上（默认静默）。
        scope = "connection" if getattr(send_text, "_end_connection_scope", False) else "turn"
        logger.debug(f"[EndRegistry] register {source}/{scope} session='{session_id or ''}' channel='{channel_id}'")

    def unregister(self, source: str, send_text: SendText | None = None, session_id: str = "") -> None:
        """注销通道。给定 send_text 时仅当仍指向同一通道才删（防误删新通道）。

        连接级兜底通道（sender 带 ``_end_connection_scope`` 标）不参与回合生命
        周期注销：它注册在全局槽，被回合收尾顺手删掉后，该端此后所有未精确命中
        的帧都会静默丢弃——且刷新页面之前不会再恢复。
        """
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
        """按端+会话+端内连接查通道：同端多连接（多条 attach）并存时按
        channel_id 精确归位；通道侧按 (source, session_id) 注册（同一会话同一
        连接只会注册一条），命中前用注册表反查该会话全部候选里 channel 匹配
        的那条。无 channel_id 时退化为 sender_for。"""
        if not channel_id:
            return self.sender_for(source, session_id)
        # 连接索引按 (source, session, channel_id) 精确命中；注册时已打标，
        # 不受会话单槽覆盖影响（同会话多条 attach 各自留存）。
        sender = self._channel_senders.get((source, session_id, channel_id))
        if sender is not None:
            return sender
        # 索引 miss 分两义：
        # - 该端有打标通道（attach 多连接端）→ 段指向的连接已不在，回退会话槽
        #   会串到别的连接——宁丢不串，返回 None 由调用方兜底。
        # - 该端完全无打标通道（matrix 等单通道端）→ channel_id 对它无意义，
        #   回退会话槽位，不因段带 channel_id 丢帧。
        if any(k[0] == source and k[1] == session_id for k in self._channel_senders):
            return None
        return self.sender_for(source, session_id)

    def has(self, source: str, session_id: str = "") -> bool:
        return self.sender_for(source, session_id) is not None

    def set_tape_sink(self, sink: Any | None) -> None:
        """注入「无人接收的帧」的兜底落带器（装配层提供，内核只持抽象回调）。

        分层要求：内核不得 import 显示端，落带实现在 ui 层装配时注入。有端接收的
        帧由该端的显示流落带（帧要带回 view_seq 供端上对账）；无人接收的帧在这里
        落——两条路互斥，不重复。
        """
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
        """投递一帧并按「命中通道」返回 ``RouteResult``（命中判据与执行结果分离）。

        命中 = 查到 sender（会话槽优先、回退全局槽、channel_id 精确归位），
        与 sender 自己的返回值无关——同步 sender 返回 None 也是命中。
        未命中记 WARNING 留痕（是「没人注册」还是「注册键与路由键不同源」，
        日志里写清键名），由调用方决定兜底（换端重投 / 丢弃）。
        """
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
