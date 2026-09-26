"""/compact — 手动压缩当前会话历史"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register
from src.coara.commands.types import CommandResult
from src.core.logger import logger

if TYPE_CHECKING:
    from src.coara.root import RootCoara


@register("compact")
async def handle_compact(root: RootCoara, args: CommandArgs) -> CommandResult:
    """手动压缩当前会话历史（LLM 摘要，被替代部分归档可回取）。"""
    # 模块会话（FlowRoot 构建对话等）里执行时压缩该模块主体的历史，而非主会话
    coara = args.target_coara or root.foreground_coara
    # 但 Web/Matrix 等入口若无 busy 检查仍会到达这里——服务端强制兜底。
    if coara.has_active_turn() or coara._process_lock.locked():
        return CommandResult(output="当前有正在进行的回合，请稍后或用 /stop 停止后再压缩", data={"compressed": False})
    if not coara.message_history:
        return CommandResult(output="当前会话还没有历史，无需压缩", data={"compressed": False})
    # 已有压缩在飞（用户连点 /compact 或自动压缩进行中）：并发保护会让本次直接
    # 返回 None，若复用「历史太短」文案会误导——这里单独给出明确提示。
    if getattr(coara, "_compress_inflight", False):
        return CommandResult(output="正在压缩中，请稍候再试", data={"compressed": False})

    from src.coara.turn_orchestrator import force_compress_history

    try:
        info = await force_compress_history(coara)
    except Exception as exc:
        logger.warning("/compact failed: {}", exc)
        # 用户可见文案不贴 provider 原文（可能含报文与配额细节），只给可行动信息
        return CommandResult(
            output="压缩失败：模型调用出错或配置不可用，历史未改动（详情见内核日志）",
            data={"compressed": False, "error": str(exc)},
        )

    if info is None:
        return CommandResult(output="历史太短，没有可压缩的内容", data={"compressed": False})

    method = str(info.get("method") or "llm")
    # 条数/token 明细留在 data 与 trace，不上屏；屏幕只留一条「已压缩」分割线
    coara._emit_trace(
        "context_compressed",
        f"Manual /compact: {info.get('original_count')} -> {info.get('compressed_count')} messages",
        payload={**info, "method": method, "manual": True},
    )
    # 手机端靠 [COARA_STATUS] 的 session_event=compacted 画线（与 model_switch 同路），
    # 发起端各自出口画线（web 落带 divider / CLI 打印 / matrix 不发气泡）
    from src.coara.mobile_sync import push_status_payload

    push_status_payload(root, force=True, session_event="compacted")
    # 成功回执静默（气泡改分割线，09-26 口径）；失败路径仍给文本提示
    return CommandResult(output="", data={"compressed": True, "info": info})
