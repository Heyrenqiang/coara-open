"""In-memory live activity tree for CLI (Channel B + C flush rules).

Uses ``workspace_trace_event`` so CLI shares one in-memory activity projection
with other surfaces. Disk dual-write to ``activity/`` is discontinued; canonical
persistence is ``traces/trace_events.jsonl``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock

from src.cli.activity_types import (
    MAX_ACTIVE_TOOLS_PER_DELEGATE,
    MAX_STATUS_OVERFLOW_HINT,
    MAX_STATUS_TOTAL_LINES,
    ToolCallBlock,
    should_flush_tool_to_history,
)
from src.coara.builtin_agents import CLI_SILENT_SUBAGENT_TYPES
from src.coara.display import format_tool_call_label
from src.core.events import TraceEvent
from src.core.text import format_elapsed
from src.llm.usage import cache_read_tokens, total_prompt_tokens
from src.ui.activity_store import workspace_trace_event

# System subagents: no live tree rows, no ✓ flush to scrollback (Channel B/C).
# janitor/daily 完全静默 默默工作 不占位不显示。
_CLI_SILENT_SUBAGENT_TYPES = CLI_SILENT_SUBAGENT_TYPES

_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "subagent_start",
        "subagent_complete",
        "subagent_failed",
        "flow_started",
        "flow_finished",
        "background_agent_start",
        "background_agent_complete",
        "tool_start",
        "tool_complete",
        "process_spawned",
        "coara_created",
        "coara_terminated",
        "thinking_progress",
        "llm_turn_complete",
    }
)


@dataclass(slots=True)
class _LiveNode:
    node_id: str
    parent_id: str
    label: str
    kind: str
    coara_id: str = ""
    subagent_type: str = ""
    status: str = ""  # ""=运行中/默认亮色；"pending"=停车灰色静态（flow 节点未启动）
    active: bool = True
    is_error: bool = False
    order: float = field(default_factory=time.monotonic)
    # 最近一次生命周期事件时间（对账丢帧兜底：超 120s 无更新强制 inactive）。
    last_event: float = field(default_factory=time.monotonic)


# 对账兜底：**无活跃委派容器祖先**的孤立节点超此时长无新事件即判 inactive
# （丢 subagent_complete 等终态帧后子树不再僵死）。
# 注意：委派进行中（delegate/background/flow 容器 active）的子树不参与此判定——
# 其收口靠终态帧与回合收尾，判活靠 tool/llm_turn_complete/thinking_progress
# 事件心跳；LLM 单次调用或长工具可远超分钟级，静默阈值取 600s 只做极端兜底。
_STALE_NODE_TIMEOUT_S = 600.0


@dataclass(slots=True)
class _DelegateLlmUsage:
    """Latest-turn provider usage for one running delegate subtree (context 口径).

    与主会话侧栏的上下文用量同口径：只保留最后一轮的 prompt（≈子智能体当前
    context 大小），不做跨轮累计（累计是计费口径，数字虚高且随轮数膨胀）。
    """

    last_prompt_tokens: int = 0
    last_cache_read_tokens: int = 0
    llm_turns: int = 0

    @property
    def cache_hit_ratio(self) -> float | None:
        # 口径唯一实现：llm.usage.cumulative_prompt_cache_hit_ratio（None = 无命中数据）
        from src.llm.usage import cumulative_prompt_cache_hit_ratio

        return cumulative_prompt_cache_hit_ratio(
            cumulative_cache_read_tokens=self.last_cache_read_tokens,
            cumulative_prompt_tokens=self.last_prompt_tokens,
        )


class ActivityLiveTracker:
    """Activity-tree state machine for the live CLI spinner."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._session_id = ""
        self._nodes: dict[str, _LiveNode] = {}
        self._finished_blocks: list[ToolCallBlock] = []
        self._coara_to_activity: dict[str, str] = {}
        self._active_delegate_ids: set[str] = set()
        self._node_started_at: dict[str, float] = {}
        self._delegate_llm_usage: dict[str, _DelegateLlmUsage] = {}
        # 静默子智能体（janitor/daily）的 coara_id：其 tool_start 不建行
        self._silent_coara_ids: set[str] = set()

    def bind_session(self, session_id: str) -> None:
        with self._lock:
            self._session_id = session_id

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()
            self._finished_blocks.clear()
            self._coara_to_activity.clear()
            self._active_delegate_ids.clear()
            self._node_started_at.clear()
            self._delegate_llm_usage.clear()

    def resync(self) -> None:
        """断连重连/整树重建兜底：清掉所有可能丢帧的节点，与服务端权威状态对齐。

        不清 _finished_blocks（已完成工具行仍需 flush 到滚动区），只清活动树。
        """
        with self._lock:
            self._nodes.clear()
            self._active_delegate_ids.clear()
            self._node_started_at.clear()
            self._delegate_llm_usage.clear()

    def clear_root_status(self) -> None:
        with self._lock:
            for node in list(self._nodes.values()):
                if node.parent_id:
                    continue
                node.active = False
                self._node_started_at.pop(node.node_id, None)
            self._prune_inactive()

    def ingest(self, event: TraceEvent) -> None:
        if event.event_type not in _LIFECYCLE_EVENT_TYPES:
            return
        # 子智能体的思考预览不进动态区：内心独白不是状态，且多个分身共享一个
        # 展示槽会互相覆盖。但它是子智能体「活着并在推进」的强心跳——长 LLM
        # 调用期间正是靠它（或 llm_turn_complete）续命，故仍刷新对应节点。
        payload = dict(event.payload or {})
        activity = workspace_trace_event(event)
        event_at = time.monotonic()

        with self._lock:
            if event.event_type == "thinking_progress":
                self._touch_coara(str(event.coara_id or payload.get("coara_id") or ""), event_at)
                return
            if event.event_type == "llm_turn_complete":
                self._touch_coara(str(event.coara_id or ""), event_at)
                self._record_llm_turn(event.coara_id, payload)
                return
            if event.event_type == "subagent_start":
                subagent_id = str(payload.get("subagent_id") or "")
                child_coara_id = str(payload.get("child_coara_id") or "")
                agent_type = str(payload.get("subagent_type") or "").strip()
                # 静默子智能体（janitor/daily）：不建活动树行 但记 coara_id 防其工具行漏出
                if agent_type in _CLI_SILENT_SUBAGENT_TYPES:
                    if child_coara_id:
                        self._silent_coara_ids.add(child_coara_id)
                    return
                if subagent_id:
                    parent_id = self._resolve_parent_tool_id(payload)
                    label = self._format_agent_label(
                        agent_type or "subagent",
                        str(payload.get("description") or "").strip(),
                    )
                    status = str(payload.get("status") or "")
                    self._upsert(
                        subagent_id,
                        parent_id,
                        label,
                        "subagent",
                        child_coara_id,
                        subagent_type=agent_type,
                        status=status,
                    )
                    if child_coara_id:
                        self._coara_to_activity[child_coara_id] = subagent_id
                    self._touch(subagent_id, event_at)
                return

            if event.event_type == "flow_started":
                flow_name = str(payload.get("flow") or "")
                if flow_name:
                    self._upsert(
                        f"flow-{flow_name}",
                        "",
                        f"flow {flow_name}",
                        "flow",
                    )
                    self._touch(f"flow-{flow_name}", event_at)
                return

            if event.event_type == "flow_finished":
                flow_name = str(payload.get("flow") or "")
                if flow_name:
                    self._finish(f"flow-{flow_name}")
                return

            if event.event_type in {"subagent_complete", "subagent_failed"}:
                subagent_id = str(payload.get("subagent_id") or "")
                child_coara_id = str(payload.get("child_coara_id") or "")
                parent_id = ""
                node = self._nodes.get(subagent_id) if subagent_id else None
                if node is not None:
                    parent_id = node.parent_id
                self._touch(subagent_id, event_at)
                if not parent_id:
                    parent_id = self._resolve_parent_tool_id(payload)
                self._finish(subagent_id, is_error=event.event_type == "subagent_failed")
                self._finish_delegate_after_child(parent_id, is_error=event.event_type == "subagent_failed")
                if child_coara_id:
                    self._coara_to_activity.pop(child_coara_id, None)
                    self._silent_coara_ids.discard(child_coara_id)
                return

            if event.event_type == "background_agent_start":
                task_id = str(payload.get("task_id") or "")
                child_coara_id = str(payload.get("child_coara_id") or "")
                agent_type = str(payload.get("subagent_type") or "").strip()
                # 静默子智能体（janitor/daily）：不建活动树行 但记 coara_id 防其工具行漏出
                if agent_type in _CLI_SILENT_SUBAGENT_TYPES:
                    if child_coara_id:
                        self._silent_coara_ids.add(child_coara_id)
                    return
                if task_id:
                    label = self._format_agent_label(
                        agent_type or "subagent",
                        str(payload.get("description") or "").strip(),
                        prefix="后台",
                    )
                    self._upsert(
                        task_id,
                        self._resolve_parent_tool_id(payload),
                        label,
                        "background",
                        child_coara_id,
                        subagent_type=agent_type,
                    )
                    if child_coara_id:
                        self._coara_to_activity[child_coara_id] = task_id
                    self._touch(task_id, event_at)
                return

            if event.event_type == "background_agent_complete":
                task_id = str(payload.get("task_id") or "")
                child_coara_id = str(payload.get("child_coara_id") or "")
                parent_id = ""
                node = self._nodes.get(task_id) if task_id else None
                if node is not None:
                    parent_id = node.parent_id
                self._touch(task_id, event_at)
                if not parent_id:
                    parent_id = self._resolve_parent_tool_id(payload)
                self._finish(task_id, is_error=bool(payload.get("has_error", False)))
                self._finish_delegate_after_child(parent_id, is_error=bool(payload.get("has_error", False)))
                if child_coara_id:
                    self._coara_to_activity.pop(child_coara_id, None)
                    self._silent_coara_ids.discard(child_coara_id)
                return

            if event.event_type == "process_spawned":
                coara_id = str(event.coara_id or "")
                if coara_id:
                    self._upsert(coara_id, "", self._format_agent_label("subprocess", event.coara_name), "process")
                    self._touch(coara_id, event_at)
                return

            if event.event_type in {"coara_created", "coara_terminated"}:
                self._finish(str(event.coara_id or ""))
                return

            if event.event_type == "tool_start":
                tool_call_id = str(payload.get("tool_call_id") or activity.activity_id)
                tool_name = str(payload.get("tool_name") or "")
                # coara_id 的真实来源是事件属性：RemoteEventBus 把帧里平铺的 coara_id
                # 提到了 TraceEvent 上，payload 里没有它。只读 payload 会恒空 → 子智能体
                # 的工具行挂不上子智能体节点（depth=0），随后被 should_flush_tool_to_history
                # 丢弃：既进不了折叠块、也不落滚动区（折叠块「0 项工具」的根因）。
                # payload 优先保留给手搓事件/旧协议的显式写法。
                coara_id = str(payload.get("coara_id") or event.coara_id or "")
                if not tool_call_id:
                    return
                # 静默子智能体的工具行：不建行
                if coara_id and coara_id in self._silent_coara_ids:
                    return
                if tool_name == "delegate":
                    # wait 是内部同步点：各子智能体行已在转，裸 delegate 行冗余不显示
                    args = payload.get("arguments") or {}
                    if str(args.get("action") or "") == "wait":
                        return
                # Full args; spinner status lines are clipped to terminal width later.
                label = format_tool_call_label(tool_name, payload.get("arguments"), max_len=None)
                parent_id = self._coara_to_activity.get(coara_id, "")
                if not parent_id:
                    parent_id = self._normalize_parent_id(activity.parent_activity_id)
                if parent_id and tool_name == "delegate":
                    return
                self._upsert(tool_call_id, parent_id, label, tool_name, coara_id)
                if not parent_id and tool_name == "delegate":
                    self._active_delegate_ids.add(tool_call_id)
                self._touch(tool_call_id, event_at)
                return

            if event.event_type == "tool_complete":
                tool_call_id = str(payload.get("tool_call_id") or activity.activity_id)
                is_error = bool(payload.get("is_error", False))
                if not tool_call_id:
                    return
                tool_name = str(payload.get("tool_name") or "")
                # Foreground-async / background delegate: tool_complete is only the
                # spawn ack — child keeps running. Keep the live paren until
                # subagent_complete / background_agent_complete.
                if tool_name == "delegate" and not is_error:
                    mode = str(payload.get("delegate_mode") or "").strip()
                    if mode in {"foreground_async", "background"}:
                        return
                self._touch(tool_call_id, event_at)
                self._finish(tool_call_id, is_error=is_error)
                self._active_delegate_ids.discard(tool_call_id)

    def get_status_lines(self) -> list[str]:
        return [line for line, _style in self.get_status_rows()]

    def get_status_styles(self) -> list[str]:
        """每行对应 get_status_lines 的 rich style class（"" 表示默认 thinking 色）。"""
        return [style for _line, style in self.get_status_rows()]

    def get_status_rows(self) -> list[tuple[str, str]]:
        """一次快照返回 (line, style)，避免 lines/styles 分两次扫描错位。"""
        return self._status_rows()

    def _status_rows(self) -> list[tuple[str, str]]:
        with self._lock:
            rows: list[tuple[str, str]] = []
            for node in self._root_nodes():
                self._render(node, rows, depth=0)
            if len(rows) > MAX_STATUS_TOTAL_LINES:
                hidden = len(rows) - MAX_STATUS_OVERFLOW_HINT
                rows = rows[:MAX_STATUS_OVERFLOW_HINT] + [(f"  ⎿ 还有 {hidden} 个活动...", "")]
            return rows

    def flush_finished_blocks(self, *, merge: bool = True) -> list[ToolCallBlock]:
        with self._lock:
            eligible = [block for block in self._finished_blocks if self._should_flush(block)]
            self._finished_blocks.clear()
            if not merge:
                return eligible
            # 完成态批量打印前归并相邻同类只读工具（read/glob/grep），
            # 降低调研噪声；完整记录仍在录像带，/log 可逐个展开
            from src.cli.activity_types import merge_adjacent_tool_blocks

            return merge_adjacent_tool_blocks(eligible)

    def has_active_blocks(self) -> bool:
        with self._lock:
            return any(node.active and not self._in_silent_subtree(node.node_id) for node in self._nodes.values())

    def subagent_tag_for_block(self, block: ToolCallBlock) -> str:
        with self._lock:
            return self._subagent_tag_for(block.tool_call_id)

    def delegate_owner_for_block(self, block: ToolCallBlock) -> str:
        """该完成块所属子智能体运行的容器 id（delegate 行 call_id / 后台 task_id）。

        空串＝主会话自己的工具行（照旧刷滚动区）；非空＝子智能体的过程行，
        归到发起它的那次 delegate 运行的折叠块里（CLI 端口径对齐 web 折叠区）。
        归属在 `_finish` 时就记在块上（节点树会 prune，不能事后回查）。
        """
        return str(getattr(block, "owner_id", "") or "")

    @staticmethod
    def format_history_line(block: ToolCallBlock, *, subagent_tag: str = "") -> str:
        mark = "✗" if block.is_error else "✓"
        indent = "  " * max(0, block.depth - 1)
        if subagent_tag:
            return f"{indent}{mark} [{subagent_tag}] {block.label}"
        return f"{indent}{mark} {block.label}"

    def _normalize_parent_id(self, parent_id: str) -> str:
        if not parent_id or parent_id == self._session_id:
            return ""
        return parent_id if parent_id in self._nodes else parent_id

    def _resolve_parent_tool_id(self, payload: dict) -> str:
        explicit = str(payload.get("parent_tool_call_id") or payload.get("parent_activity_id") or "")
        if explicit:
            return explicit
        if len(self._active_delegate_ids) == 1:
            return next(iter(self._active_delegate_ids))
        return ""

    def _upsert(
        self,
        node_id: str,
        parent_id: str,
        label: str,
        kind: str,
        coara_id: str = "",
        *,
        subagent_type: str = "",
        status: str = "",
    ) -> None:
        if parent_id and parent_id not in self._nodes:
            parent_id = ""
        if node_id in self._nodes:
            node = self._nodes[node_id]
            node.parent_id = parent_id
            node.label = label
            node.kind = kind
            node.coara_id = coara_id or node.coara_id
            if subagent_type:
                node.subagent_type = subagent_type
            node.status = status  # 空串清除 pending（run 点火变亮色）
            node.active = True
            node.is_error = False
            return
        self._nodes[node_id] = _LiveNode(
            node_id=node_id,
            parent_id=parent_id,
            label=label,
            kind=kind,
            coara_id=coara_id,
            subagent_type=subagent_type,
            status=status,
        )
        if kind in {"delegate", "subagent", "background", "process"}:
            self._node_started_at[node_id] = time.monotonic()

    def _finish(self, node_id: str, *, is_error: bool = False) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.active = False
        node.is_error = is_error
        depth = self._depth_for(node_id)
        block = ToolCallBlock(
            node.node_id,
            node.kind,
            node.label,
            is_error=is_error,
            finished=True,
            depth=depth,
            advisor_suppressed=self._in_silent_subtree(node_id),
            # 所属 delegate 容器在 prune 前先算好（节点回收后查不到祖先）：
            # 折叠块靠它把子智能体的过程行归到发起它的那次运行。
            owner_id=self._delegate_ancestor_id(node_id),
        )
        self._finished_blocks.append(block)
        if node.kind in {"delegate", "subagent", "background", "process"}:
            self._node_started_at.pop(node_id, None)
            self._delegate_llm_usage.pop(node_id, None)
        if node.kind == "delegate":
            self._active_delegate_ids.discard(node_id)
        self._prune_inactive()

    def _finish_delegate_after_child(self, parent_id: str, *, is_error: bool) -> None:
        """Close the parent delegate row once its spawned child ends."""
        if not parent_id:
            return
        parent = self._nodes.get(parent_id)
        if parent is None or not parent.active:
            return
        if parent.kind not in {"delegate", "background"}:
            return
        self._finish(parent_id, is_error=is_error)

    def _root_nodes(self) -> list[_LiveNode]:
        return [
            node
            for node in sorted(self._nodes.values(), key=lambda item: item.order)
            if not node.parent_id and self._is_visible(node.node_id) and not self._is_silent_node(node)
        ]

    def _render(self, node: _LiveNode, rows: list[tuple[str, str]], depth: int) -> None:
        if node.kind == "overflow":
            rows.append((f"{'  ' * (depth + 1)}⎿ {node.label}", ""))
            return
        if not self._is_visible(node.node_id):
            return
        children = self._visible_children(node)
        if node.active and node.kind in {"delegate", "background"}:
            paren = self._delegate_live_paren(node.node_id)
            suffix = f"  {paren}" if paren else ""
        elif node.active:
            suffix = ""
        else:
            suffix = "..."
        if node.kind == "flow":
            # flow 容器行：自身不转圈，有运行中子节点亮色、全停车/收尾灰色
            has_running = any(child.active and child.status != "pending" for child in children)
            style = "" if has_running else "class:prompt.subagent.pending"
            mark = "◌" if has_running else "·"
            rows.append((f"{'  ' * depth}{mark} {node.label}{suffix}", style))
            for child in children:
                self._render(child, rows, depth + 1)
            return
        if node.status == "pending":
            # 停车节点：灰色静态符号，spinner 不转；run 点火后同一行变亮色
            style = "class:prompt.subagent.pending"
            mark = "·"
        else:
            style = ""
            mark = "◌" if depth == 0 else "⎿"
        if depth == 0:
            rows.append((f"{mark} {node.label}{suffix}", style))
        else:
            rows.append((f"{'  ' * depth}{mark} {node.label}{suffix}", style))
        # Stats live in the paren on the delegate line — no extra heartbeat row.
        if not children and node.kind in {"workflow", "process"} and node.active:
            rows.append((f"{'  ' * (depth + 1)}⎿ 正在执行 {node.label}", ""))
            return
        for child in children:
            self._render(child, rows, depth + 1)

    def _visible_children(self, node: _LiveNode) -> list[_LiveNode]:
        children = [c for c in self._children(node.node_id) if self._is_visible(c.node_id)]
        if node.kind != "delegate":
            return children
        visible: list[_LiveNode] = []
        for child in children:
            if child.kind in {"subagent", "background"}:
                if self._is_silent_node(child):
                    continue
                visible.extend(c for c in self._children(child.node_id) if self._is_visible(c.node_id))
            else:
                visible.append(child)
        if len(visible) > MAX_ACTIVE_TOOLS_PER_DELEGATE:
            overflow = len(visible) - MAX_ACTIVE_TOOLS_PER_DELEGATE + 1
            visible = visible[: MAX_ACTIVE_TOOLS_PER_DELEGATE - 1]
            visible.append(
                _LiveNode(
                    node_id=f"{node.node_id}:overflow",
                    parent_id=node.node_id,
                    label=f"还有 {overflow} 个工具…",
                    kind="overflow",
                )
            )
        return visible

    def _children(self, parent_id: str) -> list[_LiveNode]:
        return sorted(
            [node for node in self._nodes.values() if node.parent_id == parent_id],
            key=lambda node: node.order,
        )

    def _is_visible(self, node_id: str) -> bool:
        node = self._nodes.get(node_id)
        if node is None:
            return False
        return node.active or any(self._is_visible(child.node_id) for child in self._children(node_id))

    def _depth_for(self, node_id: str) -> int:
        depth = 0
        node = self._nodes.get(node_id)
        while node is not None and node.parent_id:
            depth += 1
            node = self._nodes.get(node.parent_id)
        return depth

    def _should_flush(self, block: ToolCallBlock) -> bool:
        if block.advisor_suppressed:
            return False
        return should_flush_tool_to_history(block)

    @staticmethod
    def _is_silent_node(node: _LiveNode | None) -> bool:
        return node is not None and node.subagent_type in _CLI_SILENT_SUBAGENT_TYPES

    def _in_silent_subtree(self, node_id: str) -> bool:
        node = self._nodes.get(node_id)
        while node is not None:
            if self._is_silent_node(node):
                return True
            if not node.parent_id:
                break
            node = self._nodes.get(node.parent_id)
        return False

    def _delegate_ancestor_id(self, node_id: str) -> str:
        node = self._nodes.get(node_id)
        while node is not None:
            if node.kind in {"delegate", "background"}:
                return node.node_id
            node = self._nodes.get(node.parent_id)
        return ""

    def _subagent_tag_for(self, node_id: str) -> str:
        return self._subagent_tag_for_coara(self._nodes.get(node_id, _LiveNode("", "", "", "")).coara_id)

    def _subagent_tag_for_coara(self, coara_id: str) -> str:
        if not coara_id:
            return ""
        activity_id = self._coara_to_activity.get(coara_id, "")
        node = self._nodes.get(activity_id) if activity_id else None
        if node is None:
            return ""
        label = node.label.strip()
        if "(" in label:
            return label.split("(", 1)[0].strip()
        return label.split()[0] if label else node.kind

    def _delegate_live_paren(self, delegate_id: str) -> str:
        """``(12s · 2.8k tok · cache 80%)`` — runtime + 最后一轮 context / cache。"""
        started = self._node_started_at.get(delegate_id, time.monotonic())
        elapsed = self._format_elapsed(time.monotonic() - started)
        parts = [elapsed]
        usage_label = self._format_delegate_usage(delegate_id)
        if usage_label:
            parts.append(usage_label)
        return f"({' · '.join(parts)})"

    def _record_llm_turn(self, coara_id: str, payload: dict) -> None:
        """Roll a subagent LLM turn's latest usage into its delegate (context 口径)。"""
        coara_id = str(coara_id or "").strip()
        if not coara_id:
            return
        usage_raw = payload.get("llm_output")
        usage: dict = {}
        if isinstance(usage_raw, dict):
            nested = usage_raw.get("usage")
            if isinstance(nested, dict):
                usage = nested
        if not usage:
            direct = payload.get("usage")
            if isinstance(direct, dict):
                usage = direct
        prompt = total_prompt_tokens(usage)
        output = int(usage.get("output_tokens") or 0)
        if prompt <= 0 and output <= 0:
            return

        activity_id = self._coara_to_activity.get(coara_id, "")
        bucket_id = self._delegate_ancestor_id(activity_id) if activity_id else ""
        if not bucket_id:
            # Foreground LLM turns are shown on the toolbar; only attribute
            # usage that belongs to an active delegate/subagent subtree.
            if activity_id and self._nodes.get(activity_id) is not None:
                bucket_id = activity_id
            else:
                return

        accum = self._delegate_llm_usage.get(bucket_id)
        if accum is None:
            accum = _DelegateLlmUsage()
            self._delegate_llm_usage[bucket_id] = accum
        # context 口径：覆盖为最后一轮，不累加
        accum.last_prompt_tokens = prompt
        accum.last_cache_read_tokens = cache_read_tokens(usage)
        accum.llm_turns += 1

    def _format_delegate_usage(self, delegate_id: str) -> str:
        accum = self._delegate_llm_usage.get(delegate_id)
        if accum is None or accum.last_prompt_tokens <= 0:
            return ""
        parts = [f"{self._format_token_count(accum.last_prompt_tokens)} tok"]
        if accum.cache_hit_ratio is not None:
            parts.append(f"cache {accum.cache_hit_ratio * 100:.0f}%")
        return " · ".join(parts)

    @staticmethod
    def _format_token_count(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    @staticmethod
    def _format_agent_label(subagent_type: str, description: str, *, prefix: str = "") -> str:
        head = f"{prefix} {subagent_type}".strip()
        return f"{head}({description})" if description else head

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        return format_elapsed(seconds)

    def _touch(self, node_id: str, event_at: float) -> None:
        """刷新节点最近事件时间，并把心跳沿父链传播到所有祖先。

        事件到达即「该节点（或其子树）活着」：委派行/根的判活与括号内的
        elapsed/token 以此为准。原 prune 里单向一遍上卷依赖插入序、孙辈事件
        到不了 delegate 根；改为事件到达时即时传播，判活信号实时一致。
        """
        node = self._nodes.get(node_id)
        while node is not None:
            if node.last_event < event_at:
                node.last_event = event_at
            node = self._nodes.get(node.parent_id) if node.parent_id else None

    def _touch_coara(self, coara_id: str, event_at: float) -> None:
        """coara（子智能体）维度心跳：llm_turn_complete / thinking_progress。

        子智能体每轮 LLM 完成或流式思考推进 = 确定活着：即使期间没有工具事件
        （长 LLM 调用），也刷新其行与 delegate 祖先，避免被静默兜底误杀。
        收口后 _coara_to_activity 已清空，天然不 touch 已结束的残留。
        """
        if not coara_id:
            return
        activity_id = self._coara_to_activity.get(coara_id)
        if activity_id:
            self._touch(activity_id, event_at)

    def _in_active_container_subtree(self, node_id: str) -> bool:
        """节点自身或祖先中存在 active 的委派容器（delegate/background/flow）。

        容器 active = 内核仍在等待其收口（子智能体协程 / 后台任务 / 流程在跑），
        此时整棵子树即使长时间无事件也不判死——那是「在跑但暂时静默」。
        容器收口（inactive / 移除）后保护消失，残留靠 _STALE_NODE_TIMEOUT_S 回收。
        """
        node = self._nodes.get(node_id)
        while node is not None:
            if node.kind in {"delegate", "background", "flow"} and node.active:
                return True
            node = self._nodes.get(node.parent_id) if node.parent_id else None
        return False

    def _prune_inactive(self) -> None:
        """回收不再活跃的节点（对账兜底，极端情况才触发）。

        - 活跃委派容器（delegate/background/flow active）及其子树**不因静默判死**：
          它们正被内核运行，收口靠终态帧（subagent_complete / background_agent_complete /
          tool_complete / flow_finished）与回合收尾（clear_root_status / resync），
          判活靠事件心跳——tool_* / llm_turn_complete / thinking_progress 已在 ingest
          用 _touch 刷新。长时间无事件只说明「暂时静默」（LLM 单次调用、长工具），
          不代表死亡，误杀会让运行中的活动树整行消失。
        - 无活跃委派容器的孤立残留（终态帧丢失、断流遗留的僵尸行）才在超长静默
          后被回收，防止子树僵死。
        - 父节点已 inactive 且无活跃后代 → 整枝移除。
        """
        changed = True
        while changed:
            changed = False
            for node_id, node in list(self._nodes.items()):
                if node.active:
                    if self._in_active_container_subtree(node_id):
                        continue
                    if time.monotonic() - node.last_event > _STALE_NODE_TIMEOUT_S:
                        node.active = False
                        changed = True
                    continue
                if any(child.parent_id == node_id for child in self._nodes.values()):
                    continue
                self._nodes.pop(node_id, None)
                self._node_started_at.pop(node_id, None)
                self._delegate_llm_usage.pop(node_id, None)
                for coara_id, activity_id in list(self._coara_to_activity.items()):
                    if activity_id == node_id:
                        self._coara_to_activity.pop(coara_id, None)
                changed = True
