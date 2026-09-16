"""外挂 CLI（``coara attach``）WebSocket 端点 mixin — 从 ``web_server.py`` 拆出
（纯结构搬迁，行为不变）。

``/ws/attach`` 端点全家：握手/占用校验、attached 快照帧、消息路由
（chat/command/interrupt/continuation/pending_report/ping）、pin 空间回合
驱动（TurnStream 按发起连接路由输出）、斜杠命令透传统一命令层。

与 webui 单活跃 ``/ws`` 完全独立：多连接并存，每连接绑定一个工作空间，
回合输出按发起连接路由（TurnStream.route），互不串扰也不顶替 webui。

输出帧的 kind 是归属凭据，本模块的 ``_attach_output_frame`` 是内核帧 → attach
帧的唯一映射点（与 web 的 ``WebServer._emit_end_frame`` 同位）：映射处必须保留
kind，一律并进 ``chunk`` 等于把子智能体的正文冒充主会话正文（CLI 分不出来，
会当作前台回合正文打进滚动区，与结果回显行重复一遍）。

隐式契约（由 WebServer 主类提供）：
- ``self.root``：RootCoara（workspace_manager/_sessions/ensure_workspace_session/
  switch_llm_for_workspace/start_new_session_for_workspace/end_registry）
- ``self.attach_registry``：attach 连接注册表（register/unregister/占用互斥）
- ``self.attach_interaction_channel``：attach 专属审批通道（for_connection 定向）
- ``self._check_token(request)``：HTTP token 鉴权
- ``self._send_error(ws, ...)``：WS 错误帧出口
- ``self._spawn_bg_task(coro)``：fire-and-forget 任务追踪
- ``self._chat_tasks``：每连接回合任务表（断连清理/关停取消）
- ``self._turns`` / ``self._gc_finished_turns()``：回合流注册表与回收
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any, cast

from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import ClientConnectionResetError

from src.coara.turn_context import turn
from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase
from src.ui.turn_stream import TurnStream

# 子智能体过程帧的内核 kind 全集（``CoaraBase._route_subagent_chunk`` 与
# ``DelegateTool._route_subagent_result`` 打的就是这两个）：它们是子智能体执行中
# 的旁白/正文与最终报告，不是前台主会话正文。出 attach 帧时必须保留这个归属。
_SUBAGENT_FRAME_KINDS = frozenset({"subagent_chunk", "subagent_result"})

# 跟话流在 ``self._turns`` 里的键前缀。跟话流没有自己的回合协程（那个回合由别端/
# 内核跑），收尾只能靠 ``turn_end`` 事件补；前缀是把跟话流与开局回合流（键=turn_id，
# 自己 finally 收尾）分开的唯一凭据——收尾时绝不能碰后者。
_FOLLOWUP_STREAM_PREFIX = "followup-attach-"


def _attach_output_frame(frame: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """内核输出帧 → attach 端 ``(帧型, 载荷)``；``None`` = 本端不显示这一帧。

    CLI 端的唯一映射点（与 web 的 ``WebServer._emit_end_frame`` 同位）：kind 是
    帧归属的唯一凭据，在映射处把它抹平（一律并进 ``chunk``）等于让子智能体的
    正文冒充主会话正文——CLI 分不出，会按前台回合正文打进滚动区，与随后的
    ``[<类型>子智能体]`` 结果回显行重复（子智能体过程旁白同一原因多进一份）。

    分工：
    - ``tool``：CLI 的工具行走自己的 scrollback 链路（✓/✗ 由回合 yield 到达），
      这里不投（投了会当正文多一行）
    - ``diff``：转 diff 帧 + 归属字段（纯新增字段，端侧可忽略；子智能体改动照旧
      渲染，工具行层显示不变）
    - ``subagent_chunk`` / ``subagent_result``：保留独立帧型 + ``agent_kind``
      ——端侧一律不当前台正文渲染（旧端忽略未知帧型，行为即「不再重复」）
    - 其余（``chunk``）：主会话正文，原样
    """
    kind = str(frame.get("kind") or "")
    if kind == "tool":
        return None
    if kind == "diff":
        return (
            "diff",
            {
                "display_blocks": frame.get("display_blocks"),
                "diff_lines": frame.get("diff_lines"),
                "tool_name": frame.get("tool_name", ""),
                # 归属字段（新增）：谁产生的改动，端侧不解析也照旧渲染
                "agent_kind": "subagent" if frame.get("parent_tool_call_id") else "main",
                "parent_tool_call_id": str(frame.get("parent_tool_call_id") or ""),
            },
        )
    text = str(frame.get("text") or "")
    if not text.strip():
        return None
    if kind in _SUBAGENT_FRAME_KINDS:
        return (
            kind,
            {
                "text": text,
                "agent_kind": "subagent",
                "tool_call_id": str(frame.get("tool_call_id") or ""),
                "coara_id": str(frame.get("coara_id") or ""),
                "subagent_id": str(frame.get("subagent_id") or ""),
            },
        )
    return "chunk", {"text": text}


class AttachWsHandlers(HandlerMixinBase):
    async def _handle_attach_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """外挂 CLI 接入：鉴权 → 解析目标工作空间 → 双向占用校验 → 对话转发。

        与 webui 单活跃 /ws 完全独立：多连接并存，每连接绑定一个工作空间，
        回合输出按发起连接路由（TurnStream.route / EndRoute.channel_id），
        互不串扰也不顶替 webui。同空间可与 Web/手机视图并存，也允许多条
        attach 并存（共享会话；谁发消息，输出就定向回谁）。
        """
        ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30.0)
        await ws.prepare(request)
        try:
            self._check_token(request)
        except web.HTTPUnauthorized:
            await ws.close(code=4001, message=b"Unauthorized")
            return ws

        # 首条消息握手：{"type":"attach","workspace":<name-or-id>}。
        conn_id = uuid.uuid4().hex
        workspace_id: str | None = None
        try:
            first = await ws.receive_json(timeout=15)
            if not isinstance(first, dict) or first.get("type") != "attach":
                await self._send_error(ws, "首条消息必须是 attach 握手")
                await ws.close(code=4002, message=b"Bad handshake")
                return ws
            wanted = str(first.get("workspace") or "").strip()
            if not wanted:
                await self._send_error(ws, "attach 握手缺少 workspace")
                await ws.close(code=4002, message=b"Bad handshake")
                return ws

            if self.root.workspace_manager is None:
                await self._send_error(ws, "工作空间管理器未就绪")
                await ws.close(code=4003, message=b"Unavailable")
                return ws
            entry = self.root.workspace_manager.registry.resolve_name_or_id(wanted)
            if entry is None:
                # 外挂进程刚 ensure_workspace 登记的新空间只落在注册表文件，主进程
                # 内存版（启动时 load）尚未感知——重新 load 一次再查，让「在未登记
                # 目录裸 coara 自动外挂」也能成立。
                registry = self.root.workspace_manager.registry
                with contextlib.suppress(Exception):
                    registry.load()
                entry = registry.resolve_name_or_id(wanted)
            if entry is None:
                await self._send_error(ws, f"未知工作空间：{wanted}")
                await ws.close(code=4004, message=b"Unknown workspace")
                return ws
            workspace_id = entry.id

            # 各端视图独立：CLI attach / Web / 手机可同挂一空间；同空间会话共享、
            # turn 串行；多条 attach 各有 conn_id，输出按发起连接回投。
            conn = await self.attach_registry.register(ws, conn_id, workspace_id)
            if conn is None:
                await self._send_error(ws, f"工作空间 {entry.name} 接入失败")
                await ws.close(code=4003, message=b"Unavailable")
                return ws
            # 握手只占本连接 pin + ensure session，不改全局 cli view。
            # 多 attach 并存时后连不得覆盖前连的 view，也不得误触发 stale 重开清历史。
            # 用户显式 /ws switch 仍走命令层 set_view_workspace(cli)。
        except Exception as exc:
            logger.debug(f"Attach handshake failed: {exc}")
            with contextlib.suppress(Exception):
                await ws.close(code=4002, message=b"Bad handshake")
            return ws

        try:
            # 确认该空间 session 存在（惰性创建），握手确认带 workspace 名 + session id。
            session = self.root._sessions.get(workspace_id)
            if session is None:
                session = await self.root.ensure_workspace_session(entry)
            # attached 帧：客户端建 RootShim 需要的完整快照（会话/模型/工具/
            # 技能/工作空间列表/用量/上下文窗口/回合来源），对齐原主 CLI
            # 状态栏数据源（chat_runner 就绪横幅 + spinner bottom_toolbar）。
            coara = session.coara
            await ws.send_str(
                json.dumps(
                    self._build_attach_attached_frame(entry, coara, channel_id=conn_id),
                    ensure_ascii=False,
                )
            )
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await self._send_error(ws, "Invalid JSON")
                        continue
                    # resume_connection 重键后旧 conn_id 已删：按 ws 反查当前键。
                    current_id = next(
                        (cid for cid, c in self.attach_registry._connections.items() if c.ws is ws),
                        conn_id,
                    )
                    await self._handle_attach_message(data, ws, current_id)
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"Attach WebSocket error: {ws.exception()}")
                    break
        except ClientConnectionResetError:
            pass
        finally:
            # 断连（含异常掉线）即取消在飞回合、注销跟话通道、释放工作空间占用，不留残留。
            # conn_id 可能已被 resume_connection 重键：按 ws 反查当前键收口。
            current_id = next(
                (cid for cid, c in self.attach_registry._connections.items() if c.ws is ws),
                conn_id,
            )
            tasks = self._chat_tasks.pop(current_id, None) or set()
            for t in tasks:
                t.cancel()
            end_registry = getattr(self.root, "end_registry", None)
            if end_registry is not None:
                # 有意 getattr 防御：跟话表惰性创建（512 行起），从未建过跟话的连接没有该属性
                for sess_id, sndr in getattr(self, "_followup_senders", {}).pop(current_id, []):
                    end_registry.unregister("cli-attached", sndr, sess_id)
            # 断连挂起不取消：清本端登记，挂起的审批帧按 conn_id 留着——resume 二次
            # 握手换回同一 conn_id 后经 redeliver_pending 重发（与 web
            # mark_connection_disconnected 对称）。审批状态机由 ApprovalCenter 的
            # 超时/abort 收口，此处不取消 future。
            with contextlib.suppress(Exception):
                self.attach_interaction_channel.mark_connection_disconnected(current_id)
            await self.attach_registry.unregister(current_id)
            logger.info(f"attach connection closed: conn={current_id} workspace={workspace_id}")
        return ws

    def _build_attach_attached_frame(self, entry: Any, coara: Any, *, channel_id: str = "") -> dict[str, Any]:
        """attached 握手帧：客户端建 RootShim 的完整快照。

        数据源对齐原主 CLI（chat_runner 就绪横幅 + spinner bottom_toolbar）：
        工具/技能数、provider/model、计划模式、用量、上下文窗口、工作空间列表。
        ``channel_id`` 为本连接 id——同空间多 CLI 用它挡它端 spinner/活动树。
        全部字段尽力而为——替身/早期状态下缺省空值，不阻塞握手。
        """
        frame: dict[str, Any] = {
            "type": "attached",
            "workspace": entry.name,
            "workspace_id": entry.id,
            "session": coara.session_id,
            "provider": str(getattr(coara, "provider_name", "") or ""),
            "model": str(getattr(coara, "model_name", "") or ""),
            # 与旧字段同义的新名（客户端 RootShim 用），旧字段保留不删。
            "session_id": str(getattr(coara, "session_id", "") or ""),
            "workspace_dir": str(getattr(coara, "workspace_dir", "") or ""),
            "coara_id": str(
                getattr(coara, "coara_id", "") or getattr(getattr(coara, "identity", None), "coara_id", "") or ""
            ),
            "agent_name": str(getattr(getattr(coara, "identity", None), "name", "") or ""),
            "provider_name": str(getattr(coara, "provider_name", "") or ""),
            "model_name": str(getattr(coara, "model_name", "") or ""),
            "is_plan_mode": False,
            "tools_count": 0,
            "skills_count": len(getattr(coara, "_skills", []) or []),
            "active_name": "",
            "foreground_session_id": str(getattr(self.root, "_foreground_session_id", "") or ""),
            "workspaces": [],
            "usage": {},
            "context_window": 0,
            "turn_source": str(getattr(coara, "_active_turn_source", "") or ""),
            "provider_has_key": self._provider_has_key(coara),
            "service_desks": [],
            "channel_id": str(channel_id or "").strip(),
        }
        try:
            from src.coara.service_desk import list_service_desks

            frame["service_desks"] = list_service_desks(self.root)
        except Exception:
            logger.debug("attach snapshot: service_desks listing failed", exc_info=True)
        try:
            is_plan = coara.is_plan_mode() if callable(getattr(coara, "is_plan_mode", None)) else False
            frame["is_plan_mode"] = bool(is_plan)
        except Exception:
            logger.debug("attach snapshot: is_plan_mode probe failed", exc_info=True)
        try:
            tm = getattr(coara, "_tool_manager", None)
            if tm is not None:
                visible = tm.get_visible_tool_names(getattr(coara.identity, "is_owner_context", False))
                frame["tools_count"] = len(visible)
        except Exception:
            logger.debug("attach snapshot: tools_count probe failed", exc_info=True)
        # 工作空间列表（名↔路径映射）+ active_name。先重读注册表文件——
        # 外挂进程 ensure_workspace 刚登记的空间可能尚未进主进程内存版。
        wm = getattr(self.root, "workspace_manager", None)
        if wm is not None:
            try:
                with contextlib.suppress(Exception):
                    wm.registry.load()
                workspaces = []
                for ws_entry in wm.registry.list_active():
                    try:
                        workspaces.append(
                            {
                                "id": str(getattr(ws_entry, "id", "") or ""),
                                "name": str(getattr(ws_entry, "name", "") or ""),
                                "path": str(ws_entry.resolved_path()),
                            }
                        )
                    except Exception:
                        # 单条条目损坏不应拖垮整表，但损坏本身是数据信号
                        logger.warning(
                            "attach snapshot: workspace entry serialization failed, entry skipped", exc_info=True
                        )
                        continue
                frame["workspaces"] = workspaces
                # 本连接 pin 的空间名（勿用全局 cli view，多 attach 时会错）
                frame["active_name"] = str(getattr(entry, "name", "") or "")
            except Exception:
                logger.warning(
                    "attach snapshot: workspace list rebuild failed, workspaces frame left empty", exc_info=True
                )
        # usage 快照 + context_window：与 web 状态栏同一口径（_foreground_context_usage）。
        try:
            usage_ctx = self._attach_context_usage(coara)
            frame["usage"] = usage_ctx.get("usage", {})
            frame["context_window"] = int(usage_ctx.get("context_window") or 0)
        except Exception:
            logger.debug("attach snapshot: usage snapshot failed", exc_info=True)
        return frame

    @staticmethod
    def _provider_has_key(coara: Any) -> bool:
        """内核权威：目标空间当前 provider 是否持可用 API key。

        attach 客户端本地探测不可靠（外挂进程没 load 内核 .env / providers.yaml
        的 inline key），状态栏「/model 添加 provider」引导须以此为准。registered
        provider 实例的 api_key 已合并 yaml inline + env 解析，非空即可用。
        """
        try:
            provider_obj = getattr(coara, "provider", None)
            if provider_obj is None:
                return False
            return bool(str(getattr(provider_obj, "api_key", "") or "").strip())
        except Exception:
            return False

    def _attach_context_usage(self, coara: Any) -> dict[str, Any]:
        """pin 空间 coara 的用量/上下文窗口（口径同 _foreground_context_usage）。

        usage 发**原始** provider usage dict（input_tokens/cached_tokens 等），
        与 llm_turn_complete 事件同格式——客户端 _UsageSnapshotMirror 与 spinner
        用 total_prompt_tokens 解析，摊平成 context_tokens 会读不出 input_tokens
        导致 context 恒 0。cache_hit_ratio 单列（snapshot 的累计口径）。
        """
        hit_ratio: float | None = None
        raw_usage: dict[str, Any] = {}
        snap = getattr(coara, "_llm_usage_snapshot", None)
        if snap is not None:
            if getattr(snap, "has_reported_input", False):
                raw_usage = dict(getattr(snap, "usage", None) or {})
            try:
                hit_ratio = snap.cache_hit_ratio
            except Exception:
                hit_ratio = None
        ctx_window = 0
        provider_obj = getattr(coara, "provider", None)
        if provider_obj is not None:
            try:
                ctx_window = int(provider_obj.get_context_window(getattr(coara, "model_name", None)) or 0)
            except Exception:
                ctx_window = 0
        return {"usage": {**raw_usage, "cache_hit_ratio": hit_ratio}, "context_window": ctx_window}

    async def _handle_attach_message(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """路由单条 attach WS 消息。chat 后台任务转发，ping 即时回。

        conn_id 可能已被 resume_connection 重键（二次握手换回旧 id）：后续
        一律从连接对象取当前 id，不用入参的词法初值，保证 finally 收口、
        chat tasks、followup senders 记账对得上。"""
        conn = self.attach_registry._connections.get(conn_id)
        if conn is None:
            await self._send_error(ws, "连接未注册")
            return
        workspace_id = conn.workspace_id
        msg_type = data.get("type", "")
        if msg_type == "attach":
            # 重连二次握手：换回上次连接的 conn_id——在飞回合的 sender 与该 id 绑定，
            # 携带它既让定向路由续跑，也让回放能精确定位本连接的回合流。
            resume_id = str(data.get("resume") or "").strip()
            if resume_id:
                await self.attach_registry.resume_connection(conn_id, resume_id)
            await self._replay_attach_turns(ws, conn)
            # 重连重发挂起的审批提示（与 web redeliver_pending 对称）：断连不取消
            # pending，resume 换回原 conn_id 后按同一 approval_id 重发，CLI 答复照常
            # 经 approval_reply → ApprovalCenter.resolve 收敛。已终态的帧不重发。
            try:
                await self.attach_interaction_channel.redeliver_pending(conn.conn_id)
            except Exception as exc:
                logger.debug(f"attach redeliver pending prompts failed: {exc}")
            return
        # resume 后 conn.conn_id 已是新 id；用它路由，保证消息处理与 finally 同键。
        conn_id = conn.conn_id
        if msg_type == "chat":
            self.root.record_user_activity(workspace_id=workspace_id)
            task = asyncio.create_task(self._handle_attach_chat(data, ws, conn_id, workspace_id))
            self._chat_tasks.setdefault(conn_id, set()).add(task)

            def _discard_chat_task(t: Any, cid: str = conn_id) -> None:
                self._chat_tasks.get(cid, set()).discard(t)

            task.add_done_callback(_discard_chat_task)
        elif msg_type == "command":
            # 斜杠命令：全量透传统一命令层（target_coara=pin 空间），不再限白名单。
            self._spawn_bg_task(self._handle_attach_command(data, ws, conn_id))
        elif msg_type == "interrupt":
            reason = str(data.get("reason") or "user_stop")
            # 客户端细粒度来源透传（cli_prompt_ctrl_c / cli_sigint_* 等）；
            # 缺省回落 attach_client（未带 source 的调用方）。
            source = str(data.get("source") or "").strip() or "attach_client"
            session = self.root._sessions.get(workspace_id)
            if session is not None:
                session.coara.interrupt_current_turn(reason, interrupt_source=source)
        elif msg_type == "approval_reply":
            # 回执统一进 ApprovalCenter 做幂等终态转换；通道只是哑管道。
            # 不回显确认帧——工具层已按结果推进，CLI 端本地已关 modal。
            from src.coara.approval_center import get_approval_center

            approval_id = str(data.get("approval_id") or "")
            if approval_id:
                get_approval_center().resolve(
                    approval_id,
                    approved=bool(data.get("approved")),
                    resolved_by="cli-attached",
                    actor=conn_id,
                )
        elif msg_type == "continuation":
            # mid-turn 跟话：仅当回合进行中才入接续队列（与来自哪端无关）。
            # 无活跃回合时拒绝——否则会把「新回合」误当成接续。
            session = self.root._sessions.get(workspace_id)
            if session is None:
                await self._send_error(ws, "工作空间会话未就绪")
                return
            if not session.coara.has_active_turn():
                await self._send_error(ws, "当前无进行中的回合，请直接发送消息")
                return
            text = str(data.get("text") or "")
            if not text.strip() and not data.get("image_blocks"):
                await self._send_error(ws, "Empty continuation")
                return
            # turn 给 continuation_input_received 打上本连接 channel_id，
            # 避免同空间另一 CLI 把跟话镜像进自己的排队/spinner。
            async with turn(
                "cli-attached",
                channel_id=conn_id,
                send_text=None,
                interaction_channel=self.attach_interaction_channel.for_connection(conn_id),
            ):
                session.coara.submit_continuation_input(
                    text,
                    image_blocks=data.get("image_blocks"),
                    source="cli-attached",
                )
            # 跟话 user_message + 段归属正文：正式 TurnStream（与开局 chat 同形），
            # 禁止直推 WS 旁路顶掉本回合 sender（会丢 buffer/重连回放）。
            end_registry = getattr(self.root, "end_registry", None)
            if end_registry is not None:
                # 跟话流没有自己的回合协程，也就没有 finally 能收尾——补上 turn_end
                # 订阅（幂等）。缺了它下面前缀的流 done 恒 False（见回调 docstring）。
                self._ensure_attach_turn_end_subscription()
                _sess_id = str(getattr(session.coara, "session_id", "") or "")
                _turn_id = str(getattr(getattr(session.coara, "_active_turn", None), "turn_id", "") or "")
                _target_stream: TurnStream | None = None
                # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_turns 可能不存在
                for _s in getattr(self, "_turns", {}).values():
                    if getattr(_s, "done", True):
                        continue
                    if str(getattr(_s, "source", "") or "") != "cli-attached":
                        continue
                    ch = str(getattr(getattr(_s, "route", None), "channel_id", "") or "")
                    if ch and ch != conn_id:
                        continue
                    if _turn_id and str(getattr(_s, "turn_id", "") or "") == _turn_id:
                        _target_stream = _s
                        break
                    if _target_stream is None:
                        _target_stream = _s

                created = False
                if _target_stream is None:
                    old_sender = end_registry.sender_for("cli-attached", _sess_id)
                    if old_sender is not None:
                        end_registry.unregister("cli-attached", old_sender, _sess_id)
                    _follow_tid = _turn_id or uuid.uuid4().hex
                    _target_stream = TurnStream(
                        _follow_tid,
                        "cli-attached",
                        "root",
                        cast(Any, self),
                        channel_id=conn_id,
                        session_id=_sess_id,
                    )
                    self._turns[f"followup-attach-{_follow_tid}"] = _target_stream
                    created = True

                _target_stream.emit_user_message(text)

                # 本连接已有指向回合流的通道则不覆盖（避免顶掉开局 sender 身份，
                # 导致回合 finally 注销对不上、或后续回合被旧闭包霸占）。
                existing_ch = end_registry.sender_for_channel("cli-attached", _sess_id, conn_id)
                if created or existing_ch is None:
                    stream_for_sender: TurnStream = _target_stream

                    def _attach_stream_sender(frame: dict) -> None:
                        mapped = _attach_output_frame(frame)
                        if mapped is None:
                            return
                        kind, payload = mapped
                        stream_for_sender.emit(kind, **payload)

                    _attach_stream_sender._end_channel_id = conn_id  # type: ignore[attr-defined]
                    end_registry.register("cli-attached", _attach_stream_sender, _sess_id)
                    if not hasattr(self, "_followup_senders"):
                        self._followup_senders: dict[str, list[tuple[str, Any]]] = {}
                    self._followup_senders.setdefault(conn_id, []).append((_sess_id, _attach_stream_sender))
            await ws.send_str(json.dumps({"type": "continuation_accepted"}, ensure_ascii=False))
        elif msg_type == "drain_continuation":
            session = self.root._sessions.get(workspace_id)
            items = session.coara.drain_continuation_inputs() if session is not None else []
            payload = [
                {
                    "text": str(getattr(it, "text", "") or ""),
                    "image_blocks": getattr(it, "image_blocks", None),
                    "source": str(getattr(it, "source", "") or ""),
                }
                for it in items
            ]
            await ws.send_str(json.dumps({"type": "continuation_drained", "items": payload}, ensure_ascii=False))
        elif msg_type == "cancel_continuation":
            session = self.root._sessions.get(workspace_id)
            cancelled = False
            if session is not None:
                cancelled = self._cancel_last_continuation(session.coara, text=str(data.get("text") or ""))
            await ws.send_str(
                json.dumps({"type": "continuation_cancelled", "cancelled": cancelled}, ensure_ascii=False)
            )
        elif msg_type == "pending_report":
            self._spawn_bg_task(self._handle_attach_pending_report(data, ws))
        elif msg_type == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
        else:
            await self._send_error(ws, f"Unknown message type: {msg_type}")

    @staticmethod
    def _cancel_last_continuation(coara: Any, *, text: str = "") -> bool:
        """撤回缓冲队列尾部一条跟话（未注入回合才有效）。

        带 text 时按文本精确删除队尾起第一条匹配项（与 CLI Esc 撤回的显示
        语义对齐：队尾最新一条用户跟话，系统注入跳过）；空 text 回落旧的
        盲 pop 队尾（兼容旧客户端）。已 drain/已注入的跟话无法撤回。
        """
        queue = getattr(coara, "_continuation_inputs", None)
        if not isinstance(queue, list) or not queue:
            return False
        target = str(text or "").strip()
        if target:
            from src.core.message_tags import is_preformatted_injection

            for index in range(len(queue) - 1, -1, -1):
                item_text = str(getattr(queue[index], "text", queue[index]) or "")
                if is_preformatted_injection(item_text):
                    continue
                if item_text.strip() == target:
                    del queue[index]
                    break
            else:
                return False
        else:
            queue.pop()
        event = getattr(coara, "_continuation_event", None)
        if not queue and event is not None:
            with contextlib.suppress(Exception):
                event.clear()
        return True

    async def _handle_attach_pending_report(self, data: dict[str, Any], ws: web.WebSocketResponse) -> None:
        """待提交报告描述拦截：try_consume_pending_report_async。

        未命中（无待提交报告）返回 consumed=False，客户端按普通 chat 走 LLM。
        """
        text = str(data.get("text") or "")
        from src.coara.commands.report import try_consume_pending_report_async

        try:
            result = await try_consume_pending_report_async(self.root, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("attach pending_report error")
            await self._send_error(ws, f"pending_report 处理失败：{exc}")
            return
        if result is None:
            await ws.send_str(json.dumps({"type": "pending_report_result", "consumed": False}, ensure_ascii=False))
            return
        await ws.send_str(
            json.dumps(
                {
                    "type": "pending_report_result",
                    "consumed": True,
                    "result": {
                        "output": result.output,
                        "action": result.action,
                        "data": result.data,
                        "exit_session": result.exit_session,
                    },
                },
                ensure_ascii=False,
            )
        )

    def _ensure_attach_turn_end_subscription(self) -> None:
        """惰性订阅内核 ``turn_end``——attach 跟话流的唯一收尾时机。

        只在真的建过跟话流时才装（订阅随 WebServer 生命周期，经 ``_subscriptions``
        在 stop 时统一注销），重复调用幂等；event_bus 缺席（替身/早期状态）时静默。
        """
        # 有意 getattr 防御：句柄惰性创建（首装于 629 行）；__new__ 测试夹具也无 __init__
        existing = getattr(self, "_attach_turn_end_sub", None)
        if existing is not None:
            # stop() 已把订阅从 _subscriptions 里清掉（服务重启）：旧句柄失效，重装。
            # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_subscriptions 可能不存在
            tracked = getattr(self, "_subscriptions", None)
            if not isinstance(tracked, list) or existing in tracked:
                return
            self._attach_turn_end_sub = None
        event_bus = getattr(getattr(self, "root", None), "event_bus", None)
        if event_bus is None or not callable(getattr(event_bus, "subscribe", None)):
            return
        try:
            sub = event_bus.subscribe(self._on_attach_followup_turn_end, topic="turn_end")
        except Exception:  # noqa: BLE001 — 订阅失败不能拖垮跟话本身
            logger.debug("attach turn_end subscribe failed", exc_info=True)
            return
        self._attach_turn_end_sub = sub
        # 有意 getattr 防御：__new__ 测试夹具不跑 __init__，_subscriptions 可能不存在
        subscriptions = getattr(self, "_subscriptions", None)
        if isinstance(subscriptions, list):
            subscriptions.append(sub)

    def _on_attach_followup_turn_end(self, event: Any) -> None:
        """它端回合结束 → 收尾本连接未结束的跟话流（``followup-attach-*``）。

        跟话流没有自己的回合协程（它参与的那个回合是别端跑起来的），建立它的分支
        也就没有 finally 能收尾：不补这一手，``done`` 恒为 False——回收不掉、每次
        重连都被当「在飞回合」回放、还长期留在活跃流候选里可能被误当投递目标。

        口径与 web 侧 ``_on_web_followup_turn_end`` 一致：先按 ``turn_id`` 精确
        命中；未命中再按 ``session_id`` 收尾该会话所有未结束的跟话流（注入瞬间
        快照的 turn_id 与收尾事件可能不一致：空快照、接续 leftover 换号等）。

        只碰本前缀的跟话流，开局回合流（键=turn_id，自己 finally 收尾）不动；
        ``done`` 的流不再进候选，故重复事件天然幂等；子智能体/维护 agent 的回合
        （``origin_scope`` 非 main_loop）与主会话跟话流无关，直接跳过。
        """
        payload = getattr(event, "payload", None) or {}
        if str(payload.get("origin_scope") or "main_loop") not in ("main_loop", ""):
            return
        turn_id = str(payload.get("turn_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not turn_id and not session_id:
            return
        reason = str(payload.get("reason") or "complete") or "complete"

        exact: list[TurnStream] = []
        by_session: list[TurnStream] = []
        for key, stream in list(self._turns.items()):
            if not key.startswith(_FOLLOWUP_STREAM_PREFIX) or getattr(stream, "done", True):
                continue
            if turn_id and str(getattr(stream, "turn_id", "") or "") == turn_id:
                exact.append(stream)
            elif session_id and str(getattr(stream, "session_id", "") or "") == session_id:
                by_session.append(stream)
        targets = exact or by_session
        if not targets:
            return
        for stream in targets:
            stream.emit("turn_end", reason=reason)
            stream.finish()
        self._gc_finished_turns()

    async def _replay_attach_turns(self, ws: web.WebSocketResponse, conn: Any) -> None:
        """重连后回放：本连接 pin 空间在飞回合的 TurnStream buffer 逐帧重发。

        与 web 回放（web_server.py）同构：帧带 ``replayed: true`` 标记，客户端
        按 (turn_id, seq) 去重后接回正文流。回放本连接的回合（resume 后 conn_id
        已换回旧 id）；握手帧不带 resume 时按 pin 空间兜底，兼顾客端未及时领回。
        """
        workspace_id = conn.workspace_id
        conn_id = conn.conn_id
        for stream in list(self._turns.values()):
            if getattr(stream, "done", True):
                continue
            if str(getattr(stream, "source", "") or "") != "cli-attached":
                continue
            stream_ch = str(getattr(getattr(stream, "route", None), "channel_id", "") or "")
            if stream_ch and stream_ch != conn_id:
                continue
            if not stream_ch:
                # 未标记 channel 的回合按 pin 空间归属兜底（frame 携带后新握手前）。
                stream_ws = str(getattr(stream, "workspace_id", "") or "")
                if stream_ws and stream_ws != workspace_id:
                    continue
            for frame in stream.replay():
                replayed = dict(frame)
                replayed["replayed"] = True
                try:
                    await ws.send_str(json.dumps(replayed, ensure_ascii=False))
                except Exception:
                    logger.debug("attach replay send failed", exc_info=True)
                    return

    async def _handle_attach_chat(
        self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str, workspace_id: str
    ) -> None:
        """外挂 CLI 单回合：默认 pin 空间；行首 ``@服务台`` 则投递到服务台会话（view/pin 不换）。"""
        text = str(data.get("text", "")).strip()
        image_blocks = data.get("image_blocks")
        if image_blocks is not None and not isinstance(image_blocks, list):
            image_blocks = None
        if not text and not image_blocks:
            await self._send_error(ws, "Empty message")
            return

        session = self.root._sessions.get(workspace_id)
        if session is None:
            entry = self.root.workspace_manager.registry.resolve_name_or_id(workspace_id)
            if entry is None:
                await self._send_error(ws, "工作空间已不存在")
                return
            session = await self.root.ensure_workspace_session(entry)
        turn_coara = session.coara
        desk_label = ""

        from src.coara.service_desk import (
            ServiceDeskError,
            canonical_desk_name,
            parse_service_desk_at,
            resolve_service_desk_coara,
        )

        parsed = parse_service_desk_at(text) if text else None
        if parsed is not None:
            desk_name, body = parsed
            if not body and not image_blocks:
                await self._send_error(ws, f"@{desk_name} 后面写要说的话")
                return
            try:
                turn_coara = await resolve_service_desk_coara(self.root, desk_name, web_server=self)
            except ServiceDeskError as exc:
                await self._send_error(ws, str(exc))
                return
            text = body
            desk_label = canonical_desk_name(desk_name)

        turn_id = uuid.uuid4().hex
        source = "cli-attached"
        stream = TurnStream(turn_id, source, "root", cast(Any, self), channel_id=conn_id)
        stream.desk = desk_label
        stream.workspace_id = workspace_id
        stream.session_id = str(getattr(turn_coara, "session_id", "") or "")
        self._turns[turn_id] = stream
        stream.emit_user_message(str(data.get("text") or text or ""))
        stream.emit("turn_start")
        if turn_coara.has_active_turn():
            stream.emit("turn_queued")

        # 注册本回合显示通道到 EndRegistry：正文 chunk 由 base.py 统一路由，
        # 经 TurnStream（route=cli-attached+channel_id）定向投回本连接。多连接
        # 并存时按 (cli-attached, session_id) 键互不覆盖（pin 空间各占各的会话）。
        end_registry = getattr(self.root, "end_registry", None)
        _sess_id = str(getattr(turn_coara, "session_id", "") or "")
        sender: Any = None
        if end_registry is not None:

            def sender(frame: dict) -> None:
                # 帧型与归属由 _attach_output_frame 决定（kind 是唯一凭据）：
                # 主会话正文 → chunk；子智能体正文/报告 → subagent_chunk /
                # subagent_result（端侧不当正文渲染）；diff 展开传（不包 payload
                # 键，客户端 _feed_turn_frame → queue_diff_frame 直接读）。
                mapped = _attach_output_frame(frame)
                if mapped is None:
                    return
                kind, payload = mapped
                stream.emit(kind, **payload)

            # 打端内连接标：同空间多条 attach 并存时 EndRegistry 按 channel_id
            # 精确归位本连接，不再被 (source, session_id) 单槽互截。
            sender._end_channel_id = conn_id  # type: ignore[attr-defined]
            end_registry.register("cli-attached", sender, _sess_id)
        reason = "complete"
        error_message: str | None = None
        try:
            async with turn(
                source,
                channel_id=conn_id,
                send_text=None,
                interaction_channel=self.attach_interaction_channel.for_connection(conn_id),
            ):
                agen = turn_coara.process_message(
                    text,
                    trust_level="owner",
                    show_tool_summary=True,
                    image_blocks=image_blocks,
                    source=source,
                    turn_id=turn_id,
                )
                # 正文已由 EndRegistry 路由投递；循环只处理工具行（✓/✗ 不被
                # base 路由，仍经 yield 到达）：推 tool 帧供客户端渲染。
                async for chunk in agen:
                    if not chunk.strip():
                        continue
                    stripped = chunk.lstrip()
                    if stripped.startswith("✓") or stripped.startswith("✗"):
                        stream.emit("tool", text=chunk, ok=stripped.startswith("✓"))
        except asyncio.CancelledError:
            reason = "interrupted"
        except Exception as exc:
            reason = "error"
            logger.exception(f"Attach chat turn error: {exc}")
            from src.coara.turn_orchestrator import _user_facing_turn_error

            # 用户可见文案挂在 turn_end.message（及一条 chunk）上——客户端只认
            # turn_end，单独 emit("error") 会被忽略，最终变成 RuntimeError("error")。
            error_message = _user_facing_turn_error(exc)
            stream.emit("chunk", text=error_message)
        finally:
            if end_registry is not None and sender is not None:
                end_registry.unregister("cli-attached", sender, _sess_id)
            if reason == "error" and error_message:
                stream.emit("turn_end", reason=reason, message=error_message)
            else:
                stream.emit("turn_end", reason=reason)
            stream.finish()
            self._gc_finished_turns()

    # 召回原主 CLI 完整界面后，attach 命令面与主 CLI 等价：全量透传统一命令层，
    # 不再限白名单。target_coara 指向 pin 空间（命令层 resolve_target_coara 优先
    # 读它），跨端语义经 origin_source="cli-attached" 分流（同 web/matrix）。
    # /model 与 /new 保留 pin 空间专用实现（命令层版走前台/全局语义，attach 的
    # 空间不是前台）。

    async def _handle_attach_command(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """attach 的斜杠命令：全量透传统一命令层，target_coara=pin 空间。

        - /model  → _attach_switch_model（指定空间切模型；命令层版作用前台）
        - /new    → root.start_new_session_for_workspace（指定空间重开）
        - /ws switch → set_view_workspace(cli) + 本连接 rebind pin
        - 其余全部 → execute_command(target_coara=pin, origin_source=cli-attached)
        """
        raw = str(data.get("text", "")).strip()
        if not raw:
            await self._send_error(ws, "Empty command")
            return
        from src.coara.commands.registry import parse_command

        parsed = parse_command(raw)
        if parsed is None:
            await self._send_error(ws, "Not a command")
            return

        conn = self.attach_registry._connections.get(conn_id)
        if conn is None:
            await self._send_error(ws, "连接未注册")
            return
        workspace_id = conn.workspace_id
        session = self.root._sessions.get(workspace_id)
        if session is None:
            await self._send_error(ws, "工作空间会话未就绪")
            return
        coara = session.coara

        # attach 的交互通道（确认门复用审批原语：发给本连接的三端统一帧）。
        channel = self.attach_interaction_channel.for_connection(conn_id)

        # B 类会话配置确认门：model/new 是 attach 特判分支（绕过 execute_command），
        # 动作前必须单独过门——他端占着本会话时先让用户确认，别静默掐掉对方回合。
        if parsed.name in ("model", "new"):
            from src.coara.commands.registry import maybe_confirm_session_config

            parsed.target_coara = coara
            parsed.origin_source = "cli-attached"
            gate = await maybe_confirm_session_config(parsed, self.root, channel)
            if gate is not None:
                await self._send_attach_command_result(
                    ws,
                    output=gate.output,
                    action=gate.action,
                    data=gate.data,
                    exit_session=gate.exit_session,
                    request_id=str(data.get("request_id") or ""),
                )
                return

        if parsed.name == "model":
            output = await self._attach_switch_model(workspace_id, parsed)
            await self._send_attach_command_result(ws, output=output, request_id=str(data.get("request_id") or ""))
            return
        if parsed.name == "new":
            new_id = await self.root.start_new_session_for_workspace(
                workspace_id, interrupt_source="cli_attached_new_command"
            )
            await self._send_attach_command_result(
                ws,
                output=f"已开始新会话：`{new_id}`",
                action="new_session",
                data={"session_id": new_id},
                request_id=str(data.get("request_id") or ""),
            )
            return

        from src.ui import web_server as _web_server_mod

        result = await _web_server_mod.execute_command(
            self.root,
            raw,
            target_coara=coara,
            origin_source="cli-attached",
            interaction_channel=channel,
        )
        if result is None:
            await self._send_error(ws, "Not a command")
            return
        # /ws switch：command 已改 cli view；本连接必须能占用目标空间，否则回滚 view，
        # 避免「chrome 已是新空间、pin/聊天仍在旧空间」分叉。
        if getattr(result, "action", None) == "switch_workspace":
            data_out = getattr(result, "data", None) or {}
            new_wid = str(data_out.get("workspace_id") or "").strip()
            if new_wid and new_wid != workspace_id:
                ok = await self.attach_registry.rebind_workspace(conn_id, new_wid)
                if not ok:
                    prev_entry = None
                    if self.root.workspace_manager is not None:
                        prev_entry = self.root.workspace_manager.registry.get_by_id(workspace_id)
                    if prev_entry is not None:
                        await self.root.set_view_workspace(
                            "cli",
                            prev_entry.name,
                            republish_runtime=True,
                            emit_event=True,
                        )
                    await self._send_error(ws, "无法占用目标工作空间（可能已被其它外挂占用）")
                    return
                entry = None
                if self.root.workspace_manager is not None:
                    entry = self.root.workspace_manager.registry.get_by_id(new_wid)
                if entry is not None:
                    await self.root.ensure_workspace_session(entry)
        await self._send_attach_command_result(
            ws,
            output=result.output,
            action=result.action,
            data=result.data,
            exit_session=result.exit_session,
            request_id=str(data.get("request_id") or ""),
        )

    @staticmethod
    async def _send_attach_command_result(
        ws: web.WebSocketResponse,
        *,
        output: str | None,
        action: str | None = None,
        data: dict[str, Any] | None = None,
        exit_session: bool = False,
        request_id: str = "",
    ) -> None:
        """command_result 帧统一出口：字段对齐 CommandResult 供客户端渲染。

        ``request_id`` 回显客户端请求 id——客户端据此精确派发给发起等待者
        （缺省空串 = 旧客户端/服务端，客户端保留 FIFO 兜底）。
        """
        await ws.send_str(
            json.dumps(
                {
                    "type": "command_result",
                    "request_id": request_id,
                    "result": {
                        "output": output,
                        "action": action,
                        "data": data or {},
                        "exit_session": exit_session,
                    },
                },
                ensure_ascii=False,
            )
        )

    async def _attach_switch_model(self, workspace_id: str, parsed: Any) -> str:
        """执行 attach 的 /model：无参列出模型（标注当前空间所用），带参切换该空间模型。"""
        from src.core.config import config_manager
        from src.core.errors import ConfigError, ProviderNotFoundError
        from src.llm.model_catalog import (
            list_model_choices,
            resolve_model_by_index,
            resolve_model_selection,
        )

        session = self.root._sessions.get(workspace_id)
        if session is None:
            return "工作空间会话未就绪"
        coara = session.coara
        current = f"{coara.provider_name}/{coara.model_name}"

        # /model <arg> [<model>]：单参数命令的 value 在 parts[1]（parse_command 的
        # value 是第 3 个 token，单参时为 None）。与主 CLI handle_model 一致用 parts。
        raw_parts = [p for p in parsed.parts[1:] if not p.startswith("--")]
        arg = raw_parts[0] if raw_parts else None
        model_arg = raw_parts[1] if len(raw_parts) > 1 else None
        if not arg:
            choices = list_model_choices(config_manager)
            lines = [f"可用模型（当前 {current}）："]
            for idx, choice in enumerate(choices, start=1):
                mark = "*" if choice.key == current else " "
                lines.append(f"{mark} {idx}. {choice.label}")
            lines.append("用法：/model <序号|模型名|供应商/模型>（绑定当前工作空间）")
            return "\n".join(lines)
        try:
            if arg.isdigit():
                provider_name, model_name = resolve_model_by_index(config_manager, int(arg))
            else:
                provider_name, model_name = resolve_model_selection(config_manager, arg, model_arg)
            applied_provider, applied_model = self.root.switch_llm_for_workspace(
                workspace_id, provider_name, model_name, origin_source="cli-attached"
            )
        except (ConfigError, ProviderNotFoundError) as exc:
            return f"无法切换模型：{exc}"
        return f"已切换当前工作空间模型：`{applied_provider}/{applied_model}`"
