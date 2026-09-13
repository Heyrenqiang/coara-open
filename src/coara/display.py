"""Tool argument formatting and display helpers for terminal output.

Pure presentation logic — no business state, no LLM calls.
"""

from __future__ import annotations

from typing import Any


def _truncate(text: str, max_len: int | None) -> str:
    if max_len is None or len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _summarize_string_list(items: Any, *, max_len: int | None) -> str | None:
    if not isinstance(items, list):
        if isinstance(items, str) and items.strip():
            return _truncate(items.strip(), max_len)
        return None
    strings = [str(x).strip() for x in items if str(x).strip()]
    if not strings:
        return None
    head = _truncate(strings[0], max_len)
    extra = len(strings) - 1
    return f"{head} +{extra}" if extra > 0 else head


def summarize_web_search_arguments(arguments: dict[str, Any], *, max_len: int | None = 30) -> str | None:
    """Compact label for search(queries) spinner lines."""
    queries = arguments.get("queries")
    if isinstance(queries, list) and queries:
        text = ", ".join(str(q) for q in queries)
        return _truncate(text, max_len)
    query = str(arguments.get("query") or "").strip()
    return _truncate(query, max_len) if query else None


def format_tool_call_label(tool_name: str, arguments: Any, *, max_len: int | None = 30) -> str:
    """Single-line tool label for CLI scrollback / spinner / Matrix progress text.

    Pass ``max_len=None`` for no truncation (CLI ``✓ tool(...)`` lines).
    """
    if not arguments:
        return tool_name
    if not isinstance(arguments, dict):
        return tool_name
    args = dict(arguments)

    if tool_name == "web_search":
        summary = summarize_web_search_arguments(args, max_len=max_len)
        if summary:
            return f"{tool_name}({summary})"

    if tool_name == "shell":
        command = args.get("command")
        if command is not None and str(command).strip():
            text = str(command).strip()
            first, *rest = text.splitlines()
            suffix = " …" if rest else ""
            return f"{tool_name}({_truncate(first, max_len)}{suffix})"

    if tool_name == "read":
        path = args.get("path")
        ref = args.get("ref")
        if path is not None and str(path).strip():
            return f"{tool_name}({_truncate(str(path), max_len)})"
        if ref is not None and str(ref).strip():
            return f"{tool_name}(ref={_truncate(str(ref), max_len)})"

    if tool_name == "glob":
        pattern = args.get("pattern")
        path = args.get("path")
        if pattern is not None and str(pattern).strip():
            label = str(pattern)
            if path is not None and str(path).strip():
                label = f"{label} @ {path}"
            return f"{tool_name}({_truncate(label, max_len)})"

    if tool_name == "grep":
        pattern = args.get("pattern")
        path = args.get("path")
        if pattern is not None and str(pattern).strip():
            label = str(pattern)
            if path is not None and str(path).strip():
                label = f"{label} @ {path}"
            return f"{tool_name}({_truncate(label, max_len)})"

    if tool_name == "todo":
        # 必填 description 是给用户看的操作说明：action + description 一起显示，
        # 如 ``✓ todo(update 标记完成)``。缺 description 时退化为 action。
        bits: list[str] = []
        action = str(args.get("action") or "").strip()
        if action:
            bits.append(action)
        desc = str(args.get("description") or "").strip()
        if desc:
            bits.append(desc)
        if bits:
            return f"{tool_name}({_truncate(' '.join(bits), max_len)})"

    if tool_name == "ws":
        action = args.get("action")
        name = args.get("name") or args.get("workspace")
        if action is not None and str(action).strip():
            label = str(action)
            if name is not None and str(name).strip():
                label = f"{label} {name}"
            return f"{tool_name}({_truncate(label, max_len)})"

    if tool_name == "skill":
        action = args.get("action")
        name = args.get("name")
        if action is not None and str(action).strip():
            label = str(action)
            if name is not None and str(name).strip():
                label = f"{label} {name}"
            return f"{tool_name}({_truncate(label, max_len)})"
        if name is not None and str(name).strip():
            return f"{tool_name}({_truncate(str(name), max_len)})"

    if tool_name == "tool":
        # 挂起工具网关：activate/search 需展示目标工具名或搜索词，避免裸 ``tool(activate)``
        action = str(args.get("action") or "").strip()
        name = str(args.get("name") or "").strip()
        query = str(args.get("query") or "").strip()
        action_cn = {"search": "搜索", "activate": "装载"}.get(action, action)
        bits: list[str] = []
        if action_cn:
            bits.append(action_cn)
        if action == "activate" and name:
            bits.append(name)
        elif action == "search" and query:
            bits.append(query)
        elif name:
            bits.append(name)
        if bits:
            return f"{tool_name}({_truncate(' '.join(bits), max_len)})"

    if tool_name in ("delegate", "orchestrator"):
        # Prefer type + description; generic loop would stop at description alone.
        # Spinner line: ``delegate coaras: 摸底…  (12s · 2.8k tok)``
        sub = str(args.get("subagent_type") or "").strip()
        desc = str(args.get("description") or "").strip()
        ws = str(args.get("workspace") or "").strip()
        action = str(args.get("action") or "spawn").strip()
        flow = str(args.get("flow") or "").strip()
        node_id = str(args.get("node_id") or "").strip()
        bits: list[str] = []
        if sub:
            bits.append(sub)
        if args.get("background"):
            bits.append("bg")
        if ws:
            bits.append(f"@{ws}")
        head = " ".join(bits)
        label = f"{head}: {desc}" if head and desc else head or desc
        # 编排参数（orchestrator 带 flow 参数）：显示组网信息，
        # 让用户一眼看到这个编排在织哪张图、挂哪个节点、依赖谁、流向谁
        if flow:
            wf = f"flow={flow}"
            if node_id:
                wf += f"/{node_id}"
            deps = [str(d) for d in (args.get("depends_on") or []) if str(d).strip()]
            routes = [str(r) for r in (args.get("routes_to") or []) if str(r).strip()]
            if deps:
                wf += f" ←{','.join(deps)}"
            if routes:
                wf += f" →{','.join(routes)}"
            label = f"{label} [{wf}]" if label else wf
        if action != "spawn":
            extra = str(args.get("draft_id") or args.get("instance_id") or "").strip()
            label = " ".join(x for x in (action, label, extra) if x) or action
        if not label:
            return tool_name
        return f"{tool_name} {_truncate(label, max_len)}"

    for key in ("file_path", "path", "url", "query", "command", "description", "subagent_type"):
        val = args.get(key)
        if val is not None and str(val).strip():
            return f"{tool_name}({_truncate(str(val), max_len)})"

    for val in args.values():
        if isinstance(val, str) and val.strip():
            return f"{tool_name}({_truncate(val, max_len)})"
        if isinstance(val, list):
            summary = _summarize_string_list(val, max_len=max_len)
            if summary:
                return f"{tool_name}({summary})"

    return tool_name
