"""ApprovalCenter — 内核唯一审批仲裁者（单一事实源）.

一次工具审批 = 一条持久化状态机记录，经回合 EndChannel（方案 D4）定向投递到
消息来源端；回执按 approval_id 幂等路由，终态事件广播到全部端（在渲染的端
据此自失效）。fail-closed：无可达通道 / 超时 / 异常一律拒绝，绝不静默放行。

协议（三端共用同一套帧，字段完全一致）::

    请求 approval_request: {approval_id, question, options[], timeout_s, workspace, created_at_ms}
    回执 approval_reply:   {approval_id, approved}
    终态 approval_resolved: {approval_id, outcome}   outcome ∈ approved|rejected|timeout|cancelled

Matrix 端承载为自定义 msgtype（m.coara.approval / m.coara.approval_reply /
m.coara.approval_resolved），web/attach 端为同名 WS JSON 帧，CLI 为本地 modal。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger

if TYPE_CHECKING:
    from src.core.abort import AbortSignal

OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_CANCELLED = "cancelled"

# 发送单独限时：send 可能卡在网络抖动里，上限把最坏空等从审批超时里剥离
_SEND_TIMEOUT_SECONDS = 30.0


def _reply_actor_for_end(end_channel: Any, *, end_source: str) -> str:
    """Bind approvals to the turn originator — not merely to approval_id.

    matrix: actor must be the initiating MXID (room_id alone is shared).
    web / attach: actor is the connection id (channel_id).
    """
    if end_channel is None:
        return ""
    actor = str(getattr(end_channel, "actor", "") or "").strip()
    if actor:
        return actor
    source = (end_source or str(getattr(end_channel, "source", "") or "")).strip().lower()
    if source == "matrix":
        # Do not bind to room_id — that would still allow any room member.
        return ""
    return str(getattr(end_channel, "channel_id", "") or "").strip()


@dataclass(slots=True)
class ApprovalRecord:
    """一条审批的持久化记录（状态机：pending → terminal outcome）。"""

    approval_id: str
    question: str
    options: list[dict[str, str]]
    timeout_s: float
    source: str = ""
    workspace: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    state: str = "pending"
    outcome: str = ""
    resolved_by: str = ""
    resolved_at: float = 0.0
    # Who may settle this approval (matrix MXID / web·attach conn_id). Empty = unbound.
    reply_actor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "question": self.question,
            "options": self.options,
            "timeout_s": self.timeout_s,
            "source": self.source,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "outcome": self.outcome,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "reply_actor": self.reply_actor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRecord:
        return cls(
            approval_id=str(data["approval_id"]),
            question=str(data.get("question") or ""),
            options=[dict(o) for o in data.get("options") or [] if isinstance(o, dict)],
            timeout_s=float(data.get("timeout_s") or 0.0),
            source=str(data.get("source") or ""),
            workspace=str(data.get("workspace") or ""),
            created_at=float(data.get("created_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            state=str(data.get("state") or "pending"),
            outcome=str(data.get("outcome") or ""),
            resolved_by=str(data.get("resolved_by") or ""),
            resolved_at=float(data.get("resolved_at") or 0.0),
            reply_actor=str(data.get("reply_actor") or ""),
        )

    def request_frame(self) -> dict[str, Any]:
        """三端共用的审批请求帧字段（哑渲染器只消费它）。"""
        return {
            "approval_id": self.approval_id,
            "question": self.question,
            "options": [dict(o) for o in self.options],
            "timeout_s": int(self.timeout_s),
            "workspace": self.workspace,
            "created_at_ms": int(self.created_at * 1000),
        }


@dataclass(slots=True)
class _LiveApproval:
    """在途审批：持久化记录 + 等回执的 future + 终态后通知该端用的发送回调。"""

    record: ApprovalRecord
    future: asyncio.Future[bool]
    end_source: str
    # 终态帧投递（approval_resolved 给来源端）；发送失败不影响状态机
    send_terminal: Any = None
    # 结构化审计回调
    audit: Any = None


class ApprovalCenter:
    """审批状态机与路由中枢。RootCoara 启动时构造，挂到 ``root.approval_center``。"""

    def __init__(self, *, store_path: Path | None = None, event_bus: Any = None) -> None:
        self._store_path = store_path
        self._event_bus = event_bus
        self._live: dict[str, _LiveApproval] = {}
        self._records: dict[str, ApprovalRecord] = {}
        self._load()

    # ------------------------------------------------------------------

    @staticmethod
    def default_store_path() -> Path | None:
        """``<coara_home>/runtime/approvals.json``；无 home 时返回 None（仅内存）。"""
        try:
            from src.core.config import config_manager

            config = config_manager._config  # noqa: SLF001
            home = getattr(config, "coara_home", None) if config is not None else None
            if home is None:
                return None
            return Path(home) / "runtime" / "approvals.json"
        except Exception:
            return None

    def _load(self) -> None:
        path = self._store_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Failed to load approvals store {path}: {exc}")
            return
        items = raw.get("approvals") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        now = time.time()
        for item in items:
            try:
                record = ApprovalRecord.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(f"Skipping invalid approval record: {exc}")
                continue
            if record.state == "pending":
                # 重启即终态：future 已随进程消亡，卡若在端上仍亮着，由端侧
                # 重连补偿（query → 无此记录 → 不补发；或 resolved 帧）收口。
                record.state = "terminal"
                record.outcome = OUTCOME_CANCELLED
                record.resolved_by = "restart"
                record.resolved_at = now
            self._records[record.approval_id] = record

    def _persist(self) -> None:
        path = self._store_path
        if path is None:
            return
        payload = {"version": 1, "approvals": [r.to_dict() for r in self._records.values()]}
        try:
            write_json_atomic(path, payload)
        except Exception as exc:
            logger.warning(f"Failed to persist approvals: {exc}")

    # ------------------------------------------------------------------
    # 状态查询（离线补偿 / 观测）
    # ------------------------------------------------------------------

    def pending_records(self, *, source: str = "") -> list[ApprovalRecord]:
        """仍 pending 的记录；``source`` 非空时按来源端过滤（端侧重连补偿用）。"""
        return [r for r in self._records.values() if r.state == "pending" and (not source or r.source == source)]

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    # ------------------------------------------------------------------
    # 核心：发起审批并等待回执
    # ------------------------------------------------------------------

    async def request(
        self,
        *,
        question: str,
        options: list[dict[str, Any]],
        timeout_seconds: float,
        workspace: str = "",
        signal: AbortSignal | None = None,
        audit: Any = None,
        channel: Any = None,
    ) -> bool:
        """按回合 EndChannel 定向投递审批，等待回执。fail-closed。

        审批默认发回合来源端（EndChannel 的 interaction_channel）；回合外（后台
        子智能体等无 EndChannel/turn 上下文）无可达通道时经 CLI modal 兜底——
        无人值守（daemon 无 CLI）即 fail-closed 拒绝，不存在静默放行路径。

        ``channel``：显式指定投递通道（命令期确认门等无回合上下文的调用方用，
        如 attach for_connection / web interaction channel / matrix room 适配器）。
        None 时按回合上下文解析。

        Returns True=同意 False=拒绝。
        Raises TimeoutError（超时=拒绝语义由调用方包装）、OperationAborted、
        RemotePromptDeliveryError（无可达通道）。
        """
        from src.coara.remote_channel import RemotePromptDeliveryError
        from src.coara.turn_context import get_end_channel, get_turn_channel
        from src.core.abort import OperationAborted, wait_for_abortable

        end_channel = get_end_channel()
        remote_channel = channel
        if remote_channel is None:
            remote_channel = (
                end_channel.interaction_channel if end_channel is not None else None
            ) or get_turn_channel()
        end_source = str(getattr(channel, "_confirm_source", "") or getattr(end_channel, "source", "") or "")

        normalized = [{"label": str(o.get("label", "")), "description": str(o.get("description", ""))} for o in options]
        record = ApprovalRecord(
            approval_id=uuid.uuid4().hex,
            question=question.strip(),
            options=normalized,
            timeout_s=timeout_seconds,
            source=end_source,
            workspace=workspace,
            created_at=time.time(),
            reply_actor=_reply_actor_for_end(end_channel, end_source=end_source),
        )
        record.expires_at = record.created_at + max(timeout_seconds, 0.0)
        frame = record.request_frame()

        if signal is not None and signal.aborted:
            raise OperationAborted(signal.reason or "interrupted")

        send_terminal: Any = None
        if remote_channel is not None:
            send_terminal = getattr(remote_channel, "send_approval_resolved", None)
            if not callable(send_terminal):
                send_terminal = None

        if remote_channel is None:
            # 无回合通道（回合外：后台子智能体等）：本地 CLI modal 兜底。
            # 无 modal 可用时 fail-closed 拒绝，不存在静默放行路径。
            return await self._request_via_cli_modal(record=record, frame=frame, signal=signal, audit=audit)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        live = _LiveApproval(
            record=record,
            future=future,
            end_source=end_source,
            send_terminal=send_terminal,
            audit=audit,
        )
        # 先占位再发送：回执可能在 send 尚未返回时经 ingress 路由进来
        self._live[record.approval_id] = live
        self._records[record.approval_id] = record
        self._persist()
        self._emit("approval_requested", record, {"question": question})

        try:
            try:
                sent = await asyncio.wait_for(
                    remote_channel.send_approval_request(frame), timeout=_SEND_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.warning(f"Approval send timed out after {_SEND_TIMEOUT_SECONDS:.0f}s id={record.approval_id}")
                sent = False
            except AttributeError as exc:
                self._settle(record, OUTCOME_CANCELLED, resolved_by="send_failed")
                raise RemotePromptDeliveryError(
                    "审批通道不支持结构化审批帧（通道未实现 send_approval_request）。"
                ) from exc
            if sent is False:
                self._settle(record, OUTCOME_CANCELLED, resolved_by="send_failed")
                raise RemotePromptDeliveryError("审批请求未能送达到来源端，请检查连接后重试。")

            try:
                return await wait_for_abortable(future, signal, timeout=timeout_seconds)
            except TimeoutError:
                self._settle(record, OUTCOME_TIMEOUT, resolved_by="timeout")
                raise
            except OperationAborted:
                self._settle(record, OUTCOME_CANCELLED, resolved_by="abort")
                raise
        finally:
            self._live.pop(record.approval_id, None)
            if not future.done():
                future.cancel()

    async def _request_via_cli_modal(
        self,
        *,
        record: ApprovalRecord,
        frame: dict[str, Any],
        signal: AbortSignal | None,
        audit: Any,
    ) -> bool:
        """回合外/无远端通道：CLI modal 兜底；modal 不可用时 fail-closed。"""
        from src.coara.remote_channel import RemotePromptDeliveryError

        try:
            from src.cli.interactive_prompt import prompt_select
        except Exception as exc:
            raise RemotePromptDeliveryError("审批请求无法送达：无回合端通道且 CLI 交互不可用。") from exc

        self._records[record.approval_id] = record
        self._persist()
        self._emit("approval_requested", record, {"question": record.question})
        try:
            try:
                result = await asyncio.wait_for(
                    prompt_select(
                        question=record.question,
                        options=frame["options"],
                        timeout=record.timeout_s,
                    ),
                    timeout=record.timeout_s + 5,
                )
            except RuntimeError as exc:
                # modal spinner 未注册（daemon 内核无 CLI）：fail-closed
                self._settle(record, OUTCOME_CANCELLED, resolved_by="no_channel")
                logger.warning("Approval has no reachable delivery channel; denying instead of auto-approving")
                raise RemotePromptDeliveryError(
                    "审批请求无法送达：当前没有可用的确认通道（无远端回合通道 / CLI 未初始化）。"
                    "已拒绝执行以避免静默放行。"
                ) from exc
            except TimeoutError:
                self._settle(record, OUTCOME_TIMEOUT, resolved_by="timeout")
                raise
            approved = result is not None and result.get("selection") == frame["options"][0]["label"]
            self._settle(
                record,
                OUTCOME_APPROVED if approved else OUTCOME_REJECTED,
                resolved_by="cli",
            )
            return approved
        finally:
            self._live.pop(record.approval_id, None)

    # ------------------------------------------------------------------
    # 回执路由（幂等终态转换）
    # ------------------------------------------------------------------

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolved_by: str = "",
        actor: str = "",
    ) -> bool:
        """回执到达：首个合法回执生效；身份不匹配 / 迟到 / 重复返回 False。"""
        live = self._live.get(approval_id)
        record = self._records.get(approval_id)
        if record is None or record.state != "pending":
            return False
        expected = str(record.reply_actor or "").strip()
        if expected:
            got = str(actor or "").strip()
            if got != expected:
                logger.info(f"Approval reply actor mismatch id={approval_id} expected={expected!r} got={got!r}")
                return False
        outcome = OUTCOME_APPROVED if approved else OUTCOME_REJECTED
        self._settle(record, outcome, resolved_by=resolved_by or record.source)
        if live is not None and not live.future.done():
            live.future.set_result(approved)
        return True

    def rebind_reply_actor(self, approval_id: str, actor: str) -> bool:
        """Update who may settle a live approval (web/attach reconnect redelivery)."""
        record = self._records.get(approval_id)
        if record is None or record.state != "pending":
            return False
        new_actor = str(actor or "").strip()
        if not new_actor:
            return False
        record.reply_actor = new_actor
        self._persist()
        return True

    def cancel_all(self, *, reason: str = "interrupted") -> int:
        """打断安全网：全部在途审批转 cancelled（Ctrl+C / stop）。"""
        from src.core.abort import OperationAborted

        cancelled = 0
        for live in list(self._live.values()):
            record = live.record
            if record.state == "pending":
                self._settle(record, OUTCOME_CANCELLED, resolved_by=reason)
            if not live.future.done():
                with contextlib.suppress(asyncio.InvalidStateError):
                    live.future.set_exception(OperationAborted(reason))
                cancelled += 1
        if cancelled:
            logger.info(f"Cancelled {cancelled} pending approval(s) ({reason})")
        return cancelled

    # ------------------------------------------------------------------

    def _settle(self, record: ApprovalRecord, outcome: str, *, resolved_by: str) -> None:
        """状态机终态转换 + 持久化 + 终态广播。"""
        if record.state != "pending":
            return
        record.state = "terminal"
        record.outcome = outcome
        record.resolved_by = resolved_by
        record.resolved_at = time.time()
        self._persist()
        logger.info(f"Approval settled id={record.approval_id} outcome={outcome} by={resolved_by}")
        self._emit("approval_resolved", record, {"outcome": outcome})
        live = self._live.get(record.approval_id)
        if live is not None and callable(live.send_terminal):
            frame = {"approval_id": record.approval_id, "outcome": outcome}
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(live.send_terminal(frame))
            except RuntimeError:
                pass

    def _emit(self, event_type: str, record: ApprovalRecord, extra: dict[str, Any]) -> None:
        bus = self._event_bus
        if bus is None:
            return
        try:
            from src.core.events import TraceEvent

            bus.publish(
                TraceEvent(
                    coara_id="",
                    coara_name="",
                    event_type=event_type,
                    message=f"{event_type} {record.approval_id}",
                    payload={"approval_id": record.approval_id, **record.to_dict(), **extra},
                )
            )
        except Exception:
            logger.debug(f"approval {event_type} emit failed", exc_info=True)


_APPROVAL_CENTER: ApprovalCenter | None = None


def get_approval_center() -> ApprovalCenter:
    """取全局实例；未初始化时按默认路径惰性初始化。"""
    global _APPROVAL_CENTER
    if _APPROVAL_CENTER is None:
        _APPROVAL_CENTER = ApprovalCenter(store_path=ApprovalCenter.default_store_path())
    return _APPROVAL_CENTER


def reset_approval_center_for_tests() -> None:
    global _APPROVAL_CENTER
    _APPROVAL_CENTER = None
