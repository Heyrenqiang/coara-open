"""挂起工具的搜索与装载 — 挂起机制的核心入口。

与 skill(action=list|activate) 对称：
- search：关键词搜索挂起池，返回候选名 + 描述（只看不用，不装载）
- activate：按精确名称装载工具 schema，之后本轮即可调用
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.core.tool_base import (
    BaseTool,
    ToolInvocation,
    ToolKind,
    ToolResult,
)

# 工具名的合法字符集（activate 白名单校验）
_TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_]+")

# 中文口语/同义词 → 额外匹配词（用于工具名与英文描述）
_QUERY_SYNONYMS: dict[str, list[str]] = {
    "截图": ["截图", "截取", "截屏", "screen", "screenshot", "capture"],
    "截屏": ["截图", "截取", "截屏", "screen", "screenshot", "capture"],
    "压缩": ["压缩", "compress", "jpeg", "jpg"],
    "邮件": ["邮件", "email", "mail", "inbox"],
    "邮箱": ["邮件", "email", "mail", "inbox"],
    "打印": ["打印", "print", "printer", "打印机"],
}


def _expand_search_keywords(query: str) -> list[str]:
    """Expand a user/LLM query into lowercase keyword tokens for fuzzy tool search."""
    raw = query.strip().lower()
    if not raw or raw.startswith("+"):
        return [raw] if raw else []

    tokens = raw.split()
    expanded: set[str] = set(tokens)
    for token in tokens:
        for key, aliases in _QUERY_SYNONYMS.items():
            if token == key or token in aliases:
                expanded.update(aliases)
    return list(expanded)


def _score_deferred_tool(name: str, description: str, keywords: list[str]) -> int:
    name_lower = name.lower()
    text = f"{name_lower} {description.lower()}"
    score = 0
    for kw in keywords:
        if kw in name_lower:
            score += 10
        elif kw in text:
            score += 3
    return score


def dynamic_loading_active_for(coara: Any) -> bool:
    """K3 dynamic tool loading 是否对当前宿主生效（kimi OpenAI 端 + k3 模型）。"""
    try:
        from src.llm.active_context import resolve_active_llm
        from src.llm.endpoints import is_kimi_openai_endpoint

        ctx = resolve_active_llm(coara)
        return ctx.driver == "openai" and is_kimi_openai_endpoint(ctx.base_url) and ctx.model.lower().startswith("k3")
    except Exception:  # noqa: BLE001 — 判定失败按关闭处理
        return False


def append_tool_declaration(coara: Any, tool: Any) -> bool:
    """把工具完整定义以声明消息追加到宿主历史尾部（纯后缀，不动前缀缓存）。

    仅 kimi+k3 宿主生效；其它宿主返回 False（声明留在历史也无害，但不追加）。
    """
    if not dynamic_loading_active_for(coara):
        return False
    from src.core.message_tags import tool_declaration
    from src.core.types import Message, MessageRole
    from src.llm.tool_arguments import sanitize_tool_parameters

    payload = json.dumps(
        {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": sanitize_tool_parameters({"parameters": tool.parameters_schema}),
                    },
                }
            ]
        },
        ensure_ascii=False,
    )
    coara.message_history.append(Message(role=MessageRole.USER, content=tool_declaration(payload)))
    return True


class ToolGatewayInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], coara: Any) -> None:
        super().__init__(params)
        self._coara = coara

    def get_description(self) -> str:
        action = str(self.params.get("action", "")).strip().lower()
        name = str(self.params.get("name", "") or "").strip()
        query = str(self.params.get("query", "") or "").strip()
        if action == "activate" and name:
            return f"装载挂起工具：{name}"
        if action == "search":
            return f"搜索挂起工具：{query or '全量目录'}"
        detail = query or name or action
        return f"挂起工具 {action}: {detail}"

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action", "")).strip().lower()
        if action in ("search", "activate"):
            # 先重扫磁盘工具包：会话中新建的工具包无需 /new 即可被发现
            try:
                from src.tools.dynamic.loader import register_dynamic_tools

                register_dynamic_tools(self._coara)
            except Exception:  # noqa: BLE001 — 扫描失败不阻塞网关本体
                pass
        if action == "search":
            return self._execute_search()
        if action == "activate":
            return self._execute_activate()
        return ToolResult.error(f"未知 action：{action}。支持 search / activate")

    # ── search：只看不用 ─────────────────────────────────────────

    def _execute_search(self) -> ToolResult:
        query = str(self.params.get("query", "")).strip()

        manager = self._coara._tool_manager
        deferred = manager.get_deferred_tool_summaries(self._coara.identity.is_owner_context)

        if not deferred:
            return ToolResult.success("当前挂起工具池为空。")

        # 空 query：返回全量目录（名字 + 描述），供模型通览
        if not query:
            lines = [f"挂起工具池共 {len(deferred)} 个工具：", ""]
            for d in deferred[:30]:
                lines.append(f"- **{d['name']}** — {d['description']}")
            if len(deferred) > 30:
                lines.append(f"- … 另有 {len(deferred) - 30} 个，可用关键词缩小范围")
            return ToolResult.success("\n".join(lines))

        matched: list[tuple[str, str, int]] = []  # (name, description, score)

        # 模式 1: +must-word other — 强制包含词
        if query.startswith("+"):
            parts = query.split()
            must_words = [p[1:].lower() for p in parts if p.startswith("+")]
            other_words = [p.lower() for p in parts if not p.startswith("+")]
            for d in deferred:
                text = (d["name"] + " " + d["description"]).lower()
                if all(w in text for w in must_words):
                    score = sum(1 for w in other_words if w in text)
                    matched.append((d["name"], d["description"], score))
        # 模式 2: 自由关键词搜索（含同义词扩展）
        else:
            keywords = _expand_search_keywords(query)
            for d in deferred:
                score = _score_deferred_tool(d["name"], d["description"], keywords)
                if score > 0:
                    matched.append((d["name"], d["description"], score))

        matched.sort(key=lambda x: -x[2])
        matched = matched[:10]

        if not matched:
            available = ", ".join(d["name"] for d in deferred[:12])
            return ToolResult.success(f"挂起工具池中未找到与 {query} 匹配的工具。当前可搜索：{available}")

        lines = [f"找到 {len(matched)} 个匹配的挂起工具：", ""]
        for name, desc, _ in matched:
            lines.append(f"- **{name}** — {desc}")
        return ToolResult.success("\n".join(lines))

    # ── activate：按名装载 ───────────────────────────────────────

    def _execute_activate(self) -> ToolResult:
        name = str(self.params.get("name", "")).strip()
        if not name:
            return ToolResult.error("activate 需要提供 name 参数")
        if not _TOOL_NAME_RE.fullmatch(name):
            return ToolResult.error(f"非法工具名：{name}")

        manager = self._coara._tool_manager
        tool = manager.tools.get(name)
        if tool is None or not tool.should_defer:
            return ToolResult.error(
                f'挂起工具池中没有名为 {name} 的工具。用 `tool(action="search", query="…")` 搜索可用工具'
            )
        if tool.owner_only and not self._coara.identity.is_owner_context:
            return ToolResult.error(f"工具 {name} 仅所有者可装载")

        if name in manager._revealed:
            return ToolResult.success(f"工具 {name} 已在本会话装载过，可直接调用。")
        if not manager.reveal_tool(name):
            return ToolResult.error(f"工具 {name} 装载失败")

        # K3 dynamic tool loading：声明消息必须在对应 tool_result 之后追加，
        # 否则 assistant(tool_calls) 与 tool 响应之间会被声明隔开，严格端点 400。
        will_declare = dynamic_loading_active_for(self._coara)
        metadata: dict[str, Any] = {}
        if will_declare:
            metadata["pending_tool_declaration"] = name

        # 不重建静态 prompt：挂起清单是会话级静态的，激活只解锁 tool schema
        # 可见性（reveal_tool 内部已置空 _tool_definitions_cache），前缀保持一致
        note = "（工具定义已以声明消息进对话尾部，本请求起可见，前缀缓存不受影响。）" if will_declare else ""
        return ToolResult.success(
            f"已装载工具 **{name}** — {tool.description.split(chr(10))[0]}，本轮直接调用。{note}",
            metadata=metadata or None,
        )


class ToolGatewayTool(BaseTool):
    """挂起工具的统一入口：search 搜索候选，activate 按名装载。"""

    name = "tool"
    description = """挂起工具的统一入口。挂起工具**不会**出现在 system prompt 或常驻工具列表里，只能通过本工具按需装载。挂起池包含两类，被挂起的内置工具（直接列在 system prompt 的挂起工具清单里，activate 即可，无需 search）；磁盘工具包（用户级 `<coara_home>/users/default/tools/` 与工作空间级 `.coara/tools/` 下的自建工具，search 可发现）。需要的能力不存在时可激活 tool-creator 技能创建新工具包

