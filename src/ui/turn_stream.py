"""聊天回合的输出流：与 WS 连接解耦的单一事实源。

从 ``web_server.py`` 拆出（纯结构搬迁，行为不变）。

回合协程（process_message）由 WebServer 持有的 task 跑，产出的事件一律
``emit`` 进 ring buffer 并即时广播给当前活跃 WS 连接。连接断开只退订，
回合照跑；重连/刷新时快照附 ``replay()`` 的 buffer，前端无缝接续。

一个类，两种角色（2026-09-11 起）：除「有回合的流」（WebServer._turns 持有、
挂 task、回合收尾注销、sender 注册在 (web, session) 精确槽）外，本类还承载
**standby 流**——没有在飞回合可归属的帧的出口（``WebServer._standby_stream_for``
按 (session, workspace) 建/复用，不挂 task、不进 _turns、不参与收尾注销）。
两条路走同一个 ``_record``：先落带（persist 分配 ``view_seq``）→ 写进帧 →
广播，端侧因此永远收到带序号带归属的帧，不会拿到无序号直推。

两个序号不要混：``seq`` 是本流内的帧序号（从 1 起，replay / ring buffer 用；
standby 流跨回合承载多帧，它连续累加，自身 turn_id 每帧按帧刷新）；``view_seq``
是落带返回的**线上全局**序号（hydrate 对账、去重、gap 检测的唯一数值键——
没有落带的帧（子智能体过程帧）没有它）。

微批：``chunk`` 与 ``subagent_chunk`` 都在 ``_TURN_BATCH_WINDOW_S`` 窗口内合并后
只落一帧——前者全文一桶；后者按 ``tool_call_id`` 分桶（每条 delegate 工具行一
桶），其余字段随桶保留。不合并的话「一 token 一帧」会把 WS 与重连 buffer 冲爆。

隐式契约：构造参数 ``server`` 是 ``WebServer`` 实例，仅用其两个连接注册表——
``server.attach_registry``（cli-attached 定向投递）与 ``server.registry``
（web 单活跃投递），经 EndRoute 的 source+channel_id 决定路由。
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.ui.web_server import WebServer

# 微批合并窗口：text chunk / 子智能体正文在该窗口内合并成一帧，削掉逐 token 一帧的开销。
_TURN_BATCH_WINDOW_S = 0.016
# ring buffer 上限：刷新重连时回放这些事件，超出丢最旧（长回合尾部足够接续）。
_TURN_BUFFER_MAX = 200


class TurnStream:
    """聊天回合的输出流：与 WS 连接解耦的单一事实源。

    回合协程（process_message）产出的事件一律 ``emit`` 进 ring buffer 并即时
    广播给当前活跃 WS 连接。连接断开只退订，回合照跑；重连/刷新时快照附
    ``replay()`` 的 buffer，前端无缝接续。

    同一实例也被 ``WebServer`` 用作 standby 流（无在飞回合时的帧出口）：差别只
    在归属管理（谁持有、谁注销），落带与广播链路完全相同，且帧自带 turn_id 时
    以帧为准（``_record``），流自身的 turn_id 每帧刷新。模块 docstring 有全貌。
    """

    def __init__(
        self,
        turn_id: str,
        source: str,
        subject: str,
        server: WebServer,
        channel_id: str = "",
        session_id: str = "",
        workspace_dir: str = "",
        persist: Callable[[dict[str, Any]], int | None] | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.source = source
        self.subject = subject
        self.session_id = session_id
        # 帧的空间归属：端侧边界守卫靠它判断这帧属不属于当前看的那个空间。
        self.workspace_dir = workspace_dir
        self._server = server
        # 落带回调。缺省走内核录制器（落带是内核职责，与端无关）；显式传入时以
        # 传入者为准（测试夹具与特殊宿主）。回调签名即契约：收帧，返回 view_seq。
        self._persist = persist
        # 统一输出路由（方案 D3）：source+channel_id 决定本回合帧投到哪个端。
        from src.coara.output_router import EndRoute

        self.route = EndRoute(source=source, channel_id=channel_id)
        self.buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=_TURN_BUFFER_MAX)
        self.seq = 0
        self.done = False
        # 该回合归属的工作空间 id（attach 重连回放按 pin 空间过滤；web 回合可为空）。
        self.workspace_id: str = ""
        self.task: asyncio.Task[None] | None = None
        # 服务台投递台签（@daily 等）：帧上带 desk，CLI 回显加 [daily] 前缀
        self.desk = ""
        # 微批暂存：窗口内的 text chunk 合并，flush 时作为一条 chunk 帧发出。
        self._pending_text: list[str] = []
        # 子智能体正文同样微批，但按 tool_call_id 分桶（每条 delegate 行一桶）：
        # 不合并的话「一 token 一帧」会把 WS 与重连 buffer 打爆。
        self._pending_subagent: dict[str, dict[str, Any]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None

    def emit_user_message(
        self,
        text: str,
        *,
        attachments: list[Any] | None = None,
        client_msg_id: str | None = None,
    ) -> None:
        """正式用户行（新回合首条与中途跟话同一形态）。

        跟话不是旁路：字段与落带/广播路径与 ``_stream_chat_turn`` 开局
        ``user_message`` 一致（显示正文、附件、``client_msg_id``）。
        """
        from src.ui.web_views import user_frame_display_text

        kwargs: dict[str, Any] = {
            "content": user_frame_display_text(text),
            "attachments": list(attachments or []),
        }
        cid = str(client_msg_id or "").strip()
        if cid:
            kwargs["client_msg_id"] = cid
        self.emit("user_message", **kwargs)

    def emit(self, kind: str, **payload: Any) -> None:
        """记录一帧并调度广播。text chunk 与子智能体正文走微批，其余即时。"""
        if kind == "chunk":
            self._pending_text.append(str(payload.get("text") or ""))
            self._schedule_flush()
            return
        if kind == "subagent_chunk":
            key = str(payload.get("tool_call_id") or "")
            bucket = self._pending_subagent.get(key)
            if bucket is None:
                bucket = {
                    "texts": [],
                    "fields": {k: v for k, v in payload.items() if k != "text"},
                }
                self._pending_subagent[key] = bucket
            bucket["texts"].append(str(payload.get("text") or ""))
            self._schedule_flush()
            return
        self._flush_pending()
        self._record({"type": kind, **payload})

    def _record(self, frame: dict[str, Any]) -> None:
        self.seq += 1
        frame["seq"] = self.seq
        # 帧自带 turn_id 时以帧为准（standby 流跨回合承载帧，流自身没有回合 id）；
        # 否则用本流回合 id。
        if not frame.get("turn_id"):
            frame["turn_id"] = self.turn_id
        frame.setdefault("source", self.source)
        frame.setdefault("subject", self.subject)
        frame.setdefault("session_id", self.session_id)
        if self.workspace_dir:
            frame.setdefault("workspace_dir", self.workspace_dir)
        if self.desk:
            frame.setdefault("desk", self.desk)
        # 落带先于广播：落盘回调返回分配到的 view_seq，写进帧后再发出去——
        # 端侧因此能与 hydrate 的 latest_seq 对账（去重 / gap 检测）。没有落带
        # 的帧（子智能体过程帧）没有 view_seq，端侧按「丢弃 + 记日志」处理。
        view_seq = self._persist_frame(frame)
        if view_seq:
            frame["view_seq"] = int(view_seq)
        self.buffer.append(frame)
        self._broadcast(frame)

    def _persist_frame(self, frame: dict[str, Any]) -> int | None:
        """落带并返回 view_seq；缺省走内核录制器，故障不中断回合与广播。"""
        if self._persist is not None:
            with contextlib.suppress(Exception):
                return self._persist(frame)
            return None
        from src.ui.view_recorder import record_view_frame

        return record_view_frame(frame, coara_home=getattr(self._server, "coara_home", None))

    def _broadcast(self, frame: dict[str, Any]) -> None:
        """广播一帧给本回合路由到的端（方案 D3 单点决策）。

        - cli-attached（多连接并存）：定向发回发起连接 ``route.channel_id``。
        - web 及其它（单活跃顶替）：发当前活跃浏览器连接。
        """
        if self.route.is_attach:
            self._server.attach_registry.send_to_nowait(self.route.channel_id, frame)
            return
        if self._server.registry.has_active():
            self._server.registry.send_to_active_nowait(frame)

    def _schedule_flush(self) -> None:
        if self._flush_handle is None:
            loop = asyncio.get_running_loop()
            self._flush_handle = loop.call_later(_TURN_BATCH_WINDOW_S, self._flush_pending)

    def _flush_pending(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._pending_text:
            text = "".join(self._pending_text)
            self._pending_text = []
            if text.strip():
                self._record({"type": "chunk", "text": text})
        if self._pending_subagent:
            pending = self._pending_subagent
            self._pending_subagent = {}
            for bucket in pending.values():
                text = "".join(bucket["texts"])
                if text.strip():
                    self._record({"type": "subagent_chunk", "text": text, **bucket["fields"]})

    def finish(self) -> None:
        """回合结束：冲刷残余 chunk 并标 done（turn_end 应已 emit）。"""
        self._flush_pending()
        self.done = True

    def replay(self) -> list[dict[str, Any]]:
        """重连续接用：返回 buffer 内尚未消费的事件快照。"""
        return list(self.buffer)
