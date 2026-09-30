"""Trajectory view — infinite per-workspace tape reader.

从 web_views 录像带（conversation.jsonl）按字节区间分页读取，投影成轨迹行。
一个工作空间一条无限轨迹：会话边界只是轨迹上的分隔行，不是轨迹的容器。
数据形状一次定对（读端与 web_views 写端同源）：

- user_message → role=user 的用户输入行（含 source 端标记）
- chunk → role=assistant 的正文行（含子智能体 subagent_chunk，按父标识挂 delegate 组）
- tool → 工具行（轨迹主体；含 running/duration/is_error）
- diff → 工具产生的 diff（按 tool_call_id 挂在工具行详情里）
- divider → 系统分隔行（新会话/压缩/切空间）
- subagent 组：delegate 工具行是组头，其下 parent_tool_call_id 指向它的
  tool/chunk/diff 帧按时间序内嵌为小录像带
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.display_rules import is_hidden_tool_line
from src.ui.jsonl_byte_window import iter_jsonl_lines_byte_range

# 每次请求的窗口大小（字节）：2MB 覆盖常规密度下数百行
TRAJECTORY_WINDOW_BYTES = 2 * 1024 * 1024
# 单帧超长截断（防个别超大帧撑爆响应）
_FRAME_MAX_CHARS = 64 * 1024

# 不投轨迹行的帧类型（噪音/纯控制）
_SKIP_KINDS = {"typing", "turn_end", "turn_start"}


def _truncate(text: str) -> str:
    if len(text) <= _FRAME_MAX_CHARS:
        return text
    return text[:_FRAME_MAX_CHARS] + "\n…[截断]"


def _project_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """一帧录像带 → 轨迹行；不投影的帧返回 None"""
    kind = str(frame.get("kind") or "")
    if kind in _SKIP_KINDS:
        return None
    payload = frame.get("payload") or {}
    row: dict[str, Any] = {
        "seq": int(frame.get("view_seq") or 0),
        "ts": float(frame.get("ts") or 0.0),
        "source": str(frame.get("source") or ""),
        "turn_id": str(frame.get("turn_id") or ""),
    }
    # 外挂录像带标记（janitor/daily 维护流）：顶层或 payload.desk（旧帧只在 payload）
    desk = str(frame.get("desk") or payload.get("desk") or "")
    if desk:
        row["actor"] = desk
    parent = str(payload.get("parent_tool_call_id") or "")
    if parent:
        row["parent"] = parent

    if kind == "user_message":
        # 写端字段是 content（不是 text）
        text = str(payload.get("text") or payload.get("content") or "")
        if not text.strip():
            return None
        row["role"] = "user"
        row["text"] = _truncate(text)
        atts = payload.get("attachments")
        if isinstance(atts, list) and atts:
            row["attachments"] = atts
        return row

    if kind == "chunk":
        text = str(payload.get("text") or "")
        if not text.strip():
            return None
        row["role"] = "assistant"
        row["text"] = _truncate(text)
        if str(payload.get("kind") or "") == "subagent_chunk":
            row["subagent"] = True
        return row

    if kind == "tool":
        from src.coara.display import strip_tool_error_suffix

        text = strip_tool_error_suffix(str(payload.get("text") or payload.get("tool_label") or ""))
        # 噪音工具行不上带（delegate wait / send_file / todo park / plan plan）：
        # 与三端聊天区同一把尺（display_rules 真源），录像带不是例外。
        if is_hidden_tool_line(tool_name=str(payload.get("tool_name") or ""), label=text):
            return None
        row["role"] = "tool"
        row["text"] = text
        # 落带字段：新帧带 is_error；旧帧只有 ok（web 出站曾写 ok）
        is_error = bool(payload.get("is_error")) if "is_error" in payload else not bool(payload.get("ok", True))
        row["tool"] = {
            "name": str(payload.get("tool_name") or ""),
            "call_id": str(payload.get("tool_call_id") or ""),
            "is_error": is_error,
            "running": bool(payload.get("running", False)),
            "duration_ms": payload.get("duration_ms"),
        }
        # 参数与结果全文随帧（详情面板分段展示；录像带原则：能看到的全落带）
        args = payload.get("arguments")
        if isinstance(args, dict) and args:
            row["tool"]["arguments"] = args
        output = str(payload.get("tool_output") or "")
        if output:
            row["tool"]["output"] = _truncate(output)
        output_ref = str(payload.get("tool_output_ref") or "").strip()
        if output_ref:
            row["tool"]["output_ref"] = output_ref
        if payload.get("tool_output_truncated"):
            row["tool"]["output_truncated"] = True
        return row

    if kind == "diff":
        # diff 不单独上带：内容挂在产生它的工具行的详情里（详情面板展示）。
        # 无有效 diff 数据（不是 dict 或空）不上带——空行会把组撑成幽灵大块。
        diff = payload.get("diff") or payload.get("diff_lines")
        if not isinstance(diff, dict) or not diff:
            return None
        row["role"] = "diff_detail"
        row["tool"] = {"call_id": str(payload.get("tool_call_id") or "")}
        row["diff"] = diff
        return row

    if kind == "divider":
        row["role"] = "divider"
        row["text"] = str(payload.get("label") or payload.get("text") or "")
        return row

    if kind == "inject":
        # 系统注入帧：录像带上可见（绿 chip），聊天屏不上屏
        row["role"] = "inject"
        row["tag"] = str(payload.get("tag") or "系统消息")
        row["text"] = _truncate(str(payload.get("text") or ""))
        return row

    if kind == "thinking":
        # 思考块（每轮 LLM 一帧）：行上摘要、详情全文；空文本不上带（与 live 投影同尺）
        text = str(payload.get("text") or "")
        if not text.strip():
            return None
        row["role"] = "thinking"
        row["text"] = _truncate(text)
        return row

    if kind == "context_module":
        # 上下文模块清单（会话首帧）：行上一句、详情模块表
        row["role"] = "context_module"
        row["text"] = str(payload.get("text") or "上下文模块")
        modules = payload.get("modules")
        if isinstance(modules, list):
            row["modules"] = modules
        return row

    if kind == "prompt":
        # 系统提示词快照（会话首帧与变更帧）：行上摘要、详情全文
        row["role"] = "prompt"
        row["text"] = str(payload.get("summary") or payload.get("text") or "系统提示词快照")
        row["prompt"] = {
            "hash": str(payload.get("hash") or ""),
            "bytes": int(payload.get("bytes") or 0),
            "changed": bool(payload.get("changed", False)),
            "text": str(payload.get("text") or ""),
        }
        return row

    if kind == "files":
        row["role"] = "files"
        row["text"] = str(payload.get("caption") or "")
        files = payload.get("files")
        if isinstance(files, list):
            row["files"] = files
        return row

    if kind in ("subagent_chunk", "subagent_result"):
        text = str(payload.get("text") or "")
        if not text.strip():
            return None
        row["role"] = "assistant"
        row["text"] = _truncate(text)
        row["subagent"] = True
        return row

    if kind == "delegate_brief":
        row["role"] = "brief"
        row["text"] = _truncate(str(payload.get("text") or ""))
        return row

    # 未识别的 kind：不丢，按通用行投出（宁可多一行）
    text = str(payload.get("text") or "")
    if not text.strip():
        return None
    row["role"] = kind or "unknown"
    row["text"] = _truncate(text)
    return row


def _iter_lines_range(path: Path, start: int, end: int) -> list[str]:
    """读 [start, end) 字节区间内的完整行（首行截断丢弃）"""
    return iter_jsonl_lines_byte_range(path, start, end)


def read_trajectory_window(
    conversation_path: Path,
    *,
    before_offset: int | None = None,
    limit_bytes: int = TRAJECTORY_WINDOW_BYTES,
) -> dict[str, Any]:
    """从录像带读一个窗口。

    - before_offset=None：读最新窗口（文件尾部）
    - before_offset=N：读 [N-limit, N) 的更早窗口（向上翻页）
    返回 {rows, has_older, oldest_offset, file_size}
    """
    file_size = conversation_path.stat().st_size if conversation_path.exists() else 0
    if file_size == 0:
        return {"rows": [], "has_older": False, "oldest_offset": 0, "file_size": 0}

    end = file_size if before_offset is None else max(0, min(before_offset, file_size))
    start = max(0, end - limit_bytes)
    lines = _iter_lines_range(conversation_path, start, end)

    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        row = _project_frame(frame)
        if row is not None:
            rows.append(row)

    return {
        "rows": rows,
        "has_older": start > 0,
        "oldest_offset": start,
        "file_size": file_size,
    }
