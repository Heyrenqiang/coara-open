"""/log — 工具执行记录回看（显示折叠的展开闭环）。

查询会话录像带（session_events.jsonl 的 ``tool/exec`` 事件）定位工具执行：
- 有 ``spill_ref`` → 从 tool_output_store 惰性读完整输出（单文件一次 IO）
- 未 spill 的小输出 → 从当前会话历史消息（TOOL_RESULT 角色）取

用户主动低频命令，读录像带全文件扫描可接受；不缓存、不预热。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.coara.commands.registry import CommandArgs, register, resolve_target_coara
from src.coara.commands.types import CommandResult
from src.core.logger import logger
from src.session_log.store import read_events, resolve_session_log_path
from src.session_log.types import EVENT_TOOL_EXEC

if TYPE_CHECKING:
    from src.coara.root import RootCoara

_MAX_LIST = 20
_MAX_HEAD_LINES = 40


def _coara_home(root: RootCoara) -> str | None:
    try:
        wm = getattr(root, "workspace_manager", None)
        return str(wm.coara_home) if wm is not None else None
    except Exception:
        return None


def _session_id(coara) -> str:
    return str(getattr(coara, "session_id", "") or "")


def _read_tool_execs(root: RootCoara, coara, *, all_sessions: bool = False) -> list[dict]:
    """读取 tool/exec 事件（按 seq 升序）。

    ``all_sessions=False`` 只读当前前台会话（列表模式）；``True`` 读全部
    会话（含子代理）——``--tool`` 展开子代理输出时用，因为子代理工具记录
    写在子代理自己的 session_id 下，但同一录像带文件。
    """
    try:
        path = resolve_session_log_path(
            coara.workspace_dir,
            coara_home=_coara_home(root),
        )
        events = read_events(path)
        if not all_sessions:
            events = [e for e in events if str(e.get("session_id") or "") == _session_id(coara)]
        return [e for e in events if e.get("kind") == EVENT_TOOL_EXEC]
    except Exception as exc:
        logger.warning("/log: failed to read session log: {}", exc)
        return []


def _format_exec_head(event: dict) -> str:
    payload = event.payload or {}
    tool = str(payload.get("tool_name") or "?")
    mark = "✗" if payload.get("is_error") else "✓"
    lines = int(payload.get("lines") or 0)
    dur = payload.get("duration_ms")
    dur_s = f"{float(dur) / 1000:.1f}s" if dur is not None else ""
    meta = f" · {lines} lines" if lines > 0 else ""
    meta += f" · {dur_s}" if dur_s else ""
    spill = " · spilled" if payload.get("spill_ref") else ""
    cache = " · cache" if payload.get("cache_hit") else ""
    return f"{mark} [{tool}]{meta}{spill}{cache}"


def _load_exec_output(root: RootCoara, coara, event: dict) -> str | None:
    """取工具执行完整输出：spill 读存档，否则从历史消息找。"""
    payload = event.payload or {}
    spill_ref = str(payload.get("spill_ref") or "")
    if spill_ref:
        try:
            from src.runtime.tool_output_store import find_tool_output_record

            record = find_tool_output_record(
                workspace_dir=coara.workspace_dir,
                ref=spill_ref,
                coara_home=_coara_home(root),
            )
            return record.content
        except Exception as exc:
            logger.warning("/log: failed to load spill {}: {}", spill_ref, exc)
            return None

    tool_call_id = str(payload.get("tool_call_id") or "")
    if not tool_call_id:
        return None
    try:
        from src.core.types import MessageRole

        history = list(coara.message_history or [])
        for msg in reversed(history):
            if getattr(msg.role, "value", msg.role) != getattr(MessageRole.TOOL_RESULT, "value", "tool"):
                continue
            if str(getattr(msg, "tool_call_id", "") or "") != tool_call_id:
                continue
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                from src.utils.message_content import message_content_to_text

                return message_content_to_text(content)
            return str(content or "")
    except Exception as exc:
        logger.warning("/log: failed to read history for {}: {}", tool_call_id, exc)
    return None


@register("log")
async def handle_log(root: RootCoara, args: CommandArgs) -> CommandResult:
    """回看/展开最近工具执行。

    /log          列出最近工具执行（轻元数据）
    /log <n>      展开最近第 n 条（n=1 最近）
    /log --seq N  按录像带 seq 展开
    /log --tool <call_id>  按工具调用 id 展开（含子代理工具，跨会话查）
    """
    coara = resolve_target_coara(root, args)
    execs = _read_tool_execs(root, coara)
    if not execs:
        return CommandResult.text("本会话还没有工具执行记录。")

    # 定位目标事件
    target: dict | None = None
    seq_arg = args.flag("seq")
    tool_arg = args.flag("tool")
    if seq_arg:
        try:
            want = int(seq_arg)
        except (TypeError, ValueError):
            return CommandResult.error("/log --seq 需要整数 seq。")
        target = next((e for e in execs if int(e.get("seq") or 0) == want), None)
        if target is None:
            return CommandResult.error(f"录像带中没有 seq={want} 的工具执行记录。")
    elif tool_arg:
        # 子代理工具的记录在各自 session_id 下（同一录像带文件）：
        # 跨会话查，spill 输出由 find_tool_output_record 无 session 遍历找到
        all_execs = _read_tool_execs(root, coara, all_sessions=True)
        target = next(
            (e for e in reversed(all_execs) if str((e.get("payload") or {}).get("tool_call_id") or "") == tool_arg),
            None,
        )
        if target is None:
            return CommandResult.error(f"没有找到工具调用 {tool_arg} 的执行记录。")
    elif args.value:
        try:
            idx = int(args.value)
        except (TypeError, ValueError):
            return CommandResult.error("/log <n> 需要整数序号（1=最近一条）。")
        if idx <= 0:
            return CommandResult.error("/log <n> 序号从 1 开始（1=最近一条）。")
        if idx > len(execs):
            return CommandResult.error(f"最近工具执行只有 {len(execs)} 条。")
        target = execs[-idx]
    else:
        target = None

    # 无参：列表模式
    if target is None:
        recent = execs[-_MAX_LIST:]
        lines = [f"最近 {len(recent)} 条工具执行（/log <序号> 展开；/log --seq <seq> 按记录定位）：", ""]
        base = len(execs) - len(recent) + 1
        for offset, event in enumerate(recent):
            seq = int(event.get("seq") or 0)
            lines.append(f"  {base + offset:>3}. seq={seq}  {_format_exec_head(event)}")
        lines.append("")
        lines.append("提示：✓ 行末尾出现 · … +N lines (/log 展开) 即表示输出已折叠。")
        return CommandResult.text("\n".join(lines))

    # 展开模式
    seq = int(target.get("seq") or 0)
    payload = target.get("payload") or {}
    tool = str(payload.get("tool_name") or "?")
    content = _load_exec_output(root, coara, target)
    head = [
        f"工具执行 · seq={seq} · {tool}",
        _format_exec_head(target),
        "",
    ]
    if content is None:
        head.append("（完整输出不可用：spill 记录已过期，且历史消息中无此工具结果）")
        return CommandResult.text("\n".join(head), seq=seq, tool=tool)

    if not content.strip():
        head.append("（工具无输出）")
        return CommandResult.text("\n".join(head), seq=seq, tool=tool)

    head.append(content)
    return CommandResult.text("\n".join(head), seq=seq, tool=tool, lines=payload.get("lines"))
