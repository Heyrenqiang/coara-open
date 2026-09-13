"""/compact — 手动压缩当前会话历史。

复用 force_compress_history（与上下文溢出兜底同一实现）：
LLM 压缩（失败回退截断），被替代历史归档进事件日志，不物理丢失。
不在 RUN_WHILE_BUSY 中——压缩改写历史，只能空闲时执行。
"""

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
    # 忙闲保护：压缩会整体替换 message_history，活跃回合中执行会把进行中回合
    # 的历史换走（历史分叉/压缩归档错乱）。/compact 不在 RUN_WHILE_BUSY 中，
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
        logger.warning(f"/compact failed: {exc}")
        return CommandResult(output=f"压缩失败：{exc}", data={"compressed": False, "error": str(exc)})

    if info is None:
        return CommandResult(output="历史太短，没有可压缩的内容", data={"compressed": False})

    method = str(info.get("method") or "llm")
    method_label = "LLM 摘要" if method == "llm" else "截断兜底"
    original_tokens = int(info.get("original_tokens") or 0)
    new_tokens = int(info.get("new_tokens") or 0)
    lines = [
        f"已压缩（{method_label}）：{info.get('original_count')} → {info.get('compressed_count')} 条消息",
        f"历史 token：约 {original_tokens // 1000}K → {new_tokens // 1000}K",
        "被替代的历史已归档进事件日志（compaction），未物理丢失",
    ]
    coara._emit_trace(
        "context_compressed_manual",
        f"Manual /compact: {info.get('original_count')} -> {info.get('compressed_count')} messages",
        payload=info,
    )
    return CommandResult(output="\n".join(lines), data={"compressed": True, "info": info})
