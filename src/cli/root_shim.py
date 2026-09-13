"""RootShim: root 替身——原 CLI 界面骨架消费的 root 接口子集的远端实现。

内核化「原 CLI 界面召回」的客户端数据层。界面骨架（chat_runner /
display_controller / session / spinner）照旧访问 ``root.event_bus`` /
``root.foreground_coara`` / ``root.workspace_manager`` 等；RootShim 把状态
换成「握手快照 + 事件驱动更新」的本地镜像，把动作换成经 transport
（/ws/attach）的帧透传。

注意：这里的 ``_sessions`` / ``_foreground_session_id`` / ``_active_turn_source``
等下划线名是**对界面骨架既有消费点的契约**（它们本来就以这些名字读
root），不泄漏内核内部结构——镜像值全部来自快照与公开事件，turn 状态
（active/source/aborted）由 completed/turn_failed/turn_interrupted/
llm_request_start/user_message 等事件驱动维护。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from src.cli.attach_transport import AttachTransport
from src.cli.remote_event_bus import RemoteEventBus
from src.coara.commands.types import CommandResult
from src.context.window import LlmUsageSnapshot
from src.core.events import TraceEvent
from src.core.logger import logger
from src.core.process import schedule_threadsafe
from src.core.types import ContinuationInput
from src.llm.usage import total_prompt_tokens

# 主会话回合生命周期事件：结束 active turn 的镜像。
_TURN_END_TOPICS = ("completed", "turn_failed", "turn_interrupted", "event_turn_complete")
# 推断「回合开始/进行中」的事件。
_TURN_ACTIVE_TOPICS = (
    "user_message",
    "llm_request_start",
    "tool_start",
    "thinking_progress",
    "event_turn_start",
    "conversation_message",
    "chat_chunk",
)

# 子智能体正文帧：内核 kind 原样带来的独立帧型（服务端 _attach_output_frame）。
# CLI 的前台滚动区只认主会话正文——子智能体的旁白、中间正文、最终报告归活动树
# 那一层，其结果由 ``[<类型>子智能体]`` 回显行呈现（continuation_input_injected）。
_SUBAGENT_FRAME_TYPES = frozenset({"subagent_chunk", "subagent_result"})


def _subagent_body_frame(frame: dict[str, Any]) -> bool:
    """该帧是不是子智能体正文（不该进前台滚动区）。

    判据：帧型（新内核保留 kind）或 ``agent_kind`` 字段（纯新增，旧内核不发）。
    两者都没有的旧帧判否——按主会话正文处理，旧内核行为不变。只对正文类帧
    （chunk/chat_chunk/subagent_*）成立：diff 帧即便来自子智能体也照旧渲染
    （工具行层显示不变）。
    """
    frame_type = str(frame.get("type") or "")
    if frame_type in _SUBAGENT_FRAME_TYPES:
        return True
    if frame_type not in ("chunk", "chat_chunk"):
        return False
    return str(frame.get("agent_kind") or "").strip().lower() == "subagent"


# 入站帧取证（仅 COARA_CLI_FRAME_TRACE=1 时启用）：把每帧的类型/归属/文本长度追加写
# %TEMP%/coara-cli-frames.log。用来定案「同一段正文上屏两遍」到底是链路重复投递还是
# 端上渲染残留——跑一次复现，比对 chunk 帧的条数与 seq 即能分辨。
_FRAME_TRACE_ENV = "COARA_CLI_FRAME_TRACE"


def _frame_trace_on() -> bool:
    import os

    return os.environ.get(_FRAME_TRACE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _trace_inbound_frame(frame: dict[str, Any]) -> None:
    import json
    import tempfile
    import time
    from pathlib import Path

    try:
        text = str(frame.get("text") or frame.get("chunk") or "")
        record = {
            "ts": round(time.time(), 3),
            "type": str(frame.get("type") or ""),
            "seq": frame.get("seq"),
            "turn": str(frame.get("turn_id") or "")[:8],
            "agent_kind": str(frame.get("agent_kind") or ""),
            "replayed": bool(frame.get("replayed")),
            "n": len(text),
            "head": text[:40],
        }
        path = Path(tempfile.gettempdir()) / "coara-cli-frames.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 取证失败绝不能影响主流程
        return


def _normalize_snapshot(frame: dict[str, Any]) -> dict[str, Any]:
    """服务端 attached 扁平快照 → RootShim 嵌套结构。

    服务端 _build_attach_attached_frame 的字段（全部平铺在 attached 帧上）：
    workspace/workspace_id/session/provider/model（旧短名）+ session_id/
    workspace_dir/coara_id/agent_name/provider_name/model_name/is_plan_mode/
    tools_count/skills_count/active_name/foreground_session_id/workspaces/
    usage/context_window/turn_source。归一为 identity/foreground/
    workspace_manager/sessions 分组供各镜像消费。
    """
    usage_raw = frame.get("usage") if isinstance(frame.get("usage"), dict) else {}
    workspace_id = str(frame.get("workspace_id") or "")
    workspace_dir = str(frame.get("workspace_dir") or "")
    workspace_name = str(frame.get("workspace") or frame.get("active_name") or "")
    session_id = str(frame.get("session_id") or frame.get("session") or "")
    return {
        "identity": {
            "name": str(frame.get("agent_name") or ""),
            "coara_id": str(frame.get("coara_id") or ""),
            "workspace_dir": workspace_dir,
            # 就绪横幅的 Tools/Skills 计数：attached 快照平铺字段原样保留
            # （fg._tool_manager/_skills 是内核内部结构，客户端镜像计数即可）。
            "tools_count": frame.get("tools_count") or 0,
            "skills_count": frame.get("skills_count") or 0,
        },
        "foreground": {
            "identity": {
                "name": str(frame.get("agent_name") or ""),
                "coara_id": str(frame.get("coara_id") or ""),
                "workspace_dir": workspace_dir,
            },
            "session_id": session_id,
            "workspace_dir": workspace_dir,
            "provider_name": str(frame.get("provider_name") or frame.get("provider") or ""),
            "model_name": str(frame.get("model_name") or frame.get("model") or ""),
            "is_plan_mode": bool(frame.get("is_plan_mode", False)),
            "active_turn_source": str(frame.get("turn_source") or ""),
            "context_window": frame.get("context_window") or 0,
            "provider_has_key": frame.get("provider_has_key"),
            "usage_snapshot": {
                "usage": usage_raw,
                "cache_hit_ratio": usage_raw.get("cache_hit_ratio"),
                # usage 帧带非零 prompt tokens 即已上报过输入——镜像
                # has_reported_input，否则状态栏上下文 token 恒被门控为 0。
                "has_reported_input": total_prompt_tokens(usage_raw) > 0,
            },
        },
        "workspace_manager": {
            "active_name": str(frame.get("active_name") or ""),
            "active_workspace_id": workspace_id,
            "workspaces": frame.get("workspaces") if isinstance(frame.get("workspaces"), list) else [],
        },
        "sessions": {
            workspace_id: {
                "workspace_dir": workspace_dir,
                "workspace_name": workspace_name,
                "session_id": session_id,
                "has_active_turn": bool(frame.get("turn_source")),
            }
        }
        if workspace_id
        else {},
        "foreground_session_id": str(frame.get("foreground_session_id") or workspace_id),
        "service_desks": frame.get("service_desks") if isinstance(frame.get("service_desks"), list) else [],
    }


class _RemoteIdentity:
    """identity 替身（root 与 foreground 共用同型）：name / coara_id / workspace_dir。"""

    def __init__(self, *, name: str = "", coara_id: str = "", workspace_dir: str = "") -> None:
        self.name = name
        self.coara_id = coara_id
        self.workspace_dir = workspace_dir
        # 界面读 fg.identity.is_owner_context（工具可见性门控）：替身恒 True（主会话视角）。
        self.is_owner_context = True
        # 就绪横幅计数（attached 快照驱动）：fg._tool_manager/_skills 是内核内部
        # 结构，客户端只镜像计数。
        self.tools_count: int = 0
        self.skills_count: int = 0

    def apply(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        if data.get("name") is not None:
            self.name = str(data["name"])
        if data.get("coara_id") is not None:
            self.coara_id = str(data["coara_id"])
        if data.get("workspace_dir") is not None:
            self.workspace_dir = str(data["workspace_dir"])
        if data.get("tools_count") is not None:
            with contextlib.suppress(TypeError, ValueError):
                self.tools_count = int(data["tools_count"])
        if data.get("skills_count") is not None:
            with contextlib.suppress(TypeError, ValueError):
                self.skills_count = int(data["skills_count"])


def _extract_turn_usage(payload: dict[str, Any]) -> tuple[dict[str, int] | None, bool]:
    """Parse provider usage from ``llm_turn_complete`` payload (kernel shape)."""
    llm_output = payload.get("llm_output") or {}
    usage = llm_output.get("usage") if isinstance(llm_output, dict) else None
    if not isinstance(usage, dict):
        usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, False
    partial = False
    if isinstance(llm_output, dict):
        partial = str(llm_output.get("finish_reason") or "").strip().lower() == "partial"
    return {str(k): int(v) for k, v in usage.items() if isinstance(v, (int, float))}, partial


def _apply_usage_snapshot(snap: LlmUsageSnapshot, data: dict[str, Any]) -> None:
    """Restore attach/handshake or persisted usage onto a client-side mirror."""
    if not isinstance(data, dict):
        return
    if isinstance(data.get("cumulative_prompt_tokens"), (int, float)):
        snap.restore(data)
        return
    usage_raw = data.get("usage")
    if not isinstance(usage_raw, dict):
        snap.clear()
        return
    usage = dict(usage_raw)
    ratio = usage.pop("cache_hit_ratio", None)
    if ratio is None:
        ratio = data.get("cache_hit_ratio")
    snap.clear()
    if total_prompt_tokens(usage) <= 0:
        return
    snap.record_turn(usage=usage, history_len=0, system_len=0, tool_count=0)
    if ratio is not None:
        with contextlib.suppress(TypeError, ValueError):
            prompt = snap.cumulative_prompt_tokens
            if prompt > 0:
                snap.cumulative_cache_read_tokens = int(float(ratio) * prompt)


class _ProviderShim:
    """provider 替身：界面只调 get_context_window(model)。"""

    def __init__(self, foreground: ForegroundCoaraShim) -> None:
        self._fg = foreground

    def get_context_window(self, model: str | None = None) -> int:
        return self._fg.get_context_window(model)


class WorkspaceSessionShim:
    """_sessions 表项替身：workspace_activity.reconcile 消费 .coara/.workspace_dir/
    .workspace_name/.session_id。"""

    def __init__(self, *, workspace_id: str, workspace_dir: str = "", workspace_name: str = "") -> None:
        self.workspace_id = workspace_id
        self.workspace_dir = workspace_dir
        self.workspace_name = workspace_name
        self.session_id: str = ""
        self._active_turn = False
        self.coara = self  # coara.has_active_turn() 代理到本项

    def has_active_turn(self) -> bool:
        return self._active_turn

    def persist_session_to_disk(self) -> None:
        """会话落盘是内核职责——客户端替身 no-op（关窗清理会调到它）。"""
        return None

    def apply(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        if data.get("workspace_dir") is not None:
            self.workspace_dir = str(data["workspace_dir"])
        if data.get("workspace_name") is not None:
            self.workspace_name = str(data["workspace_name"])
        if data.get("session_id") is not None:
            self.session_id = str(data["session_id"])
        if data.get("has_active_turn") is not None:
            self._active_turn = bool(data["has_active_turn"])


class _WorkspaceEntryShim:
    """WorkspaceEntry 替身（slash_pickers / workspace catalog 消费点）：
    id / name / path / summary / status。"""

    __slots__ = ("id", "name", "path", "summary", "status")

    def __init__(self, *, id: str, name: str, path: str, summary: str = "") -> None:  # noqa: A002
        self.id = id
        self.name = name
        self.path = path
        self.summary = summary
        self.status = "active"


class WorkspaceManagerShim:
    """workspace_manager 替身：active_name / cwd_display_suffix / name_for_path /
    list_workspaces。空间名↔路径映射来自快照，workspace_switched 事件驱动更新。"""

    def __init__(self, shim: RootShim) -> None:
        self._shim = shim
        self._active_name: str = ""
        self._active_workspace_id: str = ""
        self._workspaces: list[dict[str, str]] = []  # [{id, name, path}]
        self._cwd_display_suffix: str = ""

    # --- 快照 ---

    def apply_snapshot(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        if data.get("active_name") is not None:
            self._active_name = str(data["active_name"])
        if data.get("active_workspace_id") is not None:
            self._active_workspace_id = str(data["active_workspace_id"])
        workspaces = data.get("workspaces")
        if isinstance(workspaces, list):
            self._workspaces = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "path": str(item.get("path") or ""),
                }
                for item in workspaces
                if isinstance(item, dict)
            ]
        if data.get("cwd_display_suffix") is not None:
            self._cwd_display_suffix = str(data["cwd_display_suffix"])

    # --- 界面消费的接口 ---

    @property
    def active_name(self) -> str | None:
        return self._active_name or None

    def cwd_display_suffix(self) -> str:
        return self._cwd_display_suffix

    def name_for_path(self, workspace_dir: str) -> str | None:
        """按路径解析注册空间名（对齐 WorkspaceManager.name_for_path）。"""
        raw = str(workspace_dir or "").strip()
        if raw:
            best_name: str | None = None
            best_len = -1
            for entry in self._workspaces:
                path = entry["path"]
                if not path:
                    continue
                # 规范化分隔符后做前缀匹配（注册根或其子路径），取最长匹配。
                norm = path.replace("\\", "/").rstrip("/").lower()
                cand = raw.replace("\\", "/").rstrip("/").lower()
                if (cand == norm or cand.startswith(norm + "/")) and len(norm) > best_len:
                    best_len = len(norm)
                    best_name = entry["name"]
            if best_name is not None:
                return best_name
        # 回退：前台 workspace_dir 的活跃空间（attached 快照的 workspaces 列表
        # 可能滞后于 ensure_workspace 登记）。
        with contextlib.suppress(Exception):
            from pathlib import Path

            if self._active_name and Path(raw).resolve() == Path(self._shim.foreground_coara.workspace_dir).resolve():
                return self._active_name
        return None

    def match_path_to_workspace_id(self, workspace_dir: str) -> str | None:
        """对齐 WorkspaceManager.match_path_to_workspace_id（workspace catalog
        消费点）：路径 → 注册空间 id。"""
        raw = str(workspace_dir or "").strip()
        if raw:
            best_id: str | None = None
            best_len = -1
            for entry in self._workspaces:
                path = entry["path"]
                if not path:
                    continue
                norm = path.replace("\\", "/").rstrip("/").lower()
                cand = raw.replace("\\", "/").rstrip("/").lower()
                if (cand == norm or cand.startswith(norm + "/")) and len(norm) > best_len:
                    best_len = len(norm)
                    best_id = entry["id"]
            if best_id is not None:
                return best_id
        # 回退：前台 workspace_dir 的活跃空间 id（同 name_for_path 的理由）。
        with contextlib.suppress(Exception):
            from pathlib import Path

            if (
                self._active_workspace_id
                and Path(raw).resolve() == Path(self._shim.foreground_coara.workspace_dir).resolve()
            ):
                return self._active_workspace_id
        return None

    @property
    def active_path(self) -> str:
        """对齐 WorkspaceManager.active_path（catalog 兜底读取）：前台空间路径。"""
        return str(self._shim.foreground_coara.workspace_dir or "")

    @property
    def active_id(self) -> str | None:
        """对齐 WorkspaceManager.active_id（catalog 兜底读取）。"""
        return self._active_workspace_id or None

    def list_workspaces(self) -> list[Any]:
        """注册空间列表（WorkspaceEntry 形状：id/name/path/summary/status）。"""
        return [
            _WorkspaceEntryShim(id=entry["id"], name=entry["name"], path=entry["path"]) for entry in self._workspaces
        ]

    # --- 事件驱动 ---

    def _on_workspace_switched(self, event: TraceEvent) -> None:
        payload = event.payload or {}
        name = str(payload.get("workspace_name") or payload.get("active_name") or "").strip()
        if name:
            self._active_name = name
        ws_id = str(payload.get("workspace_id") or "").strip()
        if ws_id:
            self._active_workspace_id = ws_id
        path = str(payload.get("workspace_dir") or "").strip()
        if path:
            # 若路径已在快照中，只刷新 active；否则按 id 去重后补登记。
            known = self.name_for_path(path)
            if known is None and (ws_id or name) and not any(e.get("id") == ws_id for e in self._workspaces if ws_id):
                self._workspaces.append({"id": ws_id, "name": name or "", "path": path})
            if not self._active_name and known:
                self._active_name = known


class ForegroundCoaraShim:
    """foreground_coara 替身：状态镜像（快照+事件）+ 动作透传。"""

    def __init__(self, shim: RootShim) -> None:
        self._shim = shim
        self.identity = _RemoteIdentity()
        self.session_id: str = ""
        self.workspace_dir: str = ""
        self.provider_name: str = ""
        self.model_name: str = ""
        self.is_plan_mode: bool = False
        self._active_turn = False
        self._active_turn_source: str = ""
        # 本地 attach 回合流收尾后置 True：忽略迟到的 thinking_progress 等
        # Trace 活跃事件，避免把 Thinking 再点亮（切空间后 coara_id 未刷新时
        # completed 会被误滤，迟到活跃帧会永久卡住 spinner）。
        self._suppress_trace_turn_active: bool = False
        self._continuation_inputs: list[ContinuationInput] = []
        # 已注入（drain 进模型）的跟话文本（空白压平后）。received 回执与 injected
        # 事件可能同秒并发、到达顺序无保证：若 received 后到，会把刚移除的项重新
        # 塞回排队镜像，表现为「明明已注入却一直挂在排队」。留一份哨兵供其跳过。
        self._injected_texts: set[str] = set()
        self._llm_usage_snapshot = LlmUsageSnapshot()
        self._context_window: int = 0
        # provider 是否有可用 API key（内核权威）：attached 快照驱动；状态栏
        # 「/model 添加 provider」引导以此为准（客户端本地探测 env 不可靠——
        # attach 进程没 load 内核的 .env，恒判无 key）。None = 快照未带该字段，
        # 状态栏回退本地探测（兼容旧内核）。
        self._provider_has_key: bool | None = None
        self.provider = _ProviderShim(self)
        # 本客户端发起的流式回合：本地 turn_id -> (chunk queue, end state)；
        # _turn_order 记录发起顺序，_server_turn_map 绑定服务端回显的 turn_id。
        self._turn_streams: dict[str, dict[str, Any]] = {}
        self._turn_order: list[str] = []
        self._server_turn_map: dict[str, str] = {}
        # 回放帧按 (turn_id, seq) 去重：本地流已终结的重放帧走 _replay_callback 渲染，
        # 该表记每个服务端 turn_id 已消费的最大 seq（防双显/漏帧）。
        self._replay_seq: dict[str, int] = {}

    # --- 快照 ---

    def apply_snapshot(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self.identity.apply(data.get("identity") or {})
        if data.get("session_id") is not None:
            self.session_id = str(data["session_id"])
        if data.get("workspace_dir") is not None:
            self.workspace_dir = str(data["workspace_dir"])
            self.identity.workspace_dir = self.workspace_dir
        if data.get("provider_name") is not None:
            self.provider_name = str(data["provider_name"])
        if data.get("model_name") is not None:
            self.model_name = str(data["model_name"])
        if data.get("is_plan_mode") is not None:
            self.is_plan_mode = bool(data["is_plan_mode"])
        if data.get("has_active_turn") is not None:
            self._active_turn = bool(data["has_active_turn"])
        if data.get("active_turn_source") is not None:
            self._active_turn_source = str(data["active_turn_source"] or "")
        if data.get("context_window") is not None:
            with contextlib.suppress(TypeError, ValueError):
                self._context_window = int(data["context_window"])
        if data.get("provider_has_key") is not None:
            self._provider_has_key = bool(data["provider_has_key"])
        continuations = data.get("continuation_inputs")
        if isinstance(continuations, list):
            self._continuation_inputs = [
                ContinuationInput(
                    text=str(item.get("text") or ""),
                    image_blocks=item.get("image_blocks"),
                    source=str(item.get("source") or ""),
                )
                for item in continuations
                if isinstance(item, dict)
            ]
        usage = data.get("usage_snapshot")
        if isinstance(usage, dict):
            _apply_usage_snapshot(self._llm_usage_snapshot, usage)

    # --- 状态镜像（事件驱动） ---

    def has_active_turn(self) -> bool:
        """本端是否有进行中的回合（Ctrl+C / 跟话 / spinner 的权威判定）。

        优先看本地回合流：本端发起 chat 即建流、收到服务端 turn_end 帧才
        done——与「屏幕上还在显示的回合」严格同步，不受 trace 事件乱序影响
        （completed/turn_end 事件先行清镜像、流还没收尾的竞态不再误判空闲）。
        流清空后回退镜像兜底（快照/事件重建的回合态）。
        """
        if any(not s.get("done") for s in self._turn_streams.values()):
            return True
        return self._active_turn

    def get_context_window(self, model: str | None = None) -> int:
        """对齐 provider.get_context_window(model)：镜像值，未知时 0。"""
        return self._context_window

    def _on_llm_switched(self, event: TraceEvent) -> None:
        payload = event.payload or {}
        # 只跟本 attach pin 空间：其它空间切模型不改本端状态栏
        event_wid = str(payload.get("workspace_id") or "").strip()
        local_wid = str(getattr(self._shim.workspace_manager, "_active_workspace_id", "") or "").strip()
        if event_wid and local_wid and event_wid != local_wid:
            return
        event_sid = str(payload.get("session_id") or "").strip()
        if event_sid and self.session_id and event_sid != self.session_id:
            return
        provider = str(payload.get("provider") or payload.get("provider_name") or "").strip()
        model = str(payload.get("model") or payload.get("model_name") or "").strip()
        if provider:
            self.provider_name = provider
        if model:
            self.model_name = model
        # 内核权威：事件必须带 provider_has_key。清成 None 会回退本地 env 探测，
        # attach 进程读不到内核 .env → 状态栏误成「/model 添加 provider」。
        if "provider_has_key" in payload:
            self._provider_has_key = bool(payload.get("provider_has_key"))
        with contextlib.suppress(TypeError, ValueError):
            ctx = payload.get("context_window")
            if ctx is not None:
                self._context_window = int(ctx)

    def _on_session_started(self, event: TraceEvent) -> None:
        payload = event.payload or {}
        # 归属过滤（对齐 _on_llm_switched）：他空间的 session_started 不得重置
        # 本端镜像——否则本端 session_id 基准被改，后续 usage 归属过滤全部失效。
        event_wid = str(payload.get("workspace_id") or "").strip()
        local_wid = str(getattr(self._shim.workspace_manager, "_active_workspace_id", "") or "").strip()
        if event_wid and local_wid and event_wid != local_wid:
            return
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            self.session_id = session_id
        if payload.get("is_plan_mode") is not None:
            self.is_plan_mode = bool(payload.get("is_plan_mode"))
        workspace_dir = str(payload.get("workspace_dir") or "").strip()
        if workspace_dir:
            self.workspace_dir = workspace_dir
            self.identity.workspace_dir = workspace_dir
        # /new 与 auto-renew：用量与跟话镜像归属新会话，避免状态栏 context 串旧值
        self._continuation_inputs = []
        self._llm_usage_snapshot = LlmUsageSnapshot()

    def _on_plan_mode_changed(self, event: TraceEvent) -> None:
        """计划模式开关（服务端 base.enter/exit_plan_mode 的 trace）：更新状态栏镜像。

        归属过滤：只跟本 attach pin 会话的开关事件——他空间/他会话的
        plan_mode_changed 不污染本端状态栏（服务端已按 pin 空间过滤，客户端
        再按 session 兜底，对齐 _on_usage_report 的归属判定）。
        """
        payload = event.payload or {}
        event_sid = str(payload.get("session_id") or "").strip()
        if event_sid and self.session_id and event_sid != self.session_id:
            return
        if "is_plan_mode" in payload:
            self.is_plan_mode = bool(payload.get("is_plan_mode"))

    def _on_workspace_switched(self, event: TraceEvent) -> None:
        payload = event.payload or {}
        workspace_dir = str(payload.get("workspace_dir") or "").strip()
        if workspace_dir:
            self.workspace_dir = workspace_dir
            self.identity.workspace_dir = workspace_dir
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            self.session_id = session_id
        coara_id = str(payload.get("coara_id") or "").strip()
        if coara_id:
            self.identity.coara_id = coara_id
        provider = str(payload.get("provider_name") or payload.get("provider") or "").strip()
        if provider:
            self.provider_name = provider
        model = str(payload.get("model_name") or payload.get("model") or "").strip()
        if model:
            self.model_name = model
        if "is_plan_mode" in payload:
            self.is_plan_mode = bool(payload.get("is_plan_mode"))
        # 切空间后前台回合状态由快照/后续事件重建，先按不活跃处理。
        self._active_turn = False
        self._active_turn_source = ""
        self._suppress_trace_turn_active = False
        self._continuation_inputs = []
        # 用量归属新会话：清掉旧空间累计，避免状态栏 token 串台
        self._llm_usage_snapshot = LlmUsageSnapshot()

    def _mark_turn_active(self, event: TraceEvent) -> None:
        if self._suppress_trace_turn_active:
            return
        self._active_turn = True
        payload = event.payload or {}
        source = str(payload.get("source") or payload.get("turn_source") or "").strip()
        if source:
            self._active_turn_source = source

    def _mark_turn_end(self, event: TraceEvent) -> None:
        self._active_turn = False
        self._active_turn_source = ""
        self._continuation_inputs = []
        self._injected_texts = set()

    def _on_continuation_received(self, event: TraceEvent) -> None:
        """服务端回发 continuation_input_received：去重后入队镜像。

        调用方（RootShim._apply_event）已按来源门控——只有本端（cli/cli-attached）
        的跟话才会到这里。客户端 submit_continuation_input 时已本地乐观入队一份
        （source=cli）；服务端回发（source=cli-attached）按文本去重：已有同文本项
        则更新 source 为服务端权威值，不再追加（防双份排队显示）。
        """
        payload = event.payload or {}
        text = str(payload.get("text") or payload.get("user_text") or "").strip()
        if not text:
            return
        if " ".join(text.split()) in self._injected_texts:
            # 该项已注入模型（同秒并发时本回执可能后到）：不再进排队镜像。
            return
        authoritative_source = str(payload.get("source") or "")
        for item in self._continuation_inputs:
            if item.text == text:
                item.source = authoritative_source or item.source
                if payload.get("image_blocks") and not item.image_blocks:
                    item.image_blocks = payload.get("image_blocks")
                return
        self._continuation_inputs.append(
            ContinuationInput(
                text=text,
                image_blocks=payload.get("image_blocks"),
                source=authoritative_source,
            )
        )

    def _on_continuation_injected(self, event: TraceEvent) -> None:
        # 注入即出队：按 user_texts 精确移除，payload 缺省时清空。
        payload = event.payload or {}
        texts = payload.get("user_texts")
        if not isinstance(texts, list) or not texts:
            self._continuation_inputs = []
            return
        drained = {" ".join(str(text or "").split()) for text in texts}
        self._injected_texts = getattr(self, "_injected_texts", set()) | drained
        self._continuation_inputs = [
            item for item in self._continuation_inputs if " ".join(str(item.text or "").split()) not in drained
        ]

    def _on_usage_report(self, event: TraceEvent) -> None:
        payload = event.payload or {}
        # 归属过滤：delegate/aide/janitor/daily 的 llm_turn_complete 也走 attach
        # 广播，不带过滤会把状态栏 context 整包覆盖成子智能体那轮的量（60k↔30k
        # 横跳）。事件帧本就带 session_id/coara_id，只收本会话的（对齐
        # _on_llm_switched 的归属判定）。
        event_sid = str(payload.get("session_id") or "").strip()
        if event_sid and self.session_id and event_sid != self.session_id:
            return
        usage, partial = _extract_turn_usage(payload)
        if not usage:
            return
        self._llm_usage_snapshot.record_turn(
            usage=usage,
            history_len=0,
            system_len=0,
            tool_count=0,
            partial=partial,
        )

    # --- 动作透传 ---

    async def _send(self, frame: dict[str, Any]) -> None:
        await self._shim._send(frame)

    def _send_nowait(self, frame: dict[str, Any]) -> None:
        """同步上下文发帧（SIGINT 路径等不能 await）：有运行循环调度任务，
        无则丢弃协程防 RuntimeWarning。与老 CLI 同步动作契约对齐。"""
        coro = self._send(frame)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    def process_message(
        self,
        text: str,
        image_blocks: list[dict[str, Any]] | None = None,
        source: str = "cli-attached",
    ) -> AsyncIterator[str]:
        """发起流式回合（异步生成器）。

        经 transport 发 {"type":"chat","text":...,"image_blocks":[...]}（服务端
        生成 turn_id 并在回合帧回显、source 固定 "cli-attached"）；该回合的流式
        chunk 由服务端 ``chunk`` 帧回来，``turn_end`` 帧终结。服务端各回合帧
        定向投回发起连接，因此无需 turn_id 关联——本地 turn_id 仅作流标识。
        与 RootCoara.foreground_coara.process_message 的消费方式一致::

            agen = fg.process_message(text, image_blocks=..., source="cli-attached")
            async for chunk in agen: ...
        """
        return self._stream_turn(text=text, image_blocks=image_blocks, source=source)

    async def _stream_turn(
        self,
        *,
        text: str,
        image_blocks: list[dict[str, Any]] | None,
        source: str,
    ) -> AsyncIterator[str]:
        # 本地标识（协议不上线）：服务端回合帧定向回本连接，无需关联键。
        turn_id = f"cli-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._turn_streams[turn_id] = {"queue": queue, "done": False, "error": None}
        self._turn_order.append(turn_id)
        try:
            # 本地乐观：一发 chat 就标本端回合（供 has_active_turn / source 门控）；
            # Thinking 仍由 _cli_turn_display_active 驱动，不依赖此字段。
            self._suppress_trace_turn_active = False
            self._active_turn = True
            self._active_turn_source = source or "cli-attached"
            await self._send({"type": "chat", "text": text, "image_blocks": image_blocks or []})
        except Exception:
            self._active_turn = False
            self._active_turn_source = ""
            self._suppress_trace_turn_active = True
            self._turn_streams.pop(turn_id, None)
            with contextlib.suppress(ValueError):
                self._turn_order.remove(turn_id)
            raise
        try:
            while True:
                item = await queue.get()
                if item[0] == "chunk":
                    yield str(item[1])
                elif item[0] == "end":
                    error = item[1]
                    if error:
                        raw = str(error).strip()
                        # 本地 interrupt / 断连收尾不是模型失败——勿包装成 Error: 行
                        # （否则 Ctrl+C 会多出「Error: interrupted」，冲掉原打断体验）。
                        if raw.lower() in {"interrupted", "complete"} or "断开" in raw:
                            return
                        # 软失败：把服务端文案当最后一条 chunk 交付，不抛异常——
                        # 否则 attach runner 会 logger.exception 甩一整段客户端堆栈，
                        # 且 reason-only 的 "error" 会显示成恐怖的「Error: error」。
                        msg = raw
                        if not msg or msg.lower() in {"error", "failed"}:
                            msg = "Error: 回合失败。请重试，或输入 /model 切换模型。"
                        elif not msg.startswith("Error:"):
                            msg = f"Error: {msg}"
                        yield msg
                    return
        finally:
            self._turn_streams.pop(turn_id, None)
            with contextlib.suppress(ValueError):
                self._turn_order.remove(turn_id)
            stale = [sid for sid, local in self._server_turn_map.items() if local == turn_id]
            for sid in stale:
                self._server_turn_map.pop(sid, None)
            # 无其它活跃流时清掉乐观标记，避免 finish_turn 后 Thinking 仍被 has_active_turn 拉住
            if not any(not s.get("done") for s in self._turn_streams.values()):
                self._active_turn = False
                self._active_turn_source = ""
                self._suppress_trace_turn_active = True

    def _feed_turn_frame(self, frame: dict[str, Any]) -> bool:
        """服务端回合帧（chunk/turn_end）路由到本地活跃回合流。

        服务端各回合帧定向投回发起连接，不回显客户端 turn_id——路由策略：
        唯一活跃流直接投递；多流并存时按服务端 turn_id 记忆归属（turn_start
        帧到达时把最新发起流绑定到该 turn_id）。
        """
        frame_type = str(frame.get("type") or "")
        if frame_type not in (
            "chunk",
            "turn_end",
            "chat_chunk",
            "chat_end",
            "turn_start",
            "diff",
            "tool",
            "error",
            "turn_queued",
        ):
            return False
        if frame_type == "diff":
            # diff 不依赖回合流队列：即使 turn 已结束或映射缺失也要投递 callback。
            cb = getattr(self, "_diff_callback", None)
            if cb is None:
                shim = getattr(self, "_shim", None)
                cb = getattr(shim, "_diff_callback", None) if shim is not None else None
            if cb is not None:
                try:
                    cb(frame)
                except Exception:  # noqa: BLE001
                    logger.debug("attach diff frame callback failed", exc_info=True)
            return True
        server_turn_id = str(frame.get("turn_id") or "")
        stream: dict[str, Any] | None = None
        if server_turn_id:
            local_id = self._server_turn_map.get(server_turn_id)
            if local_id is not None:
                candidate = self._turn_streams.get(local_id)
                if candidate is not None and not candidate["done"]:
                    stream = candidate
            elif frame_type == "turn_start":
                # 新回合开始：最新发起的活跃流认领该服务端 turn_id。
                if self._turn_order:
                    local_id = self._turn_order[-1]
                    candidate = self._turn_streams.get(local_id)
                    if candidate is not None and not candidate["done"]:
                        self._server_turn_map[server_turn_id] = local_id
                        stream = candidate
            if stream is None:
                # 唯一活跃流兜底（turn_start 先于注册等边缘场景）。
                active = [s for s in self._turn_streams.values() if not s["done"]]
                if len(active) == 1:
                    stream = active[0]
        else:
            active = [s for s in self._turn_streams.values() if not s["done"]]
            if len(active) == 1:
                stream = active[0]
        if stream is None:
            # 重放帧且本地无活跃流（断连时已终结）：走独立回放回调渲染，
            # 服务端权威帧不丢尾（重建的流无本地消费者，直接交给展示层）。
            if frame.get("replayed"):
                cb = getattr(self, "_replay_callback", None)
                if cb is None:
                    shim = getattr(self, "_shim", None)
                    cb = getattr(shim, "_replay_callback", None) if shim is not None else None
                if cb is not None and server_turn_id:
                    seq = int(frame.get("seq") or 0)
                    last = self._replay_seq.get(server_turn_id, 0)
                    if seq > last:
                        self._replay_seq[server_turn_id] = seq
                        try:
                            cb(frame)
                        except Exception:  # noqa: BLE001
                            logger.debug("attach replay callback failed", exc_info=True)
                    return True
            return False
        # 重放帧去重：重连后服务端把 buffer 重发一遍（replayed 标记），
        # 按 (turn_id, seq) 跳过已消费的帧，避免双显。
        if frame.get("replayed") and server_turn_id:
            seq = int(frame.get("seq") or 0)
            last_seq = int(stream.get("last_seq") or 0)
            if seq and seq <= last_seq:
                return True
        if frame_type == "turn_queued":
            # 排队帧：stream 上置 queued 标记（spinner 改显「排队中」而非思考）；
            # turn_start 到达时清掉（服务端队列转正式回合）。
            stream["queued"] = True
            return True
        if frame_type == "turn_start":
            stream["queued"] = False
            if server_turn_id:
                stream["last_seq"] = max(int(stream.get("last_seq") or 0), int(frame.get("seq") or 0))
            return True
        if frame_type == "tool":
            # ✓/✗ 工具行：服务端 attach 把 process_message 的 ✓/✗ 行独立成 tool 帧
            # （不进正文 chunk 通道）。此处转回 chunk 文本推进回合流，渲染层
            # 对 ✓/✗ 开头的 chunk 按工具行样式展示（老 CLI 语义）。
            text = str(frame.get("text") or "")
            if text.strip():
                stream["queue"].put_nowait(("chunk", text))
                if server_turn_id:
                    stream["last_seq"] = max(int(stream.get("last_seq") or 0), int(frame.get("seq") or 0))
            return True
        if frame_type == "error":
            # 旧服务端曾单独发 error 帧；记下供随后 turn_end 使用。
            pending = str(frame.get("message") or frame.get("error") or "").strip()
            if pending:
                stream["pending_error"] = pending
            return True
        if frame_type in ("chunk", "chat_chunk"):
            if _subagent_body_frame(frame):
                # 归属字段声明子智能体（新内核的 agent_kind）：同样不进前台正文。
                return True
            text = str(frame.get("text") or frame.get("chunk") or "")
            desk = str(frame.get("desk") or "").strip()
            if desk and text.strip() and not stream.get("desk_prefixed"):
                text = f"[{desk}] {text}"
                stream["desk_prefixed"] = True
            stream["queue"].put_nowait(("chunk", text))
            if server_turn_id:
                stream["last_seq"] = max(int(stream.get("last_seq") or 0), int(frame.get("seq") or 0))
            return True
        if frame_type in ("turn_end", "chat_end"):
            stream["done"] = True
            reason = str(frame.get("reason") or "complete")
            error = frame.get("error") or frame.get("message") or stream.get("pending_error")
            if reason in ("failed", "error") and not error:
                error = "回合失败。请重试，或输入 /model 切换模型。"
            stream["queue"].put_nowait(("end", error))
            return True
        return frame_type == "turn_start"

    def _end_all_turns(self, reason: str) -> None:
        """断连时终结所有进行中的回合流（界面可降级提示）。"""
        for stream in self._turn_streams.values():
            if not stream["done"]:
                stream["done"] = True
                stream["queue"].put_nowait(("end", reason))
        self._server_turn_map.clear()
        # 回放去重表随回合终结清理（服务端 turn_id 是一次性 uuid，留着只涨不消）。
        self._replay_seq.clear()

    def interrupt_current_turn(self, reason: str, interrupt_source: str = "") -> bool:
        """打断当前回合（同步 fire-and-forget，与老 CLI CoaraBase 契约一致）。

        调用方有两种：session.py 的 SIGINT/按键路径（同步上下文，直接调用——
        协程实现会被创建后丢弃，interrupt 帧根本发不出去）与界面层的
        fire-and-forget 调度。同步实现两者皆对：能取到运行循环就调度发送任务，
        取不到（极罕见，回合本就无法进行）安全丢弃协程。客户端细粒度来源
        （cli_prompt_ctrl_c / cli_sigint_* 等）经 interrupt 帧透传到内核，服务端
        不再覆盖为 attach_client——trace 能精确还原从哪条路径打断。本地镜像立即
        置不活跃，服务端权威状态经事件回来。
        """
        self._send_nowait({"type": "interrupt", "reason": str(reason), "source": str(interrupt_source or "")})
        # interrupt 打断的是流式消费端：本地回合流立即收尾，不等服务端
        # turn_end 回来（内核回合被中断后 turn_end 才发，等待会卡住界面）。
        self._end_all_turns("interrupted")
        self._active_turn = False
        self._active_turn_source = ""
        self._suppress_trace_turn_active = True
        return True

    async def submit_continuation_input(
        self,
        text: str,
        image_blocks: list[dict[str, Any]] | None = None,
        source: str = "cli-attached",
    ) -> None:
        """跟话入队（透传 type=continuation；服务端固定记 source="cli-attached"）。
        本地镜像同步入队供显示，权威回执经 continuation_input_received 事件
        （服务端须去重或与本镜像一致）。"""
        await self._send({"type": "continuation", "text": text, "image_blocks": image_blocks or []})
        self._continuation_inputs.append(
            ContinuationInput(text=text.strip(), image_blocks=image_blocks, source=source)
        )

    async def cancel_queued_continuation(self, text: str = "") -> bool:
        """撤回队尾跟话（透传 type=cancel_continuation，带目标文本精确删除）。

        本地镜像在调用方（Esc handler 经 pop_latest_queued_followup）已即时
        删除——此处只发 RPC，不再本地 pop（防双删）。"""
        await self._send({"type": "cancel_continuation", "text": str(text or "")})
        return True

    def persist_session_to_disk(self) -> None:
        """会话落盘是内核职责——客户端替身 no-op（Windows 关窗清理会调到它，
        内核侧会话文件由内核自己维护）。"""
        return None

    async def drain_continuation_inputs(self) -> list[ContinuationInput]:
        """取走排队跟话。透传 drain 请求并返回本地镜像出队结果（与本地
        root 的同步语义对齐：chat_runner 每轮循环开头直接消费返回值）；服务端
        回执 continuation_drained 会与本地镜像对账（以服务端为准）。"""
        with contextlib.suppress(Exception):
            await self._send({"type": "drain_continuation"})
        # 让一次事件循环：把尚在 call_soon_threadsafe 排队的 continuation 镜像
        # 事件先应用，避免 drain 丢帧（真实场景服务端处理往返远大于此）。
        await asyncio.sleep(0)
        drained = list(self._continuation_inputs)
        self._continuation_inputs = []
        return drained

    async def execute_command(self, command: str) -> CommandResult:
        """斜杠命令透传：发 {"type":"command"}，等待 command_result 回执。"""
        return await self._shim.execute_command(command)


# 命令回执等待上限。/compact 要拿整段历史跑一遍 LLM 摘要：实测 46 万 token 的
# 会话耗时 30–60s，用默认 30s 会被误报「内核无响应」——内核其实在干活，只是回执
# 晚到，且等待者已出队（回执被静默丢弃，用户以为没执行）。慢命令给足额度，其余
# 命令保持 30s 快速兜底（真挂住时别让用户干等）。
_COMMAND_TIMEOUT_S = 30.0
_SLOW_COMMAND_TIMEOUT_S = 600.0
_SLOW_COMMAND_NAMES = frozenset({"compact"})


def _command_name(command: str) -> str:
    """取斜杠命令名（小写、不含 /）；非命令返回空串。"""
    stripped = str(command or "").strip()
    if not stripped.startswith("/"):
        return ""
    return stripped.split(None, 1)[0][1:].lower()


def _is_slow_command(command: str) -> bool:
    return _command_name(command) in _SLOW_COMMAND_NAMES


def _command_timeout(command: str) -> float:
    return _SLOW_COMMAND_TIMEOUT_S if _is_slow_command(command) else _COMMAND_TIMEOUT_S


class RootShim:
    """root 替身：聚合 event_bus / identity / foreground_coara / workspace_manager。

    用法::

        transport = WsAttachTransport(url, handshake_frame={"type":"attach","workspace":ws})
        await transport.connect()
        shim = RootShim(transport)
        # attached 快照帧到达后 shim.ready 置位，界面骨架照旧消费 root=shim。
    """

    def __init__(self, transport: AttachTransport, loop: asyncio.AbstractEventLoop | None = None) -> None:
        try:
            self._loop: asyncio.AbstractEventLoop | None = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = loop
        self._transport = transport
        self.event_bus = RemoteEventBus(transport, loop=self._loop)
        self.identity = _RemoteIdentity()
        self.foreground_coara = ForegroundCoaraShim(self)
        self.workspace_manager = WorkspaceManagerShim(self)
        self._sessions: dict[str, WorkspaceSessionShim] = {}
        self._foreground_session_id: str | None = None
        self._ready = False
        # @服务台补全名单（attached 快照）
        self.service_desks: list[dict[str, str]] = []
        # command 回执按 request_id 精确匹配（服务端回显）；无 id 时 FIFO 兜底。
        self._command_waiters: list[tuple[str, asyncio.Future[CommandResult]]] = []
        # pending_report 回执（pending_report_result）同理：FIFO 派发。
        # None=未消费（走普通 chat），CommandResult=已消费（客户端直接渲染）。
        self._report_waiters: list[asyncio.Future[CommandResult | None]] = []
        # 本连接 channel_id（attached 握手）：同空间多 CLI 挡它端回合镜像。
        self._attach_channel_id: str = ""
        # 子智能体帧去重：`type:turn_id` → 已消费的最大 seq（重连重发不灌两份）。
        self._subagent_frame_seq: dict[str, int] = {}
        # 重连续接：二次握手携带的 conn_id（服务端换回后重放在飞回合 buffer）。
        self._resume_channel_id: str = ""

        transport.register_handler(self._on_frame)
        self.event_bus.subscribe(self._mirror_event, topic=None)

    # --- 生命周期 ---

    @property
    def ready(self) -> bool:
        """握手快照（attached 帧）已应用。"""
        return self._ready

    @property
    def connected(self) -> bool:
        return self.event_bus.connected

    async def _send(self, frame: dict[str, Any]) -> None:
        await self._transport.send(frame)

    def _send_nowait(self, frame: dict[str, Any]) -> None:
        """同步上下文发帧（connection_state 重连握手等不能 await）：有运行循环
        调度任务，无则丢弃协程防 RuntimeWarning。与 ForegroundCoaraShim 同契约。"""
        coro = self._send(frame)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        loop.create_task(coro)

    # --- root 级接口（界面消费） ---

    def has_active_turn(self) -> bool:
        """代理 foreground。"""
        return self.foreground_coara.has_active_turn()

    def foreground_active_name(self) -> str | None:
        """前台空间名（同步 workspace_manager 快照）。"""
        name = self.workspace_manager.active_name
        if name:
            return name
        # 回退：按 foreground workspace_dir 反查映射。
        return self.workspace_manager.name_for_path(self.foreground_coara.workspace_dir)

    def sync_workspace_manager_to_foreground(self) -> bool:
        """对齐本地 root 的同名方法（slash_pickers 工作空间菜单消费点）。

        客户端镜像里 active_name 已由 workspace_switched 事件/快照维护，
        无需再同步——恒 True（已对齐）。
        """
        return True

    def record_user_activity(self) -> None:
        """真实对话回合前刷新活动计时。

        服务端收到 chat 帧时内核自行 record_user_activity
        （_handle_attach_message），客户端无需发帧；但老 CLI 契约里这是同步
        方法（attached_chat_runner 直接调用），保持同步 no-op——async 实现
        会 RuntimeWarning: coroutine was never awaited。
        """
        return None

    async def execute_command(self, command: str) -> CommandResult:
        """命令透传：发 {"type":"command","text":...,"request_id":...}，等 command_result
        回执（超时兜底；/compact 等慢命令用 _SLOW_COMMAND_TIMEOUT_S）。回执按
        request_id 精确匹配派发（服务端回显）；服务端不回显 request_id（旧内核）
        时按 FIFO 兜底派给最早等待者。"""
        future: asyncio.Future[CommandResult] = asyncio.Future()
        request_id = uuid.uuid4().hex
        self._command_waiters.append((request_id, future))
        try:
            await self._send({"type": "command", "text": command, "request_id": request_id})
        except Exception:
            with contextlib.suppress(ValueError):
                self._command_waiters.remove((request_id, future))
            raise
        try:
            return await asyncio.wait_for(future, timeout=_command_timeout(command))
        except TimeoutError:
            with contextlib.suppress(ValueError):
                self._command_waiters.remove((request_id, future))
            if _is_slow_command(command):
                return CommandResult(
                    output=(
                        "内核还没回执（压缩长历史要一两分钟）。"
                        "命令仍在执行，等一会儿用 /status 看对话条数是否下降即可。"
                    ),
                    action="none",
                )
            return CommandResult(output="命令回执超时（内核无响应）", action="none")

    async def try_consume_pending_report(self, text: str) -> CommandResult | None:
        """待提交 /report 描述拦截（透传 type=pending_report，服务端已实现回执
        pending_report_result）。命中返回 CommandResult（客户端直接渲染），
        未命中返回 None（调用方按普通 chat 走 LLM）。语义对齐本地
        ``try_consume_pending_report_async``。"""
        future: asyncio.Future[CommandResult | None] = asyncio.Future()
        self._report_waiters.append(future)
        try:
            await self._send({"type": "pending_report", "text": text})
        except Exception:
            with contextlib.suppress(ValueError):
                self._report_waiters.remove(future)
            raise
        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            with contextlib.suppress(ValueError):
                self._report_waiters.remove(future)
            # 超时按未消费放行（走普通 chat），不吞用户输入。
            return None

    # --- 帧路由（transport 入站） ---

    def _schedule(self, fn: Any) -> None:
        schedule_threadsafe(self._loop, fn)

    def _on_frame(self, frame: dict[str, Any]) -> None:
        if _frame_trace_on():
            _trace_inbound_frame(frame)
        frame_type = str(frame.get("type") or "")
        if _subagent_body_frame(frame):
            # 子智能体正文帧：不进前台回合流（live 与重放帧同一入口，一并挡）。
            # 漏过去就会与结果回显行重复——同一份报告显示两遍。
            # 挡下的帧转给折叠块（CLI 副作用：明细不进滚动区，只进折叠块）。
            self._deliver_subagent_frame(frame)
            return
        if frame_type == "attached":
            # 服务端扁平快照（无嵌套 snapshot 键）：归一成嵌套结构再应用。
            # 握手帧带 resume 的回执标记：新 channel_id 换回上次连接 id，服务端
            # 已按该 id 重放在飞回合 buffer——镜像归属随之精确归位本连接。
            resumed = str(frame.get("resumed") or "").strip()
            if resumed:
                self._resume_channel_id = ""
            ch = str(frame.get("channel_id") or "").strip()
            if ch:
                self._attach_channel_id = ch
            snapshot = frame.get("snapshot") if isinstance(frame.get("snapshot"), dict) else frame
            self._schedule(lambda: self.apply_snapshot(snapshot))
        elif frame_type == "snapshot":
            snapshot = frame.get("snapshot") or {}
            self._schedule(lambda: self.apply_snapshot(snapshot))
        elif frame_type in (
            "chunk",
            "turn_end",
            "chat_chunk",
            "chat_end",
            "turn_start",
            "diff",
            "tool",
            "error",
            "turn_queued",
        ):
            self._schedule(lambda: self.foreground_coara._feed_turn_frame(frame))
        elif frame_type == "command_result":
            self._schedule(lambda: self._on_command_result(frame))
        elif frame_type == "pending_report_result":
            self._schedule(lambda: self._on_pending_report_result(frame))
        elif frame_type in ("continuation_drained", "drained_continuation_inputs"):
            self._schedule(lambda: self._on_drained_continuation_inputs(frame))
        elif frame_type == "continuation_cancelled":
            self._schedule(lambda: self._on_continuation_cancelled(frame))
        elif frame_type == "context_window":
            self._schedule(lambda: self._on_context_window(frame))
        elif frame_type in ("approval", "approval_request"):
            # 协议帧 approval_request；保留旧名 approval 兼容旧内核
            self._schedule(lambda: self._on_approval_frame(frame))
        elif frame_type == "approval_resolved":
            self._schedule(lambda: self._on_approval_resolved(frame))
        elif frame_type == "connection_state":
            if frame.get("connected"):
                # 重连成功：二次握手携带上次连接的 channel_id，服务端换回 conn_id
                # 后重放本连接 pin 空间在飞回合的 buffer（正文不丢尾）。
                resume_id = str(self._attach_channel_id or self._resume_channel_id or "").strip()
                if resume_id:
                    self._resume_channel_id = resume_id
                    self._send_nowait({"type": "attach", "resume": resume_id})
            else:
                self._schedule(lambda: self._on_disconnected())

    def _deliver_subagent_frame(self, frame: dict[str, Any]) -> None:
        """子智能体正文/结果帧 → 折叠块消费方（attached_chat_runner 注入）。

        去重：帧带 ``turn_id`` + ``seq``（TurnStream 流内帧序号，从 1 起）——
        同 (turn_id, seq) 至多重放一次，重连后服务端重发 buffer 不会把过程正文
        与结果灌两份。seq 缺失（跟话直投/旧协议）时不去重，宁多勿丢。
        """
        cb = getattr(self, "_subagent_frame_callback", None)
        if cb is None:
            return
        turn_id = str(frame.get("turn_id") or "")
        seq = int(frame.get("seq") or 0)
        if turn_id and seq:
            key = f"{frame.get('type')}:{turn_id}"
            if seq <= self._subagent_frame_seq.get(key, 0):
                return
            self._subagent_frame_seq[key] = seq
        try:
            cb(frame)
        except Exception:  # noqa: BLE001
            logger.debug("attach subagent frame callback failed", exc_info=True)

    def _event_for_this_attach(self, payload: dict[str, Any]) -> bool:
        """带 channel_id 的端作用域事件：只跟本连接；缺省放行（旧内核兼容）。"""
        event_ch = str(payload.get("channel_id") or "").strip()
        if not event_ch:
            return True
        my_ch = str(self._attach_channel_id or "").strip()
        if not my_ch:
            return True
        return event_ch == my_ch

    def _on_disconnected(self) -> None:
        """断连收口：终结进行中回合流 + 整树重建兜底（对账丢帧的活动树）。"""
        self.foreground_coara._end_all_turns("与内核连接断开")
        # 活动树丢 subagent_complete 等帧后子树僵死：断连后整树清重建，
        # 与服务端权威状态对齐（_prune_inactive 的超长静默兜底之外的双保险）。
        cb = getattr(self, "_display_resync_callback", None)
        if cb is None:
            shim = getattr(self, "_shim", None)
            cb = getattr(shim, "_display_resync_callback", None) if shim is not None else None
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.debug("attach reconnect resync callback failed", exc_info=True)

    def _on_command_result(self, frame: dict[str, Any]) -> None:
        """command_result 回执：按 request_id 精确派发（服务端回显）；无 id 时 FIFO 兜底。"""
        future: asyncio.Future[CommandResult] | None = None
        request_id = str(frame.get("request_id") or "")
        if request_id:
            # 精确匹配：找到该 request_id 的等待者（服务端回显时）。
            for idx, (rid, fut) in enumerate(self._command_waiters):
                if rid == request_id and not fut.done():
                    self._command_waiters.pop(idx)
                    future = fut
                    break
            if future is None:
                # 迟到回执（等待者已超时移除）：不再派给下一个等待者，直接丢弃。
                return
        else:
            # 无 request_id（旧内核兜底）：FIFO 派给最早等待者。
            while self._command_waiters:
                _, candidate = self._command_waiters.pop(0)
                if not candidate.done():
                    future = candidate
                    break
        if future is None:
            return
        result = frame.get("result") or {}
        if not isinstance(result, dict):
            result = {"output": str(result)}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        action = str(result.get("action") or "none")
        # /ws switch：事件可能因 attach rebind 时序晚到；回执数据先把状态栏跟上。
        if action == "switch_workspace" and isinstance(data, dict):
            self._apply_switch_workspace_chrome(data)
        future.set_result(
            CommandResult(
                output=str(result.get("output") or ""),
                action=action,  # type: ignore[arg-type]
                data=data if isinstance(data, dict) else {},
                exit_session=bool(result.get("exit_session", False)),
            )
        )

    def _apply_switch_workspace_chrome(self, data: dict[str, Any]) -> None:
        """从 /ws switch 回执刷新 shim 状态栏字段（空间名 / 路径 / 模型）。"""
        name = str(data.get("name") or data.get("workspace_name") or "").strip()
        ws_id = str(data.get("workspace_id") or "").strip()
        ws_dir = str(data.get("workspace_dir") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        coara_id = str(data.get("coara_id") or "").strip()
        if name:
            self.workspace_manager._active_name = name
        if ws_id:
            self.workspace_manager._active_workspace_id = ws_id
            self._foreground_session_id = ws_id
        if ws_dir:
            known = self.workspace_manager.name_for_path(ws_dir)
            if (
                known is None
                and (name or ws_id)
                and not any(e.get("id") == ws_id for e in self.workspace_manager._workspaces if ws_id)
            ):
                self.workspace_manager._workspaces.append({"id": ws_id, "name": name or "", "path": ws_dir})
            self.foreground_coara.workspace_dir = ws_dir
            self.foreground_coara.identity.workspace_dir = ws_dir
            self.identity.workspace_dir = ws_dir
        if session_id:
            self.foreground_coara.session_id = session_id
        if coara_id:
            # attach 不推 workspace_switched：回执必须带目标空间 coara_id，
            # 否则回合 completed 会被旧 id 误滤，迟到 thinking 永久卡住 spinner。
            self.foreground_coara.identity.coara_id = coara_id
            self.identity.coara_id = coara_id
        provider = str(data.get("provider_name") or data.get("provider") or "").strip()
        if provider:
            self.foreground_coara.provider_name = provider
        model = str(data.get("model_name") or data.get("model") or "").strip()
        if model:
            self.foreground_coara.model_name = model
        if "is_plan_mode" in data:
            self.foreground_coara.is_plan_mode = bool(data.get("is_plan_mode"))
        self.foreground_coara._active_turn = False
        self.foreground_coara._active_turn_source = ""
        self.foreground_coara._suppress_trace_turn_active = False
        self.foreground_coara._continuation_inputs = []
        self.foreground_coara._llm_usage_snapshot = LlmUsageSnapshot()
        if ws_id:
            entry = self._sessions.get(ws_id) or WorkspaceSessionShim(
                workspace_id=ws_id, workspace_dir=ws_dir, workspace_name=name
            )
            entry.workspace_dir = ws_dir or entry.workspace_dir
            entry.workspace_name = name or entry.workspace_name
            if session_id:
                entry.session_id = session_id
            self._sessions[ws_id] = entry
        # attach 跳过 workspace_switched：此处补活动树/chrome 重建（与事件路径对齐）。
        cb = getattr(self, "_display_chrome_resync_callback", None)
        if cb is None:
            cb = getattr(self, "_display_resync_callback", None)
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.debug("switch_workspace chrome resync callback failed", exc_info=True)

    def _on_pending_report_result(self, frame: dict[str, Any]) -> None:
        """pending_report_result 回执（无 request_id）：FIFO 派给最早等待者。"""
        future: asyncio.Future[CommandResult | None] | None = None
        while self._report_waiters:
            candidate = self._report_waiters.pop(0)
            if not candidate.done():
                future = candidate
                break
        if future is None:
            return
        if not frame.get("consumed"):
            future.set_result(None)
            return
        result = frame.get("result") or {}
        if not isinstance(result, dict):
            result = {"output": str(result)}
        future.set_result(
            CommandResult(
                output=str(result.get("output") or ""),
                action=str(result.get("action") or "none"),  # type: ignore[arg-type]
                data=result.get("data") if isinstance(result.get("data"), dict) else {},
                exit_session=bool(result.get("exit_session", False)),
            )
        )

    def _on_continuation_cancelled(self, frame: dict[str, Any]) -> None:
        """撤回跟话回执：cancelled=false（服务端队列已空/已注入）时对账本地镜像。"""
        if frame.get("cancelled"):
            return
        # 服务端未能撤回：本地乐观 pop 的那条无法确认归属，以清空兜底
        # （下一条 continuation_input_received / drain 回执会重建权威镜像）。
        # 保守起见不动本地镜像——权威状态经事件流回齐。

    def _on_drained_continuation_inputs(self, frame: dict[str, Any]) -> None:
        """服务端权威跟话出队回执（continuation_drained）：以其为准覆盖本地镜像。"""
        items = frame.get("items")
        if not isinstance(items, list):
            return
        self.foreground_coara._continuation_inputs = [
            ContinuationInput(
                text=str(item.get("text") or ""),
                image_blocks=item.get("image_blocks"),
                source=str(item.get("source") or ""),
            )
            for item in items
            if isinstance(item, dict)
        ]

    def _on_context_window(self, frame: dict[str, Any]) -> None:
        with contextlib.suppress(TypeError, ValueError):
            value = frame.get("value")
            if value is not None:
                self.foreground_coara._context_window = int(value)

    def _on_approval_frame(self, frame: dict[str, Any]) -> None:
        """服务端 approval 帧：经回调转给界面层弹 modal，答复回传 approval_reply。

        回调由 attached_chat_runner 注入（它持有 modal/spinner 渲染上下文）；
        未注入（瘦客户端等无界面路径）时记 WARNING——服务端 300s 超时兜底拒绝，
        不静默放行。"""
        cb = getattr(self, "_approval_callback", None)
        if cb is None:
            logger.warning(
                "attach approval frame received but no consumer registered "
                f"(approval_id={frame.get('approval_id')}); will time out server-side"
            )
            return
        try:
            cb(frame)
        except Exception:  # noqa: BLE001
            logger.exception("attach approval callback failed")

    def _on_approval_resolved(self, frame: dict[str, Any]) -> None:
        """服务端 approval_resolved 帧（超时/取消/断连收口）：关掉本地挂着的 modal。

        回调由 attached_chat_runner 注入（持有 approval_id→任务映射）；未注入
        或无对应任务时静默跳过（服务端已按超时/取消收口，无需补偿动作）。"""
        cb = getattr(self, "_approval_resolved_callback", None)
        if cb is None:
            return
        try:
            cb(frame)
        except Exception:  # noqa: BLE001
            logger.debug("attach approval_resolved callback failed", exc_info=True)

    # --- 快照 ---

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        """应用握手/重同步快照：identity + foreground + workspaces + sessions。

        服务端 attached 帧是扁平结构（_build_attach_attached_frame），先经
        ``_normalize_snapshot`` 归一成嵌套结构，再填充各镜像。
        """
        if (
            isinstance(snapshot, dict)
            and ("session_id" in snapshot or "workspace" in snapshot or "workspaces" in snapshot)
            and not ("identity" in snapshot or "foreground" in snapshot)
        ):
            snapshot = _normalize_snapshot(snapshot)
        self._apply_normalized_snapshot(snapshot)

    def _apply_normalized_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        self.identity.apply(snapshot.get("identity") or {})
        self.foreground_coara.apply_snapshot(snapshot.get("foreground") or {})
        self.workspace_manager.apply_snapshot(snapshot.get("workspace_manager") or {})
        sessions = snapshot.get("sessions")
        if isinstance(sessions, dict):
            new_sessions: dict[str, WorkspaceSessionShim] = {}
            for ws_id, data in sessions.items():
                entry = self._sessions.get(str(ws_id)) or WorkspaceSessionShim(workspace_id=str(ws_id))
                entry.apply(data if isinstance(data, dict) else {})
                new_sessions[str(ws_id)] = entry
            self._sessions = new_sessions
        fg_id = snapshot.get("foreground_session_id")
        if fg_id is not None:
            self._foreground_session_id = str(fg_id)
        elif self._foreground_session_id is None:
            # 快照未带：以前台 session 身份兜底（单空间场景）。
            self._foreground_session_id = self.foreground_coara.session_id or None
        desks = snapshot.get("service_desks")
        if isinstance(desks, list):
            self.service_desks = [
                {
                    "name": str(item.get("name") or ""),
                    "summary": str(item.get("summary") or ""),
                }
                for item in desks
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
        self._ready = True

    # --- 事件驱动状态镜像 ---

    def _mirror_event(self, event: TraceEvent) -> None:
        """全订阅镜像：按事件类型驱动各镜像字段。"""
        try:
            self._apply_event(event)
        except Exception as exc:
            logger.debug(f"RootShim: mirror event '{event.event_type}' failed: {exc}")

    def _apply_event(self, event: TraceEvent) -> None:
        topic = event.event_type
        payload = event.payload or {}
        fg = self.foreground_coara

        if topic == "workspace_switched":
            self.workspace_manager._on_workspace_switched(event)
            fg._on_workspace_switched(event)
            ws_id = str(payload.get("workspace_id") or "").strip()
            if ws_id:
                self._foreground_session_id = ws_id
            coara_id = str(payload.get("coara_id") or "").strip()
            if coara_id:
                self.identity.coara_id = coara_id
            ws_dir = str(payload.get("workspace_dir") or "").strip()
            if ws_dir:
                self.identity.workspace_dir = ws_dir
        elif topic == "session_started":
            fg._on_session_started(event)
        elif topic == "plan_mode_changed":
            fg._on_plan_mode_changed(event)
        elif topic == "llm_switched":
            fg._on_llm_switched(event)
        elif topic == "llm_turn_complete":
            fg._on_usage_report(event)
        elif topic == "continuation_input_received":
            # 仅本端跟话入 CLI 排队镜像；web/matrix 的跟话显示在各自端，
            # 绝不镜像到 CLI（各端显示独立，共用会话不等于共用显示）。
            from src.coara.turn_source import cli_shows_source

            origin = str(payload.get("source") or "").strip().lower()
            if cli_shows_source(origin) and self._event_for_this_attach(payload):
                fg._on_continuation_received(event)
        elif topic == "continuation_input_injected":
            fg._on_continuation_injected(event)
        elif topic == "session_auto_new":
            # 自动 /new：清排队跟话镜像；session_id 由随后的 session_started 更新。
            fg._continuation_inputs = []
        elif topic == "chat_chunk":
            # 未带 turn_id 的广播 chunk（其他端发起的回合）：不路由进本地流，
            # 仅用于 turn 活跃推断（下面 _TURN_ACTIVE_TOPICS 处理）。
            pass

        if topic in _TURN_ACTIVE_TOPICS:
            # 仅本端来源的回合驱动本地活跃镜像——web/手机端的回合与 CLI 无关，
            # 绝不让 CLI spinner 跟着其它端转（各端独立零同步）。无 source 的
            # 事件属未知来源，同样不置活跃（真本端回合必带 source）——严格路由。
            # 同空间多 CLI：再按 channel_id 挡它连接（发送端已挡；此处兜底）。
            from src.coara.turn_source import cli_shows_source

            source = str(payload.get("source") or payload.get("turn_source") or "").strip().lower()
            if cli_shows_source(source, unknown=False) and self._event_for_this_attach(payload):
                fg._mark_turn_active(event)
        elif topic in _TURN_END_TOPICS:
            # 结束事件若带它连接 channel_id：不收尾本端（防误清）。
            # 无 channel_id（旧内核 / 打断兜底）仍一律收尾——活跃态本就只可能
            # 被本端事件置起，误收一个 end 无害。
            if not self._event_for_this_attach(payload):
                pass
            else:
                # 子智能体/后台任务的结束事件也走 attach 广播且不带 channel_id
                # （channel 兜底放行）——再按 coara_id 归属过滤：只收本主会话的
                # 结束事件，否则子智能体跑完会把主会话 spinner 误清。
                event_coara = str(getattr(event, "coara_id", "") or "").strip()
                my_coara = str(self.identity.coara_id or self.foreground_coara.identity.coara_id or "").strip()
                if event_coara and my_coara and event_coara != my_coara:
                    pass
                else:
                    fg._mark_turn_end(event)

        self._update_sessions_from_event(event)

    def _update_sessions_from_event(self, event: TraceEvent) -> None:
        """_sessions 结构化快照的增量维护（reconcile 兜底用）。"""
        payload = event.payload or {}
        ws_id = str(payload.get("workspace_id") or "").strip()
        ws_dir = str(payload.get("workspace_dir") or "").strip()
        if not ws_id and ws_dir and ws_dir == self.foreground_coara.workspace_dir:
            ws_id = self._foreground_session_id or ""
        if not ws_id:
            return
        entry = self._sessions.get(ws_id)
        if entry is None:
            entry = WorkspaceSessionShim(workspace_id=ws_id, workspace_dir=ws_dir)
            self._sessions[ws_id] = entry
        if ws_dir:
            entry.workspace_dir = ws_dir
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            entry.session_id = session_id
        if event.event_type in _TURN_ACTIVE_TOPICS:
            from src.coara.turn_source import cli_shows_source

            source = str(payload.get("source") or payload.get("turn_source") or "").strip().lower()
            if cli_shows_source(source, unknown=False) and self._event_for_this_attach(payload):
                entry._active_turn = True
        elif event.event_type in _TURN_END_TOPICS:
            if self._event_for_this_attach(payload):
                entry._active_turn = False