| action | 用途 |
|--------|------|
| `search` | 按关键词搜索挂起池，返回候选名 + 描述，只看不用，不装载；query 支持自由关键词（含中文同义词）与 `+必须包含` 语法；**query 留空则返回全量目录** |
| `activate` | 按精确名称装载工具，name 取自 search 结果或 prompt 里的挂起工具清单 |

适用
- 用户需要的能力不在常驻工具里（截图、发邮件等），先 search 或按清单 activate
- 装载 prompt 挂起工具清单里的内置工具（如 collection），直接 activate

不适用
- 能力已在常驻工具中，直接调用对应工具
- **不要**在未装载的情况下声称具备某扩展能力
- 挂起池为空时无法装载

装载流程，必须遵守
1. 按名字 activate 装载；名字看不出用途时先 search 看描述再决定
2. activate 成功后本轮继续调用已装载的工具
3. 本会话内已装载的工具保持可调用；`/new` 后需重新装载

装载只解锁 function schema；具体参数以装载后的工具描述为准。"""
    display_name = "挂起工具"
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "activate"],
                "description": "search=搜索候选，不装载；activate=按名装载",
            },
            "query": {
                "type": "string",
                "description": "search 用的搜索关键词，支持 +强制包含；留空返回全量目录",
            },
            "name": {
                "type": "string",
                "description": "activate 用的精确工具名",
            },
        },
        "required": ["action"],
    }
    kind = ToolKind.SEARCH
    should_defer = False  # 入口工具本身不挂起

    def __init__(self, parent_coara: Any = None) -> None:
        self._parent_coara = parent_coara
        super().__init__()

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return ToolGatewayInvocation(params, self._parent_coara)
