"""Web 会话视图存储：web 聊天区的服务端持久化载体（唯一数据源）"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from src.core.coara_home import resolve_coara_home, workspace_id_for
from src.core.json_store import interprocess_file_lock
from src.core.logger import logger
from src.core.message_tags import (
    BACKGROUND_RESULT_OPEN,
    MIDRUN_MSG_OPEN,
    SUBAGENT_MSG_OPEN,
    TASK_INSTRUCTION_OPEN,
    is_preformatted_injection,
    strip_llm_only,
)
from src.core.workspace_layout import UPLOAD_DIR_NAME
from src.ui.jsonl_byte_window import iter_jsonl_lines_byte_range
from src.ui.trace_store import _AsyncFileWriter, _FileWriteOp

# message_tags 未导出裸常量；与 conversation_projection 同源
SYSTEM_REMINDER_OPEN = "<系统提醒>"
SYSTEM_INFO_OPEN = "<系统消息>"  # system_info() 同源

_TAIL_READ_BYTES = 256 * 1024

_META_SUFFIX = ".meta.json"
# sidecar：快路径节流写；慢路径（校准）才同步落高水位
_META_WRITE_MIN_INTERVAL_S = 1.0
# 疑似外进程写：最多每 N 秒看一眼 sidecar（不每帧 stat/读盘）
_FOREIGN_SEQ_CHECK_INTERVAL_S = 2.0
_SEQ_LOCK_SUFFIX = ".seq.lock"
# 快照读窗：只读**本线**字节窗口再 parse（一切皆工作空间——绝不跨空间扫带）
_VIEW_READ_WINDOW_BYTES = 4 * 1024 * 1024
_VIEW_READ_MAX_EXPAND = 4

_FOLD_MAX_CALLS = 30
_FOLD_MAX_FRAMES_PER_CALL = 100

# 旧落带：子智能体结果；读端跳过，不回写
_LEGACY_SUBAGENT_BUBBLE_RE = re.compile(r"^\[sa-[a-zA-Z0-9_一-鿿]+-[0-9a-fA-F]+\]")

# 内核注入信封：写/读共用 is_injected_user_text，不冒 user 气泡
_INJECTED_USER_PREFIXES = (
    SUBAGENT_MSG_OPEN,
    MIDRUN_MSG_OPEN,
    BACKGROUND_RESULT_OPEN,
    SYSTEM_REMINDER_OPEN,
    SYSTEM_INFO_OPEN,
)


def resolve_view_meta_path(path: Path) -> Path:
    """线元数据 sidecar 路径（``conversation.jsonl`` → ``conversation.jsonl.meta.json``）。"""
    return path.with_name(path.name + _META_SUFFIX)


def resolve_view_seq_lock_path(path: Path) -> Path:
    """Cross-process lock for view_seq allocation on this line."""
    return path.with_name(path.name + _SEQ_LOCK_SUFFIX)


def _view_seq_file_lock(lock_path: Path):
    """Exclusive lock so two kernels cannot mint the same view_seq on one line."""
    return interprocess_file_lock(lock_path)


def view_line_epoch(workspace_dir: str | Path | None, subject: str, *, generation: int = 0) -> str:
    """线身份 ``{ws}::{subject}[#世代]``；世代变则端上丢缓存全量重取。"""
    base = f"{str(workspace_dir or '')}::{subject}"
    return base if generation <= 0 else f"{base}#{generation}"


# 外挂维护流 desk：帧必须落根线 conversation.jsonl（录像带页只读这条）
_DESK_ROOT_SUBJECTS = frozenset({"janitor", "daily", "记录"})


def view_tape_subject(agent_kind_or_subject: str | None, *, desk: str | None = None) -> str:
    """会话 agent_kind / 帧 subject → 视图线名。

    - ``main`` / 空串 → ``root``（``conversation.jsonl``）
    - desk 为 janitor/daily/记录 → 强制 ``root``（外挂组靠 desk 折叠，不拆文件）
    - ``flow`` / ``subagent`` / 模块 subject 保持原样分文件
    """
    desk_norm = str(desk or "").strip().lower()
    if desk_norm in _DESK_ROOT_SUBJECTS or str(desk or "").strip() in _DESK_ROOT_SUBJECTS:
        return "root"
    raw = str(agent_kind_or_subject or "").strip()
    if raw in ("", "main", "root"):
        return "root"
    return raw


def resolve_web_view_path(
    workspace_dir: str | Path,
    *,
    coara_home: Path | None,
    subject: str,
    session_id: str,
) -> Path:
    """root→空间一条 ``conversation.jsonl``；模块/flow→``{subject}__{session_id}.jsonl``。"""
    subject = view_tape_subject(subject)
    if subject == "flow":
        from src.workflow.paths import workflow_assets_root

        name = f"{subject}__{session_id}.jsonl"
        path = workflow_assets_root(coara_home=coara_home) / "web_views" / name
    else:
        workspace_path = Path(workspace_dir).expanduser().resolve()
        home = resolve_coara_home(workspace_path, coara_home)
        name = "conversation.jsonl" if subject == "root" else f"{subject}__{session_id}.jsonl"
        path = home / "workspaces" / workspace_id_for(workspace_path) / "web_views" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class WebViewStore:
    """web 会话视图存储：逐帧 append + view_seq 分配 + 常态读取聚合。

    一切皆工作空间：``view_seq`` 权威按**线**隔离——线 = 该空间的视图文件 Path
    （``workspaces/<id>/web_views/conversation.jsonl`` 等）。进程内可有多条线并行占号，
    共享的只是分配互斥锁与 writer，**绝无**跨空间共用一个序号水位。
    """

    def __init__(self) -> None:
        self._writer = _AsyncFileWriter()
        # 仅保护下方 per-line 字典的并发突变；不是「全局序号」。
        self._seq_lock = threading.Lock()
        # 每条线（每个空间/subject 文件）已分配到的最大 view_seq。
        self._seq_cache: dict[Path, int] = {}
        # 每条线的元数据内存镜像：{"gen": int, "latest_seq": int, "dirty": bool, "written_ts": float}
        self._line_meta: dict[Path, dict[str, Any]] = {}

    @staticmethod
    def _line_key(path: Path) -> Path:
        """线身份：解析后的绝对路径，避免同一空间因相对/未 resolve 路径拆成两套水位。"""
        try:
            return path.expanduser().resolve()
        except OSError:
            return path

    # 写路径

    def append_event(
        self,
        path: Path,
        *,
        kind: str,
        turn_id: str,
        source: str,
        subject: str,
        session_id: str,
        seq: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """分配 view_seq 并异步落一帧，返回分配到的 view_seq（失败返回 0）"""
        ts_now = time.time()
        # 分隔线只由显式动作（新会话）落帧；web 端没有任何自动/隐式分隔线。
        # 时间只作为消息自身的属性存在（需要时间感时由消息行自己的时间承担）。
        try:
            path = self._line_key(path)
            view_seq = self._next_view_seq(path)
            body = payload or {}
            frame = {
                "view_seq": view_seq,
                "seq": int(seq),
                "turn_id": turn_id,
                "ts": ts_now,
                "source": source,
                "subject": subject,
                "session_id": session_id,
                "kind": kind,
                "payload": body,
            }
            # desk 提到顶层：读端（trajectory）认顶层 desk；只塞 payload 时刷新后外挂组会散
            desk = str(body.get("desk") or "").strip()
            if desk:
                frame["desk"] = desk
            self._writer.append_jsonl(path, frame)
            return view_seq
        except Exception as exc:  # noqa: BLE001 — 视图存储故障绝不中断回合
            logger.warning(f"web view persist failed (path={path}, kind={kind}): {exc}")
            return 0

    def make_persist(self, workspace_dir: str | Path, *, coara_home: Path | None) -> Any:
        """构造注入 TurnStream 的落盘回调：帧 dict（_record 已补 seq/turn_id/source/subject）→ 视图文件"""

        def persist(frame: dict[str, Any]) -> int | None:
            try:
                session_id = str(frame.get("session_id") or "")
                subject = view_tape_subject(frame.get("subject"), desk=frame.get("desk"))
                if not session_id:
                    logger.warning(
                        "web view persist skipped: empty session_id kind={} source={}",
                        frame.get("type") or frame.get("kind"),
                        frame.get("source"),
                    )
                    return None
                path = resolve_web_view_path(
                    workspace_dir, coara_home=coara_home, subject=subject, session_id=session_id
                )
                kind = str(frame.get("type") or "")
                if kind == "subagent_chunk":
                    # 子智能体过程正文一律落带（刷新/断线回放靠它）；读端按父标识归集进
                    # subagent_texts[父 call_id]，不投影成主会话消息。
                    text = str(frame.get("text") or "")
                    if not text.strip():
                        return None
                    payload = {"text": text}
                    tool_call_id = str(frame.get("tool_call_id") or "")
                    parent_tool_call_id = str(frame.get("parent_tool_call_id") or "") or tool_call_id
                    if tool_call_id:
                        payload["tool_call_id"] = tool_call_id
                    if parent_tool_call_id:
                        # 与 tool_call_id 同写：旧出口只传 tool_call_id 时这里补 parent，hydrate 才能归集
                        payload["parent_tool_call_id"] = parent_tool_call_id
                elif kind == "diff":
                    # 落盘与广播分离——这里负责映射。
                    diff_lines = frame.get("diff_lines")
                    payload: dict[str, Any] = {"diff": diff_lines} if isinstance(diff_lines, dict) else {}
                    # 产生它的那次工具调用 id（与同工具 tool 行同值）：端上至此 不靠相邻关系猜位置。主会话 diff
                    # 也一并落（同一帧字段）。
                    tool_call_id = str(frame.get("tool_call_id") or "")
                    if tool_call_id:
                        payload["tool_call_id"] = tool_call_id
                    # 子智能体产生的 diff：另存父标识与折叠区渲染材料——build_messages 不把它投影成主流消息，而是归集进
                    # subagent_diffs[父 call_id]。
                    parent_tool_call_id = str(frame.get("parent_tool_call_id") or "")
                    if parent_tool_call_id:
                        payload["parent_tool_call_id"] = parent_tool_call_id
                        payload["display_blocks"] = frame.get("display_blocks")
                        payload["tool_name"] = str(frame.get("tool_name") or "")
                        # 节点归属：同一个父行下挂着多个子智能体（flow 编排节点）时，
                        # 端上靠它把帧分到各自节点（三级折叠）；丢了只能整片平铺
                        subagent_id = str(frame.get("subagent_id") or "")
                        if subagent_id:
                            payload["subagent_id"] = subagent_id
                        coara_id = str(frame.get("coara_id") or "")
                        if coara_id:
                            payload["coara_id"] = coara_id
                else:
                    payload = {
                        k: v
                        for k, v in frame.items()
                        if k not in ("type", "seq", "turn_id", "source", "subject", "session_id")
                    }
                view_seq = self.append_event(
                    path,
                    kind=kind,
                    turn_id=str(frame.get("turn_id") or ""),
                    source=str(frame.get("source") or ""),
                    subject=subject,
                    session_id=session_id,
                    seq=int(frame.get("seq") or 0),
                    payload=payload,
                )
                return view_seq or None
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"web view persist callback failed: {exc}")
                return None

        return persist

    def _next_view_seq(self, path: Path) -> int:
        """分配下一个 view_seq：同一条线内单调，文件缺失/清空也不回退。

        快路径：内存水位 +1 + sidecar 节流写。慢路径：冷启动 / reset / 外进程争用时校准。
        """
        path = self._line_key(path)
        with self._seq_lock:
            if self._seq_fast_path_ready(path):
                return self._mint_view_seq_fast(path)
            return self._mint_view_seq_calibrated(path)

    def _seq_fast_path_ready(self, path: Path) -> bool:
        """内存高水位已校准且无线重置 / 外进程争用迹象。"""
        if path not in self._seq_cache:
            return False
        meta = self._line_meta.get(path)
        if meta is None or meta.get("reset_baseline"):
            return False
        return not self._foreign_seq_writer_suspected(path, meta)

    def _foreign_seq_writer_suspected(self, path: Path, meta: dict[str, Any]) -> bool:
        """sidecar 上的序号明显超前于本进程内存 → 可能有第二写者，触发一次校准。

        本进程 dirty / write_pending 时磁盘落后是预期，不算外进程。
        检查有间隔，避免快路径每帧 stat/读 sidecar。
        """
        if meta.get("dirty") or meta.get("write_pending"):
            return False
        now = time.time()
        last = float(meta.get("foreign_check_ts") or 0.0)
        if now - last < _FOREIGN_SEQ_CHECK_INTERVAL_S:
            return False
        meta["foreign_check_ts"] = now
        meta_path = resolve_view_meta_path(path)
        try:
            st = meta_path.stat()
        except OSError:
            return False
        # 本进程刚写过且 mtime 未变——跳过读内容
        if st.st_mtime <= float(meta.get("written_ts") or 0.0) + 0.001:
            return False
        disk_seq = int(self._read_sidecar_dict(path).get("latest_seq") or 0)
        return disk_seq > int(meta.get("latest_seq") or 0)

    def _mint_view_seq_fast(self, path: Path) -> int:
        """已校准：内存 +1 + sidecar 节流。调用方持 ``_seq_lock``。"""
        nxt = int(self._seq_cache[path]) + 1
        self._seq_cache[path] = nxt
        meta = self._line_meta[path]
        meta["latest_seq"] = nxt
        meta["dirty"] = True
        self._persist_line_meta(path)
        return nxt

    def _mint_view_seq_calibrated(self, path: Path) -> int:
        """冷/重置/争用：跨进程锁内取 max(sidecar, jsonl 尾, 内存) 后同步落盘。调用方持 ``_seq_lock``。"""
        with _view_seq_file_lock(resolve_view_seq_lock_path(path)):
            mem = self._line_meta.get(path)
            reset_baseline = bool(mem and mem.get("reset_baseline"))
            disk = self._read_sidecar_dict(path)
            disk_gen = int(disk.get("gen") or 0)
            disk_seq = int(disk.get("latest_seq") or 0)
            mem_gen = int(mem["gen"]) if mem is not None else 0
            gen = max(disk_gen, mem_gen)
            jsonl_hi = read_latest_view_seq(path)
            cached = int(self._seq_cache[path]) if path in self._seq_cache else 0
            if reset_baseline:
                # reset 内存 latest_seq=0 是重估基准，不能参与 max（会挡住磁盘高水位）
                base = max(disk_seq, jsonl_hi, cached)
            else:
                mem_seq = int(mem["latest_seq"]) if mem is not None else 0
                # write_pending 时内存可能比 sidecar 新
                pending_seq = mem_seq if mem is not None and mem.get("write_pending") else 0
                base = max(disk_seq, jsonl_hi, mem_seq, cached, pending_seq)
            nxt = base + 1
            self._seq_cache[path] = nxt
            meta = {
                "gen": gen,
                "latest_seq": nxt,
                "dirty": True,
                "existing": bool(disk) or (mem is not None and bool(mem.get("existing"))),
                "written_ts": float(mem.get("written_ts") or 0.0) if mem is not None else 0.0,
                "write_pending": False,
                "foreign_check_ts": time.time(),
            }
            self._line_meta[path] = meta
            payload = {"gen": gen, "latest_seq": nxt, "updated_ts": time.time()}
            self._write_line_meta_sync(path, meta, payload)
            return nxt

    @staticmethod
    def _read_sidecar_dict(path: Path) -> dict[str, Any]:
        """读 sidecar 原始 dict；缺失/损坏返回空。"""
        try:
            parsed = json.loads(resolve_view_meta_path(path).read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # 线身份（世代）与元数据

    def _write_line_meta_sync(self, path: Path, meta: dict[str, Any], payload: dict[str, Any]) -> None:
        """在调用线程内原子落盘 sidecar 并同步镜像（失败只记日志）"""
        try:
            from src.core.json_store import write_json_atomic

            write_json_atomic(resolve_view_meta_path(path), payload)
        except Exception as exc:  # noqa: BLE001 — 元数据故障绝不中断落帧
            logger.debug(f"web view meta persist failed (path={path}): {exc}")
        meta["dirty"] = False
        meta["existing"] = True
        meta["written_ts"] = time.time()
        meta["write_pending"] = False

    def _load_line_meta(self, path: Path) -> dict[str, Any]:
        """读该线的元数据（内存镜像优先；sidecar 缺失/损坏按新线处理）"""
        meta = self._line_meta.get(path)
        if meta is not None:
            return meta
        data = self._read_sidecar_dict(path)
        meta = {
            "gen": int(data.get("gen") or 0),
            "latest_seq": int(data.get("latest_seq") or 0),
            "dirty": False,
            # sidecar 之前是否存在：只读路径（快照 epoch 读世代）会加载元数据， 但绝不能因为 flush/close
            # 就往别的空间的目录里凭空写一个 sidecar。
            "existing": bool(data),
            "written_ts": 0.0,
            "write_pending": False,
            "foreign_check_ts": 0.0,
        }
        self._line_meta[path] = meta
        return meta

    def _persist_line_meta(self, path: Path, *, force: bool = False) -> None:
        """投递 sidecar 落盘（节流；force 同步写——flush/close 收口用）"""
        meta = self._line_meta.get(path)
        if meta is None:
            return
        if not meta["dirty"] and (not force or not meta["existing"]):
            return
        if meta.get("reset_baseline"):
            # reset_line 的 latest_seq=0 是重估基准而非高水位：永不经本函数落盘 （会盖住 FIFO 里已投递的更高水位）；gen
            # 推进由 reset_line 同步落盘。
            return
        now = time.time()
        if not force and now - float(meta["written_ts"] or 0.0) < _META_WRITE_MIN_INTERVAL_S:
            return
        payload = {"gen": int(meta["gen"]), "latest_seq": int(meta["latest_seq"]), "updated_ts": now}
        if force:
            self._write_line_meta_sync(path, meta, payload)
            return
        meta["dirty"] = False
        meta["existing"] = True
        meta["written_ts"] = now
        meta["write_pending"] = True
        target = resolve_view_meta_path(path)
        try:
            from src.core.json_store import write_json_atomic

            def _write_sidecar() -> None:
                try:
                    write_json_atomic(target, payload)
                except Exception as exc:  # noqa: BLE001 — 元数据故障绝不中断落帧
                    logger.debug(f"web view meta persist failed (path={target}): {exc}")
                finally:
                    meta["write_pending"] = False

            self._writer.enqueue(_FileWriteOp(path=target, text="", append=False, callback=_write_sidecar))
        except Exception as exc:  # noqa: BLE001 — 元数据故障绝不中断落帧
            meta["write_pending"] = False
            logger.debug(f"web view meta persist failed (path={target}): {exc}")

    def _wait_writer_turn(self, timeout: float = 5.0) -> None:
        """等待 worker 执行完此刻之前已投递的全部 op（sentinel 屏障）"""
        done = threading.Event()
        self._writer.enqueue(_FileWriteOp(path=Path(), text="", append=False, done=done))
        if not done.wait(timeout=timeout):
            logger.debug("web view writer barrier wait timeout")

    def line_generation(self, path: Path) -> int:
        """该线当前世代（0 = 从未重建）。快照的 epoch 由它参与构造。"""
        path = self._line_key(path)
        with self._seq_lock:
            return int(self._load_line_meta(path)["gen"])

    def reset_line(self, path: Path) -> int:
        """重建这条线：世代 +1（epoch 随之变化）、序号基准归零重估"""
        path = self._line_key(path)
        with self._seq_lock:
            meta = self._load_line_meta(path)
            meta["gen"] = int(meta["gen"]) + 1
            meta["latest_seq"] = 0
            meta["dirty"] = True
            meta["written_ts"] = 0.0
            meta["reset_baseline"] = True
            self._seq_cache.pop(path, None)
            self._wait_writer_turn()
            disk_hi = int(self._read_sidecar_dict(path).get("latest_seq") or 0)
            payload = {"gen": int(meta["gen"]), "latest_seq": disk_hi, "updated_ts": time.time()}
            self._write_line_meta_sync(path, meta, payload)
            logger.info(f"web view line reset (gen={meta['gen']}, path={path})")
            return int(meta["gen"])

    # 生命周期（flush/close 收口：等待已投递的 meta 写落盘）

    def _wait_meta_writes(self, timeout: float = 2.0) -> None:
        """强制落脏 sidecar，并等待异步写收口（flush/close 用，尽力而为）"""
        with self._seq_lock:
            for meta_path in list(self._line_meta):
                self._persist_line_meta(meta_path, force=True)
        deadline = time.monotonic() + timeout
        settled_once = False
        while time.monotonic() < deadline:
            with self._seq_lock:
                settled = all(
                    not bool(m["dirty"]) and not bool(m.get("write_pending")) for m in self._line_meta.values()
                )
            if settled:
                settled_once = True
                break
            time.sleep(0.01)
        if settled_once:
            self._writer.flush(timeout=max(0.0, deadline - time.monotonic()))

    def flush(self, timeout: float | None = None) -> bool:
        self._wait_meta_writes(timeout=timeout if timeout is not None else 2.0)
        return self._writer.flush(timeout=timeout)

    def close(self) -> None:
        self._wait_meta_writes()
        try:
            self._writer.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"web view store close failed: {exc}")

    # 读路径（常态 hydrate）

    @staticmethod
    def iter_frames(path: Path) -> list[dict[str, Any]]:
        """按落盘顺序读**本线**全部可解析帧（坏行跳过）。测试/诊断用；hydrate 走窗口读。"""
        frames: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(frame, dict):
                        frames.append(frame)
        except OSError:
            return []
        return frames

    @staticmethod
    def _iter_lines_byte_range(path: Path, start: int, end: int) -> list[str]:
        """读本线 [start, end) 内完整行（与 trajectory 共用 jsonl_byte_window）。"""
        return iter_jsonl_lines_byte_range(path, start, end)

    @classmethod
    def _parse_frames_byte_range(cls, path: Path, start: int, end: int) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for line in cls._iter_lines_byte_range(path, start, end):
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                frames.append(frame)
        return frames

    @staticmethod
    def _min_view_seq(frames: list[dict[str, Any]]) -> int:
        lo = 0
        saw = False
        for frame in frames:
            try:
                seq = int(frame.get("view_seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= 0:
                continue
            if not saw or seq < lo:
                lo = seq
                saw = True
        return lo if saw else 0

    @classmethod
    def iter_frames_windowed(
        cls,
        path: Path,
        *,
        since_seq: int = 0,
        before_seq: int = 0,
        window_bytes: int = _VIEW_READ_WINDOW_BYTES,
        max_expand: int = _VIEW_READ_MAX_EXPAND,
    ) -> tuple[list[dict[str, Any]], bool]:
        """按字节窗口读本线帧。返回 (frames_asc, has_older_bytes)。"""
        try:
            file_size = path.stat().st_size
        except OSError:
            return [], False
        if file_size <= 0:
            return [], False

        window_bytes = max(64 * 1024, int(window_bytes))
        max_expand = max(1, int(max_expand))

        if before_seq > 0:
            # 扩窗必须一次性重读连续区间（拼接两个相邻窗会在边界各丢半行）。
            start = end = file_size
            frames: list[dict[str, Any]] = []
            for _ in range(max_expand):
                start = max(0, end - window_bytes)
                frames = cls._parse_frames_byte_range(path, start, end)
                min_seq = cls._min_view_seq(frames) if frames else 0
                if start == 0 or (min_seq > 0 and min_seq < before_seq and len(frames) >= 8):
                    break
                window_bytes *= 2
                end = file_size
            return frames, start > 0

        # 尾窗：初始 hydrate 或 after_view_seq 增量（同款：扩窗整体重读，不拼接）
        end = file_size
        start = max(0, end - window_bytes)
        frames = cls._parse_frames_byte_range(path, start, end)
        expands = 0
        while since_seq > 0 and start > 0 and expands < max_expand - 1:
            min_seq = cls._min_view_seq(frames)
            if min_seq > 0 and min_seq <= since_seq:
                break
            expands += 1
            start = max(0, end - window_bytes * (expands + 1))
            frames = cls._parse_frames_byte_range(path, start, end)
        return frames, start > 0

    @classmethod
    def build_messages(
        cls,
        path: Path,
        *,
        limit: int,
        merge_chunks: bool = False,
        since_seq: int = 0,
        before_seq: int = 0,
        source_filter: str | None = None,
        subagent_out: dict[str, str] | None = None,
        subagent_diffs_out: dict[str, list[dict[str, Any]]] | None = None,
        subagent_briefs_out: dict[str, str] | None = None,
        subagent_texts_out: dict[str, str] | None = None,
        fold_truncated_out: dict[str, int] | None = None,
        full_scan: bool = False,
        has_older_out: list[bool] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """从视图帧聚合聊天行：(messages, total, latest_view_seq)。

        默认对本线做字节窗口读；``full_scan=True`` 仅测试/排障。
        ``has_older_out``（可选）收一个布尔：字节窗未读到文件头为 True——
        翻页到底的权威判据（行数不足一页可能只是窗口截断，不是历史到头）。
        """
        if full_scan:
            frames = cls.iter_frames(path)
            has_older_bytes = False
        else:
            # 尾窗/增量：iter_frames_windowed 整体重读连续区间（不拼接相邻窗）。
            # 初始 hydrate（since/before 皆 0）另按帧数扩窗——密帧长会话单窗可能不够一页消息。
            need_frames = max(int(limit) * 24, 240)
            window = _VIEW_READ_WINDOW_BYTES
            frames, has_older_bytes = cls.iter_frames_windowed(
                path, since_seq=since_seq, before_seq=before_seq, window_bytes=window
            )
            expand = 1
            while (
                has_older_bytes
                and since_seq <= 0
                and before_seq <= 0
                and len(frames) < need_frames
                and expand < _VIEW_READ_MAX_EXPAND
            ):
                expand += 1
                window = _VIEW_READ_WINDOW_BYTES * expand
                frames, has_older_bytes = cls.iter_frames_windowed(
                    path, since_seq=0, before_seq=0, window_bytes=window
                )
        latest_seq = 0
        for frame in frames:
            try:
                latest_seq = max(latest_seq, int(frame.get("view_seq") or 0))
            except (TypeError, ValueError):
                continue

        turns: dict[str, dict[str, Any]] = {}
        order: list[dict[str, Any]] = []
        # 折叠映射的排名（{call_id: 最新 view_seq}）：只服务末尾裁剪，不对外暴露
        result_rank: dict[str, int] = {}
        brief_rank: dict[str, int] = {}
        text_rank: dict[str, int] = {}
        # （指令帧晚一拍）；行晚于指令帧（历史数据落盘次序不齐）时靠预扫兜底认
        delegate_calls_by_turn: dict[tuple[str, str], str] = {}
        # 分隔标记帧（新会话/模型切换）：无 turn_id，直接按落盘顺序占一行， 渲染成时间线分隔线（role=assistant +
        # divider 字段，前端识别成分隔线）。
        dividers: list[tuple[int, dict[str, Any]]] = []
        _bootstrap_kinds = frozenset(
            {"turn_start", "user_message", "chunk", "error", "diff", "files", "tool", "turn_end"}
        )
        for frame in frames:
            kind = str(frame.get("kind") or "")
            turn_id = str(frame.get("turn_id") or "")
            payload = frame.get("payload") or {}
            try:
                view_seq = int(frame.get("view_seq") or 0)
            except (TypeError, ValueError):
                view_seq = 0
            if source_filter:
                frame_source = str(frame.get("source") or "")
                if frame_source and frame_source != source_filter:
                    # 他端落带帧：是线上事实（latest_seq 已覆盖），但不属于本端显示
                    continue
            # 主会话 delegate 工具行登记到所在 turn：老指令帧缺父行标识时 按它反查归集（单遍按时间线推进，
            # 最新一行胜出——同行内多次 delegate 时指令认最近那条发起行）。
            if (
                kind == "tool"
                and not str(payload.get("parent_tool_call_id") or "")
                and str(payload.get("tool_name") or "") == "delegate"
            ):
                tc_id = str(payload.get("tool_call_id") or "")
                if turn_id and tc_id:
                    delegate_calls_by_turn[(str(frame.get("session_id") or ""), turn_id)] = tc_id
            if kind == "divider":
                label = str(payload.get("label") or "").strip()
                try:
                    ts = float(frame.get("ts") or 0.0)
                except (TypeError, ValueError):
                    ts = 0.0
                if label:
                    entry: dict[str, Any] = {
                        "role": "assistant",
                        "text": "",
                        "divider": label,
                        "seq": view_seq,
                    }
                    # 老帧无 ts（补写前落盘）不给时间标签，避免与消息时间对不上
                    if ts > 0:
                        entry["ts"] = ts
                    dividers.append((view_seq, entry))
                continue
            if kind == "subagent_result":
                # （回合已收尾时落带）也要收，故在此处先于回合归属判定处理。
                if subagent_out is not None and view_seq > since_seq:
                    tc_id = str(payload.get("tool_call_id") or "")
                    text = str(payload.get("text") or "")
                    # delegate 行——读端与写端出口（_emit_end_frame）同一把尺。
                    if tc_id and text.strip() and not is_preformatted_injection(text):
                        subagent_out[tc_id] = text
                        result_rank[tc_id] = view_seq
                continue
            if kind == "subagent_chunk":
                # 子智能体过程正文（已落带）：按父 call_id 顺序拼接归集给折叠区，不投影成
                # 主会话消息。与实时路径（store 侧累加）同一把尺。
                # parent 优先；仅有 tool_call_id 的旧落带（出口曾漏传 parent）回退认它。
                if subagent_texts_out is not None and view_seq > since_seq:
                    parent_call_id = str(
                        payload.get("parent_tool_call_id") or payload.get("tool_call_id") or ""
                    )
                    text = str(payload.get("text") or "")
                    if parent_call_id and text and not is_preformatted_injection(text):
                        subagent_texts_out[parent_call_id] = subagent_texts_out.get(parent_call_id, "") + text
                        text_rank[parent_call_id] = view_seq
                continue
            if _is_brief_frame(kind, payload):
                # 标记都算——读端识别，不改写用户数据。
                if subagent_briefs_out is not None and view_seq > since_seq:
                    call_id = str(payload.get("parent_tool_call_id") or "")
                    text = str(payload.get("content") or "")
                    if not call_id:
                        # 先看这帧之前已登记的（工具行先落带的常态），没有就
                        session_id = str(frame.get("session_id") or "")
                        call_id = delegate_calls_by_turn.get((session_id, turn_id), "") or _next_delegate_call_id(
                            frames, frame, session_id, turn_id
                        )
                    if text.strip():
                        subagent_briefs_out[call_id] = text
                        brief_rank[call_id] = view_seq
                continue
            if _is_injected_user_frame(kind, payload):
                continue
            parent_tool_call_id = str(payload.get("parent_tool_call_id") or "")
            if parent_tool_call_id and kind in ("tool", "diff") and view_seq > since_seq:
                # 子智能体自己的工具行 / 改动：不进主会话正文流，按父 call_id 归集给端上的 delegate
                # 折叠区（同样先于回合归属判定处理）。
                if subagent_diffs_out is not None:
                    subagent_diffs_out.setdefault(parent_tool_call_id, []).append(
                        _subagent_frame_entry(kind, payload, view_seq)
                    )
                continue
            if turn_id and turn_id not in turns and kind in _bootstrap_kinds:
                turns[turn_id] = {
                    "turn_id": turn_id,
                    "ended": False,
                    "texts": [],  # (view_seq, text, is_command_result)
                    "blocks": [],  # (view_seq, entry)
                    "users": [],  # (view_seq, content, attachments|None, delegate_task, client_msg_id) 同回合多条跟话
                    "last_seq": view_seq,
                }
                order.append(turns[turn_id])
            if turn_id not in turns:
                continue
            turn = turns[turn_id]
            turn["last_seq"] = max(turn["last_seq"], view_seq)
            if kind == "turn_start":
                continue
            if kind == "user_message":
                # 两条，切空间整线重建时同一句话会显示多个气泡。
                cmid = str(payload.get("client_msg_id") or "")
                if cmid and any(existing[4] == cmid for t in turns.values() for existing in t["users"]):
                    continue
                atts = payload.get("attachments")
                turn["users"].append(
                    (
                        view_seq,
                        str(payload.get("content") or ""),
                        atts if isinstance(atts, list) and atts else None,
                        bool(payload.get("delegate_task")),  # delegate 指令跟话标记
                        cmid,
                    )
                )
            elif kind == "chunk":
                text = str(payload.get("text") or "")
                if text.strip() and not _LEGACY_SUBAGENT_BUBBLE_RE.match(text.lstrip()):
                    turn["texts"].append((view_seq, text, bool(payload.get("is_command_result"))))
            elif kind == "error":
                message = str(payload.get("message") or "")
                if message:
                    turn["blocks"].append((view_seq, {"role": "assistant", "text": message, "seq": view_seq}))
            elif kind == "diff":
                # 落盘统一格式 payload={"diff": canonical diff_lines}（主回合/ 唤醒/跟话三路一致，均含 diff 键）。
                # 旧的不一致帧不兼容——破相就破相。
                diff = payload.get("diff")
                if isinstance(diff, dict) and diff.get("hunks"):
                    turn["blocks"].append(
                        (
                            view_seq,
                            {
                                "role": "assistant",
                                "text": "",
                                "diff": diff,
                                # 产出它的工具调用 id：端上精确挂位（相邻关系不可靠）
                                "tool_call_id": str(payload.get("tool_call_id") or ""),
                                "seq": view_seq,
                            },
                        )
                    )
            elif kind == "tool":
                # 与实时插入同一份数据，刷新回放位置不变。
                label = str(payload.get("text") or "").strip()
                if label:
                    turn["blocks"].append(
                        (
                            view_seq,
                            {
                                "role": "assistant",
                                "text": "",
                                "tool": {
                                    "label": label,
                                    "ok": bool(payload.get("ok", True)),
                                    "tool_name": str(payload.get("tool_name") or ""),
                                    "tool_call_id": str(payload.get("tool_call_id") or ""),
                                    "duration_ms": payload.get("duration_ms"),
                                },
                                "seq": view_seq,
                            },
                        )
                    )
            elif kind == "files":
                files = payload.get("files")
                if isinstance(files, list) and files:
                    files_entry: dict[str, Any] = {
                        "role": "assistant",
                        "text": str(payload.get("caption") or ""),
                        "files": files,
                        "seq": view_seq,
                    }
                    turn["blocks"].append((view_seq, files_entry))
            elif kind == "turn_end":
                turn["ended"] = True

        messages: list[dict[str, Any]] = []
        # 「上次回合中断」由启动恢复路径的 in-flight 注记承载（INTERRUPTED_SESSION_NOTE）， 不在此处猜测
        for turn in order:
            # 用户行与助手产出按 view_seq 交织：前段输出 → 跟话 → 后段输出 孤儿 delegate 指令 turn（历史他端 delegate
            # 泄漏帧——只有伪造的 user_message、无任何正文落盘）不渲染：无正文说明该回合正文根本 不在这份 web
            # 视图里（attach/matrix 回合不写 web），仅剩的指令 气泡是假 web 输入。
            _user_raw = [
                (seq, content, atts, delegate, cid)
                for seq, content, atts, delegate, cid in turn["users"]
                if str(content).strip() or atts
            ]
            _has_body = bool(turn["texts"] or turn["blocks"])
            user_items: list[tuple[int, dict[str, Any]]] = [
                (
                    seq,
                    {
                        "role": "user",
                        "text": content,
                        "seq": seq,
                        # hydrate 按 client_msg_id 配对本端乐观气泡
                        **({"client_msg_id": cid} if cid else {}),
                        **({"attachments": atts} if atts else {}),
                    },
                )
                for seq, content, atts, delegate, cid in _user_raw
                if _has_body or not delegate
            ]
            if merge_chunks and turn["texts"]:
                merged_text = "".join(t for _, t, _cmd in turn["texts"])
                entry = {"role": "assistant", "text": merged_text, "seq": turn["last_seq"]}
                # merge 模式：用户行仍按 seq 插在合并正文之前/之间；块跟在后
                items: list[tuple[int, dict[str, Any]]] = list(user_items)
                first_text_seq = turn["texts"][0][0] if turn["texts"] else turn["last_seq"]
                items.append((first_text_seq, entry))
                items.extend(turn["blocks"])
                for _seq, _row in sorted(items, key=lambda x: x[0]):
                    messages.append(_row)
            else:
                items = list(user_items)
                for seq, text, is_cmd in turn["texts"]:
                    row: dict[str, Any] = {"role": "assistant", "text": text, "seq": seq}
                    if is_cmd:
                        row["is_command_result"] = True
                    items.append((seq, row))
                items.extend(turn["blocks"])
                for _seq, entry in sorted(items, key=lambda x: x[0]):
                    messages.append(entry)

        # 分隔标记帧按 view_seq 归位到消息序列的对应时间点（新会话/模型切换 分隔线出现在它发生的位置，而非堆在末尾）。
        if dividers:
            combined: list[tuple[int, dict[str, Any]]] = [(int(m.get("seq") or 0), m) for m in messages]
            combined.extend(dividers)
            combined.sort(key=lambda x: x[0])
            messages = [m for _, m in combined]

        # 上传附件（ref 落盘）在出口补 inline 直出 URL，前端气泡直接渲染。
        for m in messages:
            atts = m.get("attachments")
            if not isinstance(atts, list):
                continue
            for att in atts:
                if isinstance(att, dict) and not att.get("url") and att.get("ref"):
                    att["url"] = f"/api/workspace/file-raw?path={UPLOAD_DIR_NAME}/{att['ref']}"

        # 否则同一个字段在带游标与不带游标时是两种含义。
        total = len(messages)
        if since_seq > 0:
            messages = [m for m in messages if int(m.get("seq") or 0) > since_seq]
        # 端侧 prepend 到头部，与尾部增量（since_seq）互为镜像，互不相交。
        if before_seq > 0:
            messages = [m for m in messages if int(m.get("seq") or 0) < before_seq]
        if len(messages) > limit:
            messages = messages[-limit:]
        # 字节窗未读到文件头：告知端上还有更早历史（hasMoreHistory = total > len）
        if has_older_bytes and total <= len(messages):
            total = len(messages) + 1
        if has_older_out is not None:
            has_older_out.clear()
            has_older_out.append(has_older_bytes)
        # 会让端侧把被裁掉的帧当成「已送达」永不再补，屏幕历史出现永久静默空洞
        if messages:
            latest_seq = int(messages[-1].get("seq") or 0)
        # 标记——封顶是边界，不是静默丢弃。
        dropped_results = _trim_fold_text_map(subagent_out, result_rank)
        dropped_briefs = _trim_fold_text_map(subagent_briefs_out, brief_rank)
        dropped_texts = _trim_fold_text_map(subagent_texts_out, text_rank)
        dropped_frames = _trim_fold_frame_map(subagent_diffs_out)
        if fold_truncated_out is not None:
            fold_truncated_out["results"] = dropped_results
            fold_truncated_out["diffs"] = dropped_frames
            fold_truncated_out["briefs"] = dropped_briefs
            fold_truncated_out["texts"] = dropped_texts
        return messages, total, latest_seq


def _next_delegate_call_id(frames: list[dict[str, Any]], current: dict[str, Any], session_id: str, turn_id: str) -> str:
    """brief 反查兜底：同 session 同 turn_id 内当前帧之后最近的 delegate 工具行 call_id"""
    if not turn_id:
        return ""
    try:
        start = frames.index(current) + 1
    except ValueError:
        return ""
    for frame in frames[start:]:
        if str(frame.get("session_id") or "") != session_id:
            continue
        if str(frame.get("turn_id") or "") != turn_id:
            continue
        if str(frame.get("kind") or "") != "tool":
            continue
        payload = frame.get("payload") or {}
        if str(payload.get("parent_tool_call_id") or ""):
            continue  # 子智能体自己的工具行，不是主会话发起行
        if str(payload.get("tool_name") or "") != "delegate":
            continue
        call_id = str(payload.get("tool_call_id") or "")
        if call_id:
            return call_id
    return ""


def _is_brief_frame(kind: str, payload: dict[str, Any]) -> bool:
    """delegate 任务指令帧判据（只认这一类，普通 user_message 一律不碰）"""
    if kind != "user_message":
        return False
    if payload.get("delegate_brief") or payload.get("delegate_task"):
        return True
    return str(payload.get("content") or "").lstrip().startswith(TASK_INSTRUCTION_OPEN)


def user_frame_display_text(text: str) -> str:
    """Web 用户行的**显示正文**：剥掉 ``<仅模型可见>`` 段（显示层专用）"""
    return strip_llm_only(text)


def is_injected_user_text(text: str) -> bool:
    """内核注入信封判据（写端与读端共用）：正文以注入信封标签开头"""
    return str(text or "").lstrip().startswith(_INJECTED_USER_PREFIXES)


def _is_injected_user_frame(kind: str, payload: dict[str, Any]) -> bool:
    """注入信封 user_message：不冒气泡、不进折叠。"""
    if kind != "user_message":
        return False
    return is_injected_user_text(str(payload.get("content") or ""))


def _trim_fold_text_map(out: dict[str, str] | None, rank: dict[str, int]) -> int:
    """折叠文本映射裁剪：只留最近 ``_FOLD_MAX_CALLS`` 个 call_id（原地修改）"""
    if out is None or len(out) <= _FOLD_MAX_CALLS:
        return 0
    dropped = len(out) - _FOLD_MAX_CALLS
    keep = set(sorted(out, key=lambda k: rank.get(k, 0), reverse=True)[:_FOLD_MAX_CALLS])
    for key in [k for k in out if k not in keep]:
        del out[key]
    return dropped


def _trim_fold_frame_map(out: dict[str, list[dict[str, Any]]] | None) -> int:
    """折叠帧映射裁剪：每个 call_id 只留最近 N 条，call_id 数也封顶（原地修改）。"""
    if out is None:
        return 0
    dropped = 0
    for call_id, frames in list(out.items()):
        if len(frames) > _FOLD_MAX_FRAMES_PER_CALL:
            dropped += len(frames) - _FOLD_MAX_FRAMES_PER_CALL
            out[call_id] = frames[-_FOLD_MAX_FRAMES_PER_CALL:]
    if len(out) <= _FOLD_MAX_CALLS:
        return dropped
    keep = set(sorted(out, key=lambda k: int(out[k][-1].get("view_seq") or 0), reverse=True)[:_FOLD_MAX_CALLS])
    for call_id in [k for k in out if k not in keep]:
        dropped += len(out[call_id])
        del out[call_id]
    return dropped


def _subagent_frame_entry(kind: str, payload: dict[str, Any], view_seq: int) -> dict[str, Any]:
    """折叠区条目的帧形状：与实时 WS 帧一致（端上同一套渲染路径）"""
    if kind == "tool":
        return {
            "type": "tool",
            "text": str(payload.get("text") or ""),
            "ok": bool(payload.get("ok", True)),
            "tool_name": str(payload.get("tool_name") or ""),
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "duration_ms": payload.get("duration_ms"),
            "view_seq": view_seq,
        }
    diff_lines = payload.get("diff")
    if not isinstance(diff_lines, dict):
        diff_lines = payload.get("diff_lines")
    return {
        "type": "diff",
        "display_blocks": payload.get("display_blocks"),
        "diff_lines": diff_lines,
        "tool_name": str(payload.get("tool_name") or ""),
        # 产生它的那次工具调用 id：端上把 diff 精确挂到同 id 的 tool 行之后
        "tool_call_id": str(payload.get("tool_call_id") or ""),
        "view_seq": view_seq,
    }


def read_latest_view_seq(path: Path) -> int:
    """视图文件当前最大 view_seq（尾部快读；无文件/无有效帧返回 0）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - _TAIL_READ_BYTES))
            tail = handle.read()
    except OSError:
        return 0
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
            seq = int(frame.get("view_seq") or 0)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, AttributeError):
            continue  # 窗口首行可能截断：跳过继续向内找
        if seq > 0:
            return seq
    return 0
