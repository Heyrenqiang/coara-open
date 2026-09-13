"""Tool registration, visibility filtering, and caching."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.core.tool_base import BaseTool


class ToolManager:
    """Manages the tool registry, whitelist filtering, plan mode, and definition caching."""

    # Tools allowed in plan mode (all others are hidden when plan_mode is active).
    _PLAN_MODE_ALLOWED: set[str] = {
        "read",
        "glob",
        "grep",
        "web_search",
        "web_fetch",
        "delegate",
        "todo",
        "write",
        "edit",
        "plan_mode",
    }

    def __init__(self, owner: Any | None = None) -> None:
        self._owner = owner  # 宿主 CoaraBase（动态声明补发等宿主协作用；可空=纯工具管理）
        self._tools: dict[str, BaseTool] = {}
        self._tool_whitelist: set[str] | None = None
        self._disabled: set[str] = set()  # 主会话工具开关：禁用 = 不注入 prompt + 执行被拒
        self._tool_definitions_cache: list[dict[str, Any]] | None = None
        self._deferred: set[str] = set()  # 挂起的工具名
        self._revealed: set[str] = set()  # 已揭示的挂起工具名
        # reveal 持久化（#92）：从首个注册的工作空间绑定工具（WorkspaceBoundTool）
        # 继承 workspace root，revealed 集合落盘到该空间的 session 状态目录；
        # 未注册任何工作空间工具时持久化自动关闭（纯内存，行为与旧版一致）
        self._persistence_workspace: Path | None = None
        self._reveal_restore_loaded = False
        self._pending_restored_reveals: set[str] = set()
        self._plan_mode = False
        self._plan_file_path: Path | None = None

    @property
    def tools(self) -> dict[str, BaseTool]:
        return self._tools

    def set_whitelist(self, whitelist: set[str] | None) -> None:
        self._tool_whitelist = whitelist

    def get_disabled_names(self) -> list[str]:
        """Sorted disabled tool names (tools.disabled 主会话工具开关)."""
        return sorted(self._disabled)

    def set_disabled(self, disabled: set[str] | list[str] | None) -> None:
        """配置主会话工具开关（tools.disabled）：禁用的工具不注入 prompt 且执行被拒。"""
        self._disabled = set(disabled or [])
        # 禁用收回已装载的挂起工具，保证彻底不可见
        self._revealed.difference_update(self._disabled)
        self._tool_definitions_cache = None

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def register_tool(self, tool: BaseTool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            logger.warning(f"Tool already registered: {tool.name}, skipping")
            return
        self._tools[tool.name] = tool
        if tool.should_defer:
            self._deferred.add(tool.name)
        else:
            self._deferred.discard(tool.name)
        self._tool_definitions_cache = None
        logger.debug(f"Tool registered: {tool.name} (defer={tool.should_defer})")
        if self._persistence_workspace is None:
            workspace_root = getattr(tool, "_workspace_root", None)
            if isinstance(workspace_root, Path):
                self._persistence_workspace = workspace_root
        self._ensure_reveal_restore_loaded()
        self._apply_pending_restored_reveal(tool.name)

    def register_tools(self, tools: list[BaseTool]) -> None:
        for tool in tools:
            self.register_tool(tool)

    def invalidate_cache(self) -> None:
        self._tool_definitions_cache = None

    def enter_plan_mode(self, plan_file_path: Path) -> None:
        self._plan_mode = True
        self._plan_file_path = plan_file_path
        self._tool_definitions_cache = None
        logger.info(f"Plan mode activated. Plan file: {plan_file_path}")

    def exit_plan_mode(self) -> None:
        self._plan_mode = False
        self._plan_file_path = None
        self._tool_definitions_cache = None
        logger.info("Plan mode deactivated.")

    @property
    def is_plan_mode(self) -> bool:
        return self._plan_mode

    def get_bound_tool_names(self) -> list[str]:
        if isinstance(self._tool_whitelist, set):
            return list(self._tool_whitelist)
        return []

    def get_visible_tool_names(self, is_owner_ctx: bool) -> list[str]:
        """Visible tool names only — no schema deepcopy (for delegate whitelist inherit)."""
        tool_whitelist = self.get_bound_tool_names()
        names: list[str] = []
        for tool in self.tools.values():
            if tool.name in self._disabled:
                continue
            if not self._whitelist_allows(tool, tool_whitelist):
                continue
            if tool.owner_only and not is_owner_ctx:
                continue
            if self._plan_mode and tool.name not in self._PLAN_MODE_ALLOWED:
                continue
            names.append(tool.name)
        return names

    @staticmethod
    def _whitelist_allows(tool: BaseTool, tool_whitelist: list[str]) -> bool:
        """Agent YAML whitelist applies to all tools."""
        if not tool_whitelist:
            return True
        return tool.name in tool_whitelist

    def get_visible_tool_definitions(self, is_owner_ctx: bool) -> list[dict[str, Any]]:
        """Return visible tool definitions; approval meta only for the main session.

        ``require_approval`` / ``approval_reason`` are injected for write/edit/delete/shell
        on the user-facing root only. Subagents (and non-user-facing maintenance agents)
        never see those params — their calls also skip the approval gate.
        """
        visible: list[dict[str, Any]] = []
        tool_whitelist = self.get_bound_tool_names()
        inject_meta = self._should_inject_approval_meta()

        for tool in self._tools.values():
            if tool.name in self._disabled:
                continue
            if not self._whitelist_allows(tool, tool_whitelist):
                continue
            if tool.owner_only and not is_owner_ctx:
                continue
            if self._plan_mode and tool.name not in self._PLAN_MODE_ALLOWED:
                continue
            definition = dict(tool.definition)
            params = definition.get("parameters", {})
            definition["parameters"] = (
                _inject_approval_meta_params(tool.name, params) if inject_meta else (params or {"type": "object"})
            )
            visible.append(definition)

        return visible

    def _should_inject_approval_meta(self) -> bool:
        """主会话才注入审批元参数；子智能体 / janitor / daily 不注入。

        不注入 ≠ 不弹窗：工具类 ``requires_approval`` 仍可能触发审批门。
        门禁跳过另见 ``tool_policy._skips_call_approval``（depth / user_facing / 系统维护名）。
        """
        owner = self._owner
        if owner is None:
            return True
        # 只用真实 int（MagicMock 的 int() 默认是 1，不能拿来当深度）
        depth = getattr(owner, "delegate_depth", 0)
        if type(depth) is int and depth >= 1:
            return False
        identity = getattr(owner, "identity", None)
        if identity is not None and getattr(identity, "user_facing", True) is False:
            return False
        # daily「记录」会话 user_facing=True、depth=0，但仍是系统维护 persona——
        # schema 里的 require_approval 对它无意义（审批门已按 persona 跳过）。
        persona = getattr(identity, "persona", None) if identity is not None else None
        name = str(getattr(persona, "name", "") or "").strip().lower()
        if name:
            from src.coara.builtin_agents import SYSTEM_ONLY_SUBAGENT_TYPES

            if name in SYSTEM_ONLY_SUBAGENT_TYPES:
                return False
        return True

    def dump_visibility(self, is_owner_ctx: bool) -> dict[str, Any]:
        """Return a debug map explaining why each registered tool is visible or hidden."""
        tool_whitelist = self.get_bound_tool_names()
        visible: list[str] = []
        hidden: dict[str, list[str]] = {}

        for tool in self._tools.values():
            reasons: list[str] = []
            if tool.name in self._disabled:
                reasons.append("disabled")
            if not self._whitelist_allows(tool, tool_whitelist):
                reasons.append("not_in_whitelist")
            if tool.owner_only and not is_owner_ctx:
                reasons.append("owner_only")
            if self._plan_mode and tool.name not in self._PLAN_MODE_ALLOWED:
                reasons.append("plan_mode_restricted")
            if reasons:
                hidden[tool.name] = reasons
            else:
                visible.append(tool.name)
        return {"visible": visible, "hidden": hidden}

    def get_tool_definitions_for_llm(self, is_owner_ctx: bool) -> list[dict[str, Any]]:
        """Return tool definitions for LLM function calling.

        Deferred tools are excluded unless they have been revealed. Approval
        meta-parameters are already injected by ``get_visible_tool_definitions``.
        """
        if self._tool_definitions_cache is not None:
            return self._tool_definitions_cache
        definitions: list[dict[str, Any]] = []
        for td in self.get_visible_tool_definitions(is_owner_ctx):
            name = td.get("name", "")
            if self._is_deferred_and_hidden(name):
                continue
            definitions.append(
                {
                    "name": name,
                    "description": td.get("description", ""),
                    "parameters": td.get("parameters", {}),
                }
            )
        self._tool_definitions_cache = definitions
        return definitions

    def _is_deferred_and_hidden(self, name: str) -> bool:
        if name not in self._deferred:
            return False
        # K3 dynamic tool loading：挂起工具只经历史声明消息装载，不进请求级 tools
        # （与 request-level tools 并存会 400 duplicate tool name）
        if self._owner is not None:
            try:
                from src.tools.builtin.integration.tool import dynamic_loading_active_for

                if dynamic_loading_active_for(self._owner):
                    return True
            except Exception:  # noqa: BLE001 — 判定失败按常规 deferred 语义
                pass
        return name not in self._revealed

    def reveal_tool(self, name: str) -> bool:
        """Reveal a deferred tool so it becomes visible to the LLM.

        Returns True if the tool was revealed, False if not found or not deferred.
        """
        if name not in self._tools:
            return False
        if name not in self._deferred:
            return False
        if name in self._disabled:
            return False
        self._revealed.add(name)
        self._tool_definitions_cache = None
        self._persist_revealed()
        logger.info(f"Tool revealed: {name}")
        return True

    def clear_revealed(self) -> None:
        """Clear all revealed tools (e.g. on session reset)."""
        self._revealed.clear()
        self._pending_restored_reveals.clear()
        self._tool_definitions_cache = None
        self._persist_revealed()

    # ── reveal 持久化（#92）──

    def _ensure_reveal_restore_loaded(self) -> None:
        """Load persisted revealed names once a workspace is known.

        恢复时校验工具仍注册：已注册且仍为挂起工具 → 直接恢复可见；
        已注册但不再是挂起工具 → 天然可见，丢弃；尚未注册的暂存
        ``_pending_restored_reveals``——待其注册时再生效
        （永不注册的条目仅存内存，不回写磁盘，等同丢弃）
        """
        if self._reveal_restore_loaded or self._persistence_workspace is None:
            return
        self._reveal_restore_loaded = True
        from src.coara.workspace_state import load_revealed_tools

        try:
            restored = load_revealed_tools(self._persistence_workspace)
        except Exception:
            logger.debug("Failed to load persisted revealed tools", exc_info=True)
            return
        for name in restored:
            self._pending_restored_reveals.add(name)
            self._apply_pending_restored_reveal(name)

    def _apply_pending_restored_reveal(self, name: str) -> None:
        if name not in self._pending_restored_reveals:
            return
        if name not in self._tools:
            return
        self._pending_restored_reveals.discard(name)
        if name in self._deferred:
            self._revealed.add(name)
            self._tool_definitions_cache = None
            self._reissue_dynamic_declaration(name)
            logger.info(f"Restored revealed tool from disk: {name}")

    def _reissue_dynamic_declaration(self, name: str) -> None:
        """K3 dynamic tool loading：恢复的 revealed 若缺声明消息则补发一条。

        声明消息只在首次 activate 时进历史一次；跨重启恢复 revealed 后，会话若是
        全新（/new 或旧历史无声明），模型将看不到工具定义（K3 动态声明不随请求
        自动携带）。此处按宿主当前历史对账：已有同名声明跳过，缺失则追加。
        非 kimi+k3 宿主由 _append_tool_declaration_if_supported 内部判定跳过。
        """
        owner = self._owner
        tool = self._tools.get(name)
        if owner is None or tool is None:
            return
        try:
            from src.core.message_tags import strip_tool_declaration

            for msg in getattr(owner, "message_history", []) or []:
                content = getattr(msg, "content", None)
                if not isinstance(content, str):
                    continue
                decl = strip_tool_declaration(content)
                if decl and f'"name": "{tool.name}"' in decl:
                    return  # 声明已在历史中（会话恢复场景）
            from src.tools.builtin.integration.tool import append_tool_declaration

            append_tool_declaration(owner, tool)
        except Exception:  # noqa: BLE001 — 补发失败不阻塞恢复（执行通道仍在）
            logger.debug(f"reissue dynamic declaration failed: {name}", exc_info=True)

    def _persist_revealed(self) -> None:
        if self._persistence_workspace is None:
            return
        from src.coara.workspace_state import save_revealed_tools

        try:
            save_revealed_tools(self._persistence_workspace, self._revealed)
        except Exception:
            logger.debug("Failed to persist revealed tools", exc_info=True)

    def get_deferred_tool_summaries(
        self,
        is_owner_ctx: bool,
        *,
        include_revealed: bool = False,
    ) -> list[dict[str, str]]:
        """Return name + short description for deferred pool tools (tool gateway).

        Excludes already-loaded tools unless ``include_revealed=True``.
        """
        summaries: list[dict[str, str]] = []
        tool_whitelist = self.get_bound_tool_names()

        for tool in self._tools.values():
            if not tool.should_defer:
                continue
            if tool.name in self._disabled:
                continue
            if not include_revealed and tool.name in self._revealed:
                continue
            if not self._whitelist_allows(tool, tool_whitelist):
                continue
            if tool.owner_only and not is_owner_ctx:
                continue
            if self._plan_mode and tool.name not in self._PLAN_MODE_ALLOWED:
                continue
            summary_text = (tool.summary or "").strip() or (tool.description or "").split("\n")[0].strip()
            if len(summary_text) > 160:
                summary_text = summary_text[:159] + "…"
            summaries.append({"name": tool.name, "description": summary_text})

        summaries.sort(key=lambda x: x["name"])
        return summaries


# Meta-parameters injected into select tool schemas so the LLM can request
# human approval per-call. Stripped before create_invocation (see executor).
_REQUIRE_APPROVAL_PARAM: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "需要用户审批时设为 true"
    ),
    "default": False,
}

_APPROVAL_REASON_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "需要审批的原因",
    "default": "",
}

# Tools whose schemas carry the approval meta-parameters.
# Kept small to avoid schema bloat; root.md already instructs the model
# on when to request approval.
_APPROVAL_META_TOOL_NAMES: frozenset[str] = frozenset({"write", "edit", "delete", "shell"})


def _inject_approval_meta_params(tool_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Return schema with approval meta-parameters injected for high-risk tools.

    Non-approval tools return the original schema object (no deepcopy) — callers
    must not mutate it. Approval tools get a deep copy before injection.
    """
    if tool_name not in _APPROVAL_META_TOOL_NAMES:
        return schema if schema else {"type": "object"}
    cloned = copy.deepcopy(schema) if schema else {"type": "object"}
    if cloned.get("type") is None:
        cloned["type"] = "object"
    props = cloned.setdefault("properties", {})
    if not isinstance(props, dict):
        props = {}
        cloned["properties"] = props
    if "require_approval" not in props:
        props["require_approval"] = dict(_REQUIRE_APPROVAL_PARAM)
    if "approval_reason" not in props:
        props["approval_reason"] = dict(_APPROVAL_REASON_PARAM)
    return cloned
