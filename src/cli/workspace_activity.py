"""多工作空间活跃状态登记（CLI 动态区「工作空间状态区」的数据源）。

设计（2026-08-21 拍板的多工作空间 CLI 显示框架）：
- 每个有活跃回合的工作空间占一行：转圈帧 + 空间色标签 + 动作摘要 + 计时
- 动作摘要只在这一行滚动更新，不进滚动区
- 排序：后台空间按最近事件时间倒序（最活跃在上），前台空间行恒在最下
- 空间消失：回合结束（completed/turn_failed/turn_interrupted 主会话事件）
  或 reconcile 兜底（has_active_turn 变 False，防事件丢失后行不消失）

空间色：按 workspace key（resolve 后的目录路径）md5 哈希取色，跨进程稳定
（内置 hash() 对 str 按进程随机化，不可用）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.cli.theme import get_workspace_palette
from src.coara.builtin_agents import CLI_SILENT_SUBAGENT_TYPES

# reconcile 与事件流之间的竞争缓冲：回合刚结束但 completed 事件未到/已到时
# 行的存活宽限，避免 reconcile 与事件互相打架造成行闪烁
_STALE_GRACE_S = 2.0


def workspace_color(key: str) -> tuple[str, str]:
    """按 workspace key 稳定取色，返回 (亮色, 暗色)。色板统一由 cli.theme 提供。"""
    palette = get_workspace_palette()
    digest = hashlib.md5(str(key).encode("utf-8")).hexdigest()
    return palette[int(digest, 16) % len(palette)]


def _resolve_key(workspace_dir: str) -> str:
    raw = str(workspace_dir or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return raw


@dataclass(slots=True)
class WorkspaceActivityRow:
    """一个工作空间的活跃状态行（spinner 渲染快照）。"""

    key: str
    name: str
    summary: str = "运转中"
    started_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    subagent_count: int = 0
    is_foreground: bool = False


class WorkspaceActivityRegistry:
    """按工作空间归集各空间活跃状态（事件驱动 + reconcile 兜底校正）。"""

    def __init__(self) -> None:
        self._root: Any | None = None
        self._rows: dict[str, WorkspaceActivityRow] = {}
        self._fg_key: str = ""
        self._session_to_key: dict[str, str] = {}
        # 系统静默子智能体（janitor/daily）的 coara_id：其活动不建行
        self._silent_coara_ids: set[str] = set()

    def bind_root(self, root: Any) -> None:
        self._root = root

    def has_rows(self) -> bool:
        return bool(self._rows)

    # ── 事件入口 ──

    def ingest(self, event: Any) -> None:
        event_type = getattr(event, "event_type", "")
        payload = getattr(event, "payload", None) or {}
        child_id = str(payload.get("child_coara_id") or "")

        if event_type in ("subagent_start", "background_agent_start"):
            subagent_type = str(payload.get("subagent_type") or "").strip()
            if subagent_type in CLI_SILENT_SUBAGENT_TYPES:
                if child_id:
                    self._silent_coara_ids.add(child_id)
                return
        elif event_type in ("subagent_complete", "subagent_failed", "background_agent_complete"):
            if child_id and child_id in self._silent_coara_ids:
                self._silent_coara_ids.discard(child_id)
                return

        # 静默子智能体本体发出的活动（tool_start 等）：不建行
        coara_id = str(payload.get("coara_id") or getattr(event, "coara_id", "") or "")
        if coara_id in self._silent_coara_ids:
            return

        if event_type in ("completed", "turn_failed", "turn_interrupted"):
            # 只有工作空间主会话的回合结束才收行（子智能体回合也发 completed，
            # 不能误收）；主会话身份靠 root._sessions 确认，确认不了就留给
            # reconcile 兜底
            key = self._key_for_event(event)
            if key and self._is_main_session_event(key, payload):
                self._rows.pop(key, None)
            return

        key = self._key_for_event(event)
        if not key:
            return
        now = time.monotonic()
        row = self._rows.get(key)
        if row is None:
            row = WorkspaceActivityRow(key=key, name=self._name_for_key(key))
            self._rows[key] = row
        row.last_event_at = now

        if event_type == "tool_start":
            tool_name = str(payload.get("tool_name") or "").strip()
            row.summary = f"正在运行 {tool_name}" if tool_name else "正在运行工具"
        elif event_type == "llm_request_start":
            row.summary = "等待模型"
        elif event_type in ("subagent_start", "background_agent_start"):
            row.subagent_count += 1
            row.summary = f"子智能体 ×{row.subagent_count}"
        elif event_type in ("subagent_complete", "subagent_failed", "background_agent_complete"):
            row.subagent_count = max(0, row.subagent_count - 1)
            row.summary = f"子智能体 ×{row.subagent_count}" if row.subagent_count else "运转中"
        elif event_type == "tool_complete":
            if row.subagent_count:
                row.summary = f"子智能体 ×{row.subagent_count}"
            else:
                row.summary = "运转中"

    # ── 兜底校正 ──

    def reconcile(self) -> None:
        """从 root._sessions + has_active_turn() 校正：防事件丢失后行不消失/不出现。"""
        root = self._root
        if root is None:
            return
        sessions = getattr(root, "_sessions", None) or {}
        fg_session_id = getattr(root, "_foreground_session_id", None)
        active_keys: set[str] = set()
        self._session_to_key.clear()
        for workspace_id, session in sessions.items():
            coara = getattr(session, "coara", None)
            if coara is None:
                continue
            key = _resolve_key(str(getattr(session, "workspace_dir", "") or ""))
            if not key:
                continue
            session_id = str(getattr(session, "session_id", "") or "")
            if session_id:
                self._session_to_key[session_id] = key
            if workspace_id == fg_session_id:
                self._fg_key = key
            try:
                active = bool(coara.has_active_turn())
            except Exception:
                active = False
            if not active:
                continue
            active_keys.add(key)
            row = self._rows.get(key)
            if row is None:
                row = WorkspaceActivityRow(key=key, name=str(getattr(session, "workspace_name", "") or ""))
                self._rows[key] = row
            if not row.name:
                row.name = str(getattr(session, "workspace_name", "") or "") or self._name_for_key(key)

        now = time.monotonic()
        for key, row in list(self._rows.items()):
            if key in active_keys:
                continue
            if now - row.last_event_at > _STALE_GRACE_S:
                self._rows.pop(key, None)

    # ── 渲染快照 ──

    def render_rows(self) -> list[WorkspaceActivityRow]:
        """reconcile 后输出排序快照：后台按最近事件倒序，前台行恒在最下。"""
        self.reconcile()
        rows = list(self._rows.values())
        for row in rows:
            row.is_foreground = bool(self._fg_key) and row.key == self._fg_key
        background = sorted((r for r in rows if not r.is_foreground), key=lambda r: r.last_event_at, reverse=True)
        foreground = [r for r in rows if r.is_foreground]
        return background + foreground

    # ── 内部 ──

    def _key_for_event(self, event: Any) -> str:
        payload = getattr(event, "payload", None) or {}
        key = _resolve_key(str(payload.get("workspace_dir") or ""))
        if key:
            return key
        session_id = str(payload.get("session_id") or "")
        return self._session_to_key.get(session_id, "")

    def _is_main_session_event(self, key: str, payload: dict) -> bool:
        """确认事件来自该工作空间的主会话（而非其子智能体回合）。"""
        root = self._root
        sessions = getattr(root, "_sessions", None) if root is not None else None
        session_id = str(payload.get("session_id") or "")
        for session in (sessions or {}).values():
            coara = getattr(session, "coara", None)
            if coara is None:
                continue
            if _resolve_key(str(getattr(session, "workspace_dir", "") or "")) != key:
                continue
            main_session = str(getattr(session, "session_id", "") or "")
            # 找到了该空间的会话：只认主会话 session_id
            return bool(session_id) and session_id == main_session
        # 会话表里没有这个空间（事件先于此空间登记）：无法证伪，按主会话处理
        return True

    def _name_for_key(self, key: str) -> str:
        root = self._root
        if root is not None:
            try:
                from src.coara.turn_detach import workspace_display_name

                name = workspace_display_name(root, key)
                if name:
                    return name
            except Exception:
                pass
        try:
            return Path(key).name
        except Exception:
            return key
