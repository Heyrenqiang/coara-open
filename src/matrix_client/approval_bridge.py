"""Matrix approval custom-msgtype protocol + delivery (dumb pipe).

审批语义的单一事实源在 :class:`~src.coara.approval_center.ApprovalCenter`；本模块
不持有 pending 状态机（future / 记录都在 ApprovalCenter），只负责矩阵端协议面与
投递:

- 出站: 把 ApprovalCenter 的请求/终态帧编码成自定义 msgtype 的 m.room.message
  发送到回合房间（投递定位 room_id 由请求发出时的回合房间记录，供终态帧在回合
  结束后仍能回到同房）
- 入站: 识别手机端 ``m.coara.approval_reply`` 事件并路由进
  ApprovalCenter.resolve（按 approval_id 幂等终态转换）

审批只发回合来源端（与 chunk/diff 同一 source 派发铁律）；无 EndChannel / turn
上下文的回合外审批由 ApprovalCenter fail-closed 拒绝，不设 standing 逃生门。

协议（自定义 msgtype，content 即结构化 JSON；与 web/attach WS 帧字段一致）::

    m.coara.approval        请求卡（bot → room）
      content: {msgtype, body, approval_id, question, options[], timeout_s,
                workspace, created_at_ms}
    m.coara.approval_reply  回执（手机 → bot）
      content: {msgtype, body, approval_id, approved}
    m.coara.approval_resolved  终态（bot → room）
      content: {msgtype, body, approval_id, outcome}
      outcome ∈ approved|rejected|timeout|cancelled

``body`` 是 Matrix m.room.message 必填的渲染文本（普通客户端看到 question /
outcome 摘要；coara App 按 msgtype 渲染卡片）。其余键为结构化载荷，Android 对齐
以本 docstring 为准。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.coara.turn_context import get_turn_channel_id
from src.core.logger import logger

# 矩阵审批自定义 msgtype（m.room.message 的 content.msgtype）
APPROVAL_MSGTYPE_REQUEST = "m.coara.approval"
APPROVAL_MSGTYPE_REPLY = "m.coara.approval_reply"
APPROVAL_MSGTYPE_RESOLVED = "m.coara.approval_resolved"

# 房间事件发送回调：由 bot/matrix_runner 启动时注册（签名见 register_approval_room_sender）。
_RoomEventSender = Callable[[str, dict[str, Any]], Awaitable[bool]]
_ROOM_SENDER: _RoomEventSender | None = None

# 投递目标映射：approval_id → 卡片所在房间。ApprovalCenter 的终态帧（超时/打断/
# 陈旧回执 settle）可能发生在原回合结束后、context 已切换，settle 时据此定位房间。
# 只记投递目标，不构成审批业务 pending 状态。
_APPROVAL_ROOMS: dict[str, str] = {}


def register_approval_room_sender(sender: _RoomEventSender) -> None:
    """注册房间事件发送回调（bot/CLI runner 各持 client 注册自己的实现）。

    ``sender(room_id, content)`` 以 content 原样 room_send 一条 m.room.message
    （msgtype 由 content 携带），返回 bool 表示是否送达。
    """
    global _ROOM_SENDER
    _ROOM_SENDER = sender


# ----------------------------------------------------------------------
# 载荷构造（纯函数）
# ----------------------------------------------------------------------


def build_approval_request_content(frame: dict[str, Any]) -> dict[str, Any]:
    """请求卡 content：ApprovalCenter 请求帧 → m.coara.approval 事件载荷。"""
    question = str(frame.get("question") or "").strip()
    return {
        "msgtype": APPROVAL_MSGTYPE_REQUEST,
        "body": question or "审批请求",
        "approval_id": str(frame.get("approval_id") or ""),
        "question": question,
        "options": [
            {
                "label": str(opt.get("label", "")).strip(),
                "description": str(opt.get("description", "")).strip(),
            }
            for opt in frame.get("options") or []
        ],
        # 手机端据此本地倒计时自失效（晚到渲染的历史卡自动禁用），与服务端等待窗口对齐
        "timeout_s": int(frame.get("timeout_s") or 0),
        "workspace": str(frame.get("workspace") or ""),
        "created_at_ms": int(frame.get("created_at_ms") or 0),
    }


def build_approval_resolved_content(frame: dict[str, Any]) -> dict[str, Any]:
    """终态 content：m.coara.approval_resolved（手机端据此关闭/置灰卡片）。"""
    approval_id = str(frame.get("approval_id") or "")
    outcome = str(frame.get("outcome") or "")
    return {
        "msgtype": APPROVAL_MSGTYPE_RESOLVED,
        "body": f"审批已结束：{outcome}",
        "approval_id": approval_id,
        "outcome": outcome,
    }


def parse_approval_reply_content(content: Any) -> tuple[bool | None, str | None]:
    """解析手机端 m.coara.approval_reply 事件 content。返回 ``(decision, approval_id)``。

    非回执 msgtype / 结构不符返回 ``(None, None)``。
    """
    if not isinstance(content, dict):
        return None, None
    if content.get("msgtype") != APPROVAL_MSGTYPE_REPLY:
        return None, None
    approved = content.get("approved")
    if not isinstance(approved, bool):
        return None, None
    raw_id = content.get("approval_id")
    approval_id = str(raw_id).strip() or None if raw_id is not None else None
    return approved, approval_id


# ----------------------------------------------------------------------
# 出站投递（回合房间）
# ----------------------------------------------------------------------


async def _send_to_room(room_id: str, content: dict[str, Any]) -> bool:
    sender = _ROOM_SENDER
    if sender is None:
        logger.warning("Matrix approval send requested but no room sender registered")
        return False
    try:
        sent = await sender(room_id, content)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Matrix approval send raised room={room_id}: {exc}")
        return False
    return sent is not False


async def deliver_approval_request_frame(frame: dict[str, Any], *, room_id: str | None = None) -> bool:
    """把 ApprovalCenter 请求帧发成 m.coara.approval 卡片到指定房间。

    缺省 ``room_id`` 取回合来源房间（ApprovalCenter 在无 turn 上下文时不会
    走到本通道）；命令期确认等显式传 room（room-bound channel）。发送失败
    返回 False，由 Center fail-closed。
    """
    room = str(room_id or get_turn_channel_id() or "").strip() or None
    approval_id = str(frame.get("approval_id") or "")
    if not room:
        logger.warning(
            f"Matrix approval requested but no turn room (id={approval_id}, "
            f"sender_registered={_ROOM_SENDER is not None})"
        )
        return False
    if _ROOM_SENDER is None:
        logger.warning(
            f"Matrix approval room sender not registered (id={approval_id} room={room}); "
            "matrix client may not have finished setup"
        )
        return False
    content = build_approval_request_content(frame)
    if not await _send_to_room(room, content):
        logger.warning(f"Matrix approval room_send failed id={approval_id} room={room}")
        return False
    _APPROVAL_ROOMS[approval_id] = room
    logger.info(f"Matrix approval raised id={approval_id} room={room}")
    return True


async def deliver_approval_resolved_frame(frame: dict[str, Any]) -> bool:
    """终态帧：发 m.coara.approval_resolved 到卡所在房间（超时/打断/用户已答）。"""
    approval_id = str(frame.get("approval_id") or "")
    room_id = _APPROVAL_ROOMS.pop(approval_id, None) or get_turn_channel_id()
    if not room_id:
        # 房间未知（如进程重启丢映射）：终态已 settle，不补帧也不影响状态机
        logger.debug(f"Matrix approval resolved frame skipped (no room) id={approval_id}")
        return True
    content = build_approval_resolved_content(frame)
    await _send_to_room(room_id, content)
    return True


# ----------------------------------------------------------------------
# 入站回执路由（RoomMessageUnknown 事件层）
# ----------------------------------------------------------------------


def handle_approval_reply_event(*, sender: str, bot_user_id: str, content: Any) -> bool:
    """处理 RoomMessageUnknown 里携带的审批回执事件。

    由 bot / CLI matrix_runner 的 RoomMessageUnknown 回调调用（在文本预过滤与
    调度之前——回执必须赶在持锁等待的审批回合前 resolve）。
    Return True 表示事件是审批回执（已消费）；False 表示无关事件。
    """
    decision, approval_id = parse_approval_reply_content(content)
    if decision is None:
        return False
    if sender == bot_user_id:
        # bot 自己的 echo（正常不会回执）；防御性跳过
        return False
    if not approval_id:
        # 无实例 id：不 resolve（不猜最近一条），但 msgtype 匹配即机器信封，消费掉
        logger.info("Matrix approval reply without approval_id discarded")
        return True

    from src.coara.approval_center import get_approval_center

    resolved = get_approval_center().resolve(
        approval_id, approved=decision, resolved_by="matrix", actor=sender
    )
    if resolved:
        logger.info(f"Matrix approval resolved id={approval_id} decision={'yes' if decision else 'no'}")
    else:
        # 陈旧/未知回执（卡已超时、作废或进程重启后终态化）：幂等路由自然拒绝
        logger.info(f"Matrix approval stale/unknown reply discarded id={approval_id}")
    return True
