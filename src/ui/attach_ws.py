"""外挂 CLI（``coara attach``）WebSocket 端点 mixin — 从 ``web_server.py`` 拆出 （纯结构搬迁，行为不变）"""

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

# 的旁白/正文与最终报告，不是前台主会话正文。出 attach 帧时必须保留这个归属。
_SUBAGENT_FRAME_KINDS = frozenset({"subagent_chunk", "subagent_result"})

# 跟话流在 ``self._turns`` 里的键前缀。
_FOLLOWUP_STREAM_PREFIX = "followup-attach-"


def _attach_output_frame(frame: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """内核输出帧 → attach 端 ``(帧型, 载荷)``；``None`` = 本端不显示这一帧"""
    kind = str(frame.get("kind") or "")
    if kind == "tool":
        # 子智能体工具行不进主会话流：过程行由活动树/折叠块通道呈现 （parent_tool_call_id 是归属凭据），
        # 投进来会当主会话工具行多一行。
        if str(frame.get("parent_tool_call_id") or ""):
            return None
        from src.coara.display import format_tool_line_bullet, strip_tool_error_suffix

        label = strip_tool_error_suffix(str(frame.get("text") or "").strip())
        if not label:
            return None
        ok = not bool(frame.get("is_error"))
        # 成败都用 •；失败靠帧字段 is_error 让 CLI 标红（不用 × / 隐式字符）
        return (
            "tool",
            {
                "text": f"{format_tool_line_bullet()} {label}\n",
                "ok": ok,
                "is_error": not ok,
                "tool_name": str(frame.get("tool_name") or ""),
                "tool_call_id": str(frame.get("tool_call_id") or ""),
            },
        )
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
    # 正文折进折叠块——与 web 出口（_emit_end_frame）同一把尺。
    from src.core.message_tags import is_preformatted_injection

    if is_preformatted_injection(text):
        return None
    parent_id = str(frame.get("parent_tool_call_id") or "")
    if kind in _SUBAGENT_FRAME_KINDS:
        # 子智能体正文/结果的父标识：parent_tool_call_id 优先，回退 tool_call_id
        # （正文帧两者同值；工具帧只在 parent_tool_call_id 上带父）
        fold_id = parent_id or str(frame.get("tool_call_id") or "")
        if not fold_id:
            # 缺父标识的子智能体帧：投出去只会当主会话正文显示，丢掉（异常态，web 出口同尺）
            return None
        return (
            kind,
            {
                "text": text,
                "agent_kind": "subagent",
                "tool_call_id": fold_id,
                "coara_id": str(frame.get("coara_id") or ""),
                "subagent_id": str(frame.get("subagent_id") or ""),
            },
        )
    if parent_id:
        # 上游分叉：带父标识的普通正文同样折进发起它的 delegate 折叠块，不进主滚动区
        return (
            "subagent_chunk",
            {"text": text, "agent_kind": "subagent", "tool_call_id": parent_id},
        )
    return "chunk", {"text": text}


class AttachWsHandlers(HandlerMixinBase):
    async def _handle_attach_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """外挂 CLI 接入：鉴权 → 解析目标工作空间 → 双向占用校验 → 对话转发"""
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

            # 各端视图独立：CLI attach / Web / 手机可同挂一空间；同空间会话共享、 turn 串行；多条 attach 各有 conn_id，
            # 输出按发起连接回投。
            conn = await self.attach_registry.register(ws, conn_id, workspace_id)
            if conn is None:
                await self._send_error(ws, f"工作空间 {entry.name} 接入失败")
                await ws.close(code=4003, message=b"Unavailable")
                return ws
            # 握手只占本连接 pin + ensure session，不改全局 cli view。
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
            # 技能/工作空间列表/用量/上下文窗口/回合来源）。
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
            # 断连（含异常掉线）即取消在飞回合、注销跟话通道、释放工作空间占用，不留残留。 conn_id 可能已被
            # resume_connection 重键：按 ws 反查当前键收口。
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
            # 超时/abort 收口，此处不取消 future。
            with contextlib.suppress(Exception):
                self.attach_interaction_channel.mark_connection_disconnected(current_id)
            await self.attach_registry.unregister(current_id)
            logger.info(f"attach connection closed: conn={current_id} workspace={workspace_id}")
        return ws

    def _build_attach_attached_frame(self, entry: Any, coara: Any, *, channel_id: str = "") -> dict[str, Any]:
        """attached 握手帧：客户端建 RootShim 的完整快照"""
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
            "turn_source": str(getattr(getattr(coara, "_segments", None), "source", "") or ""),
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
            frame["usage_cumulative"] = {
                k: v
                for k, v in usage_ctx.items()
                if k.startswith("cumulative_") or k in ("last_turn_partial", "estimated")
            }
        except Exception:
            logger.debug("attach snapshot: usage snapshot failed", exc_info=True)
        return frame

    @staticmethod
    def _provider_has_key(coara: Any) -> bool:
        """目标空间当前 provider 是否持可用 API key（占位符不算）。"""
        try:
            from src.core.api_keys import is_usable_api_key

            provider_obj = getattr(coara, "provider", None)
            if provider_obj is None:
                return False
            return is_usable_api_key(str(getattr(provider_obj, "api_key", "") or ""))
        except Exception:
            return False

    def _attach_context_usage(self, coara: Any) -> dict[str, Any]:
        """pin 空间 coara 的用量/上下文窗口（口径同 _foreground_context_usage）"""
        hit_ratio: float | None = None
        raw_usage: dict[str, Any] = {}
        cumulative: dict[str, Any] = {}
        snap = getattr(coara, "_llm_usage_snapshot", None)
        if snap is not None:
            if getattr(snap, "has_reported_input", False):
                raw_usage = dict(getattr(snap, "usage", None) or {})
            try:
                hit_ratio = snap.cache_hit_ratio
            except Exception:
                hit_ratio = None
            # 才补涨——attach 端应一接上就与内核同数。
            cumulative = {
                "cumulative_prompt_tokens": int(getattr(snap, "cumulative_prompt_tokens", 0) or 0),
                "cumulative_cache_read_tokens": int(getattr(snap, "cumulative_cache_read_tokens", 0) or 0),
                "cumulative_output_tokens": int(getattr(snap, "cumulative_output_tokens", 0) or 0),
                "cumulative_cost": float(getattr(snap, "cumulative_cost", 0.0) or 0.0),
                "last_turn_partial": bool(getattr(snap, "last_turn_partial", False)),
                "estimated": bool(getattr(snap, "estimated", False)),
            }
        ctx_window = 0
        provider_obj = getattr(coara, "provider", None)
        if provider_obj is not None:
            try:
                ctx_window = int(provider_obj.get_context_window(getattr(coara, "model_name", None)) or 0)
            except Exception:
                ctx_window = 0
        return {"usage": {**raw_usage, "cache_hit_ratio": hit_ratio}, "context_window": ctx_window, **cumulative}

    def _client_usage_snapshot(self, coara: Any) -> dict[str, Any]:
        """Shape accepted by attach client ``_apply_usage_snapshot`` (restore path)."""
        ctx = self._attach_context_usage(coara)
        usage = dict(ctx.get("usage") or {})
        hit = usage.pop("cache_hit_ratio", None)
        return {
            "usage": usage,
            "cache_hit_ratio": hit,
            "estimated": bool(ctx.get("estimated", False)),
            "cumulative_prompt_tokens": int(ctx.get("cumulative_prompt_tokens") or 0),
            "cumulative_cache_read_tokens": int(ctx.get("cumulative_cache_read_tokens") or 0),
            "cumulative_output_tokens": int(ctx.get("cumulative_output_tokens") or 0),
            "cumulative_cost": float(ctx.get("cumulative_cost") or 0.0),
            "last_turn_partial": bool(ctx.get("last_turn_partial", False)),
        }

    async def _handle_attach_message(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """路由单条 attach WS 消息"""
        conn = self.attach_registry._connections.get(conn_id)
        if conn is None:
            await self._send_error(ws, "连接未注册")
            return
        workspace_id = conn.workspace_id
        msg_type = data.get("type", "")
        if msg_type == "attach":
            # 重连二次握手：换回上次连接的 conn_id——在飞回合的 sender 与该 id 绑定， 携带它既让定向路由续跑，
            # 也让回放能精确定位本连接的回合流。
            resume_id = str(data.get("resume") or "").strip()
            if resume_id:
                await self.attach_registry.resume_connection(conn_id, resume_id)
            await self._replay_attach_turns(ws, conn)
            # 重连重发挂起的审批提示（与 web redeliver_pending 对称）：断连不取消
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
            # 客户端细粒度来源透传（cli_prompt_ctrl_c / cli_sigint_* 等）； 缺省回落 attach_client（未带 source
            # 的调用方）。
            source = str(data.get("source") or "").strip() or "attach_client"
            # 征求同意门要等用户在同一连接上回 approval_reply——接收循环不能 inline 等门
            # （等门期间读不到回执＝死锁，只能 300s 超时）。门与打断整体调为后台任务，接收循环立即腾出。
            task = asyncio.create_task(self._run_attach_interrupt(data, ws, conn_id, workspace_id, reason, source))
            with contextlib.suppress(Exception):
                self._bg_tasks.add(task)  # type: ignore[attr-defined]
                task.add_done_callback(self._bg_tasks.discard)  # type: ignore[attr-defined]
        elif msg_type == "approval_reply":
            # 回执统一进 ApprovalCenter 做幂等终态转换；通道只是哑管道。
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
            # mid-turn 跟话：仅当回合进行中才入接续队列（与来自哪端无关）。 无活跃回合时拒绝——否则会把「新回合」
            # 误当成接续。
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
            # turn 给 continuation_input_received 打上本连接 channel_id， 避免同空间另一 CLI
            # 把跟话镜像进自己的排队/spinner。
            async with turn(
                "cli-attached",
                channel_id=conn_id,
                send_text=None,
                interaction_channel=self.attach_interaction_channel.for_connection(conn_id),
            ):
                follow: dict[str, Any] = {
                    "image_blocks": data.get("image_blocks"),
                    "source": "cli-attached",
                }
                cid = str(data.get("client_msg_id") or "").strip()
                if cid:
                    follow["client_msg_id"] = cid
                session.coara.submit_continuation_input(text, **follow)
            # 跟话 user_message + 段归属正文：正式 TurnStream（与开局 chat 同形）， 禁止直推 WS 旁路顶掉本回合
            # sender（会丢 buffer/重连回放）。
            end_registry = getattr(self.root, "end_registry", None)
            if end_registry is not None:
                # 跟话流没有自己的回合协程，也就没有 finally 能收尾——补上 turn_end 订阅（幂等）。缺了它下面前缀的流
                # done 恒 False（见回调 docstring）。
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
                    # 只顶替「上一个跟话的通道」——判据以注册表的通道类型为准（与 web / 手机同一处）；
                    # 回合 sender 是别的协程的活性通道，身份不辨就顶替会让在跑回合的输出失去路由（V3）。
                    if end_registry.is_followup("cli-attached", _sess_id):
                        old_sender = end_registry.sender_for("cli-attached", _sess_id)
                        if old_sender is not None:
                            end_registry.unregister("cli-attached", old_sender, _sess_id)
                    _follow_tid = _turn_id or uuid.uuid4().hex
                    _follow_ws = str(
                        getattr(session.coara, "workspace_dir", "") or getattr(self, "workspace_dir", "") or ""
                    )
                    _target_stream = TurnStream(
                        _follow_tid,
                        "cli-attached",
                        "root",
                        cast(Any, self),
                        channel_id=conn_id,
                        session_id=_sess_id,
                        workspace_dir=_follow_ws,
                    )
                    self._turns[f"followup-attach-{_follow_tid}"] = _target_stream
                    created = True
                elif not getattr(_target_stream, "workspace_dir", ""):
                    # 旧流若漏打空间戳，跟话续用前补上，避免继续静默丢带
                    _target_stream.workspace_dir = str(
                        getattr(session.coara, "workspace_dir", "") or getattr(self, "workspace_dir", "") or ""
                    )

                _target_stream.emit_user_message(text)

                # CLI 表现为注入成功但 spinner/正文全被挡住。
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
                    # 跟话 sender 标记：回合结束时按它把失效的槽位收回（区别于回合 sender）。
                    _attach_stream_sender._end_followup = True  # type: ignore[attr-defined]
                    _attach_stream_sender._end_followup_stream = _target_stream  # type: ignore[attr-defined]
                    end_registry.register("cli-attached", _attach_stream_sender, _sess_id, kind="followup")
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
                    "client_msg_id": str(getattr(it, "client_msg_id", "") or ""),
                }
                for it in items
            ]
            await ws.send_str(json.dumps({"type": "continuation_drained", "items": payload}, ensure_ascii=False))
        elif msg_type == "cancel_continuation":
            session = self.root._sessions.get(workspace_id)
            cancelled = False
            if session is not None:
                cancelled = self._cancel_last_continuation(
                    session.coara,
                    text=str(data.get("text") or ""),
                    client_msg_id=str(data.get("client_msg_id") or ""),
                )
            await ws.send_str(
                json.dumps({"type": "continuation_cancelled", "cancelled": cancelled}, ensure_ascii=False)
            )
        elif msg_type == "pending_report":
            self._spawn_bg_task(self._handle_attach_pending_report(data, ws))
        elif msg_type == "ping":
            await ws.send_str(json.dumps({"type": "pong"}))
        else:
            await self._send_error(ws, f"Unknown message type: {msg_type}")

    async def _run_attach_interrupt(
        self,
        data: dict[str, Any],
        ws: web.WebSocketResponse,
        conn_id: str,
        workspace_id: str,
        reason: str,
        source: str,
    ) -> None:
        """interrupt 的后台执行体：征求同意门 → 获批打断并转移所有权（接收循环不阻塞）"""
        session = self.root._sessions.get(workspace_id)
        if session is None:
            logger.debug("attach interrupt: pinned workspace session unknown, skip")
            return
        # 征求同意门（用户裁决 2026-09-24）：它端回合在跑时打断先经审批征得同意；
        # 获批后打断端经段机制获得会话所有权（与输入同权）。
        from src.coara.commands.registry import confirm_cross_end_action

        approved = await confirm_cross_end_action(
            session.coara,
            origin_source="cli-attached",
            verb="打断当前回合",
            detail="打断后本端将获得该会话的所有权。",
            interaction_channel=self.attach_interaction_channel.for_connection(conn_id),
        )
        if not approved:
            await self._send_error(ws, "已取消打断（会话仍归属原端）")
            return
        session.coara.interrupt_current_turn(
            reason,
            interrupt_source=source,
            take_ownership_source="cli-attached",
            take_ownership_channel_id=conn_id,
        )

    @staticmethod
    def _cancel_last_continuation(coara: Any, *, text: str = "", client_msg_id: str = "") -> bool:
        """撤回缓冲队列里一条跟话（未注入回合才有效）"""
        queue = getattr(coara, "_continuation_inputs", None)
        if not isinstance(queue, list) or not queue:
            return False
        cid = str(client_msg_id or "").strip()
        if cid:
            for index in range(len(queue) - 1, -1, -1):
                item_id = str(getattr(queue[index], "client_msg_id", "") or "")
                if item_id == cid:
                    del queue[index]
                    break
            else:
                return False
        else:
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
        """待提交报告描述拦截：try_consume_pending_report_async。"""
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
        """惰性订阅内核 ``turn_end``——attach 跟话流的唯一收尾时机"""
        # 有意 getattr 防御：句柄惰性创建（首装于 629 行）；__new__ 测试夹具也无 __init__
        existing = getattr(self, "_attach_turn_end_sub", None)
        if existing is not None:
            # stop() 已把订阅从 _subscriptions 里清掉（服务重启）：旧句柄失效，重装。
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
        """它端回合结束 → 收尾本连接未结束的跟话流（``followup-attach-*``）"""
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
        # 跟话 sender 随回合结束收回槽位（V3）：其流已 finish，继续占着 (cli-attached, session)
        # 槽会让后续帧路由进死流。按 _end_followup 标识别、只收回自己的连接。
        end_registry = getattr(self.root, "end_registry", None)
        if end_registry is not None:
            for conn_id, entries in list(getattr(self, "_followup_senders", {}).items()):
                kept: list[tuple[str, Any]] = []
                for sess_id, sndr in entries:
                    follow_stream = getattr(sndr, "_end_followup_stream", None)
                    stale = getattr(sndr, "_end_followup", False) and (
                        follow_stream is None or getattr(follow_stream, "done", False)
                    )
                    if stale:
                        end_registry.unregister("cli-attached", sndr, sess_id)
                    else:
                        kept.append((sess_id, sndr))
                if kept:
                    self._followup_senders[conn_id] = kept
                else:
                    self._followup_senders.pop(conn_id, None)

    async def _replay_attach_turns(self, ws: web.WebSocketResponse, conn: Any) -> None:
        """重连后回放：本连接 pin 空间在飞回合的 TurnStream buffer 逐帧重发"""
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
        # 必须打 workspace_dir：缺省落带走 record_view_frame，无空间戳则整帧丢弃
        tape_ws = str(getattr(turn_coara, "workspace_dir", "") or getattr(self, "workspace_dir", "") or "")
        stream = TurnStream(
            turn_id,
            source,
            "root",
            cast(Any, self),
            channel_id=conn_id,
            session_id=str(getattr(turn_coara, "session_id", "") or ""),
            workspace_dir=tape_ws,
        )
        stream.desk = desk_label
        stream.workspace_id = workspace_id
        self._turns[turn_id] = stream
        stream.emit_user_message(str(data.get("text") or text or ""))
        stream.emit("turn_start")
        if turn_coara.has_active_turn():
            stream.emit("turn_queued")

        # 跟话 user_message + 段归属正文：正式 TurnStream（与开局 chat 同形）， 禁止直推 WS 旁路顶掉本回合 sender（会丢
        # buffer/重连回放）。
        end_registry = getattr(self.root, "end_registry", None)
        _sess_id = str(getattr(turn_coara, "session_id", "") or "")
        sender: Any = None
        if end_registry is not None:

            def sender(frame: dict) -> None:
                # 主会话正文 → chunk；子智能体正文/报告 → subagent_chunk /
                mapped = _attach_output_frame(frame)
                if mapped is None:
                    return
                kind, payload = mapped
                stream.emit(kind, **payload)

            # 打端内连接标：同空间多条 attach 并存时 EndRegistry 按 channel_id 精确归位本连接，不再被 (source,
            # session_id) 单槽互截。
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
                # 正文与工具行均由 EndRegistry 路由投递（chunk 走 base.py 统一路由， ✓/✗ yield 已删、工具行经
                # _route_tool_line → sender → _attach_output_frame 拼「·」形态到 stream.emit("tool")）。
                async for _ in agen:
                    pass
        except asyncio.CancelledError:
            reason = "interrupted"
        except Exception as exc:
            reason = "error"
            logger.exception("Attach chat turn error: {}", exc)
            from src.coara.turn_orchestrator import _user_facing_turn_error

            # 用户可见文案挂在 turn_end.message（及一条 chunk）上
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

    async def stream_awakened_turn_cli(
        self,
        target_coara: Any,
        text: str,
        *,
        origin_source: str,
        task_id: str,
        workspace_dir: Any = None,
    ) -> None:
        """后台完成唤醒回合（CLI 发起）：把输出经 TurnStream 投回发起 CLI 连接"""
        turn_id = uuid.uuid4().hex
        session_origin = getattr(target_coara, "session_origin", None) or {}
        channel_id = str(session_origin.get("channel_id") or "")
        if not channel_id:
            # 无发起连接时 TurnStream 帧会静默丢（send_to_nowait 找不到 conn）； 仍跑完回合以免占锁，
            # 但回投不可见——记警告便于排查。
            logger.warning(
                f"Awakened cli turn: empty session_origin.channel_id "
                f"(task_id={task_id}, origin={origin_source}); frames will not reach any attach client"
            )
        _ws_dir = workspace_dir or getattr(target_coara, "workspace_dir", None) or self.workspace_dir
        _sess_id = str(getattr(target_coara, "session_id", "") or "")
        stream = TurnStream(
            turn_id,
            "cli-attached",
            "root",
            cast(Any, self),
            channel_id=channel_id,
            session_id=_sess_id,
            workspace_dir=str(_ws_dir),
            mark_replayed=True,
        )
        # 与 web 唤醒 / 普通 attach 回合同款：进 _turns 才能被重连回放扫到。
        self._turns[turn_id] = stream
        stream.emit("turn_start", awakened=True, task_id=task_id, origin_source=origin_source)

        # 不再塌成 background（路由与占用归属都按真实发起端算）。
        end_registry = getattr(self.root, "end_registry", None)
        sender: Any = None
        if end_registry is not None:

            def sender(frame: dict) -> None:
                mapped = _attach_output_frame(frame)
                if mapped is None:
                    return
                kind, payload = mapped
                stream.emit(kind, **payload)

            if channel_id:
                sender._end_channel_id = channel_id  # type: ignore[attr-defined]
            end_registry.register("cli-attached", sender, _sess_id)
        reason = "complete"
        error_message: str | None = None
        try:
            async with turn(
                "cli-attached",
                channel_id=channel_id,
                send_text=None,
                interaction_channel=self.attach_interaction_channel.for_connection(channel_id),
            ):
                # 唤醒回合打标（同 web 唤醒）：turn_end 带 awakened=True，活动时钟不刷新。
                target_coara._awakened_turn_active = True
                agen = target_coara.process_message(
                    text,
                    trust_level="owner",
                    show_tool_summary=True,
                    # source 沿用发起端 cli-attached：段归属正确，与上方注册键匹配
                    source="cli-attached",
                    turn_id=turn_id,
                )
                async for _ in agen:
                    pass
        except asyncio.CancelledError:
            reason = "interrupted"
            raise
        except Exception as exc:
            reason = "error"
            logger.exception("Awakened cli turn error: {}", exc)
            from src.coara.turn_orchestrator import _user_facing_turn_error

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

    # CLI 命令全量透传统一命令层，不再限白名单。

    async def _handle_attach_command(self, data: dict[str, Any], ws: web.WebSocketResponse, conn_id: str) -> None:
        """attach 的斜杠命令：全量透传统一命令层，target_coara=pin 空间"""
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
            from src.coara.commands.registry import maybe_confirm_session_config, settle_b_class_before_handler

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
            await settle_b_class_before_handler(parsed, self.root)

        if parsed.name == "model":
            output, model_data = await self._attach_switch_model(workspace_id, parsed)
            await self._send_attach_command_result(
                ws,
                output=output,
                data=model_data,
                request_id=str(data.get("request_id") or ""),
            )
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
        # /ws switch：command 已改 cli view；本连接必须能占用目标空间，否则回滚 view， 避免「chrome 已是新空间、
        # pin/聊天仍在旧空间」分叉。
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
            usage_snapshot=self._client_usage_snapshot(coara),
        )

    async def _send_attach_command_result(
        self,
        ws: web.WebSocketResponse,
        *,
        output: str | None,
        action: str | None = None,
        data: dict[str, Any] | None = None,
        exit_session: bool = False,
        request_id: str = "",
        usage_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """command_result 帧统一出口：字段对齐 CommandResult 供客户端渲染"""
        frame: dict[str, Any] = {
            "type": "command_result",
            "request_id": request_id,
            "result": {
                "output": output,
                "action": action,
                "data": data or {},
                "exit_session": exit_session,
            },
        }
        if usage_snapshot is not None:
            frame["usage_snapshot"] = usage_snapshot
        await ws.send_str(json.dumps(frame, ensure_ascii=False))

    async def _attach_switch_model(self, workspace_id: str, parsed: Any) -> tuple[str, dict[str, Any] | None]:
        """执行 attach 的 /model：无参列出模型（标注当前空间所用），带参切换该空间模型"""
        from src.coara.commands.config import _empty_model_catalog_result
        from src.core.config import config_manager
        from src.core.errors import ConfigError, ProviderNotFoundError
        from src.llm.model_catalog import (
            list_model_choices,
            resolve_model_by_index,
            resolve_model_selection,
        )

        session = self.root._sessions.get(workspace_id)
        if session is None:
            return "工作空间会话未就绪", None
        coara = session.coara
        current = f"{coara.provider_name}/{coara.model_name}"

        # /model <arg> [<model>]：单参数命令的 value 在 parts[1]（parse_command 的 value 是第 3 个 token，单参时为
        # None）。
        raw_parts = [p for p in parsed.parts[1:] if not p.startswith("--")]
        # 旧 /model --add：一律改走模型配置页（密钥只在 Web 设置）
        if any(p == "--add" for p in parsed.parts[1:]):
            empty = _empty_model_catalog_result(self.root, current)
            return empty.output, empty.data
        arg = raw_parts[0] if raw_parts else None
        model_arg = raw_parts[1] if len(raw_parts) > 1 else None
        if not arg:
            choices = list_model_choices(config_manager)
            if not choices:
                empty = _empty_model_catalog_result(self.root, current)
                return empty.output, empty.data
            lines = [f"可用模型（当前 {current}）："]
            for idx, choice in enumerate(choices, start=1):
                mark = "*" if choice.key == current else " "
                lines.append(f"{mark} {idx}. {choice.label}")
            lines.append("用法：/model <序号|模型名|供应商/模型>（绑定当前工作空间）")
            return "\n".join(lines), {
                "current": current,
                "choices": [{"idx": i + 1, "key": c.key, "label": c.label} for i, c in enumerate(choices)],
            }
        try:
            if arg.isdigit():
                provider_name, model_name = resolve_model_by_index(config_manager, int(arg))
            else:
                provider_name, model_name = resolve_model_selection(config_manager, arg, model_arg)
            applied_provider, applied_model = self.root.switch_llm_for_workspace(
                workspace_id, provider_name, model_name, origin_source="cli-attached"
            )
        except (ConfigError, ProviderNotFoundError) as exc:
            return f"无法切换模型：{exc}", None
        return f"已切换当前工作空间模型：`{applied_provider}/{applied_model}`", {
            "provider": applied_provider,
            "model": applied_model,
        }
