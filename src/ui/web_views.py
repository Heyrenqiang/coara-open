"""Web 会话视图存储：web 聊天区的服务端持久化载体（唯一数据源）。

设计定案（2026-08-31）：实时显示（TurnStream 帧流）与刷新恢复读同一份数据——
本模块维护的 per-(subject,session_id) JSONL 视图文件。录像带（session_events）
退为全端冷备/审计，web 聊天区不再读它。

帧格式（每行一帧）::

    {"view_seq":42,"seq":3,"turn_id":"abc","ts":...,"source":"web",
     "subject":"root","session_id":"...","kind":"chunk","payload":{...}}

- ``view_seq`` 全局单调自增（跨进程文件锁 + sidecar 强制高水位；重启经尾部校准），
  是前端 hydrate 对账的唯一数值键；``seq`` 是 TurnStream 回合内帧序号（replay 用）。
- kind：turn_start / user_message / turn_queued / chunk / error / turn_end /
  diff / files / subagent_result。
- 写路径复用 trace_store 的 ``_AsyncFileWriter``（异步批量，不阻塞回合协程）；
  任何落盘失败只记日志，绝不中断回合。

单调与 epoch（2026-09-11 钉死）：

- **同一条线内 ``view_seq`` 永久单调、不重置**：分配时取「jsonl 尾部最大序号」与
  「sidecar 高水位（``<file>.meta.json``）」的较大者 +1。文件被归档/清空/截断都不会
  让序号回退——端上缓存的旧前缀才始终可用（去重、gap 判定成立）。
- **线的身份是 epoch = ``{workspace_dir}::{subject}``**（重建过的线带 ``#世代``）。
  epoch 不变 ⇒ 序号连续可比；epoch 变化 ⇒ 端上必须丢弃本地缓存全量重取。
- 重置序号只有 ``WebViewStore.reset_line`` 一条合法路径（世代 +1 → epoch 变化）。
  任何归档/清理/轮转都不得静默把文件清空让序号从 1 重来。

轮转与历史保护（2026-09-13 钉死）：

- 录像带是内核的对话存档，**只追加、永不删**：没有轮转、没有清理、没有容量上限
  触发的删除。历史帧一旦落盘就是事实。
- 需要腾空间时只允许**改名归档**（同目录另存），不得让序号回退，也不得让端上已
  持有的旧前缀失效。
- 写入唯一入口 ``src/ui/view_recorder.py``（内核录制器，端无关）；新端接入走它，
  不得各造落带路径。
- 自检入口 ``src/ui/view_tape_audit.py``（只读）：序号缺口、未闭合回合、来源端
  分布随时可查。

路径：
- 主会话/模块会话 ``<coara_home>/workspaces/<workspace_id>/web_views/{subject}__{session_id}.jsonl``
- flow 系统带 ``workflow_assets_root(coara_home)/web_views/{subject}__{session_id}.jsonl``
- 线元数据 sidecar ``<view 文件>.meta.json``（世代 + 序号高水位）
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from src.core.coara_home import resolve_coara_home, workspace_id_for
from src.core.logger import logger
from src.core.message_tags import (
    MIDRUN_MSG_OPEN,
    SUBAGENT_MSG_OPEN,
    TASK_INSTRUCTION_OPEN,
    strip_llm_only,
)
from src.core.workspace_layout import UPLOAD_DIR_NAME
from src.ui.trace_store import _AsyncFileWriter, _FileWriteOp

_TAIL_READ_BYTES = 256 * 1024

# 线的元数据 sidecar（``conversation.jsonl.meta.json``）：记这条线的**重建世代**与
# **序号高水位**。两条硬保证：
# - 高水位：jsonl 被归档/清空/截断（尾部读不到序号）时，view_seq 仍从历史最大值续，
#   绝不回退到 1——端上按 view_seq 做的前缀比对、去重与 gap 判定才成立。
# - 世代：它是「线被重建」的唯一合法表达。世代变化 ⇒ epoch 变化 ⇒ 端上丢弃缓存
#   全量重取。除 ``WebViewStore.reset_line`` 外没有任何路径会重置序号。
_META_SUFFIX = ".meta.json"
# sidecar 写盘节流：世代等低频字段不必每帧落盘。序号分配路径会 ``force=True``
# 立刻写下高水位——交棒窗口/双进程靠它避免撞号。
_META_WRITE_MIN_INTERVAL_S = 1.0
_SEQ_LOCK_SUFFIX = ".seq.lock"

# 折叠映射（subagent_results / subagent_briefs / subagent_diffs）的边界：
# 只回游标之后的帧，且 call_id 数与每个 call_id 的帧数都封顶——长线上历史
# 子智能体产出不随会话长度无界回传（每次刷新/切空间/增量补齐都会拉它）。
_FOLD_MAX_CALLS = 30
_FOLD_MAX_FRAMES_PER_CALL = 100

# 历史脏帧消隐：2026-09-11 之前，子智能体最终答复由 web_server 的
# continuation_input_injected 回调合成一条 turn_start + chunk 落带，刷新后它会作为
# assistant 气泡复活（子智能体答复混进主对话）。合成源已删，但既有带里仍留着这些行
# ——读端按形状识别并跳过，只消隐这一种历史产物，不回写、不影响游标。
_LEGACY_SUBAGENT_BUBBLE_RE = re.compile(r"^\[sa-[a-zA-Z0-9_一-鿿]+-[0-9a-fA-F]+\]")

# 内核注入信封（user_message 帧）：子智能体消息 / 途中消息不是用户输入。
# 写端本该不把它们落成 user 帧——但 interact 注入走的是「无端来源的 leftover
# 按收尾端开回合」（continuation_leftover 的空 source 分支），web 收尾时会经
# web_server 的 user_message 落盘。读端按标签前缀兜底消隐，不冒 role=user 气泡；
# 与 brief 帧不同，它们没有可折叠的父工具行，故只跳过、不进任何折叠映射。
_INJECTED_USER_PREFIXES = (SUBAGENT_MSG_OPEN, MIDRUN_MSG_OPEN)


def resolve_view_meta_path(path: Path) -> Path:
    """线元数据 sidecar 路径（``conversation.jsonl`` → ``conversation.jsonl.meta.json``）。"""
    return path.with_name(path.name + _META_SUFFIX)


def resolve_view_seq_lock_path(path: Path) -> Path:
    """Cross-process lock for view_seq allocation on this line."""
    return path.with_name(path.name + _SEQ_LOCK_SUFFIX)


@contextlib.contextmanager
def _view_seq_file_lock(lock_path: Path):
    """Exclusive lock so two kernels cannot mint the same view_seq on one line."""
    import os

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]  # POSIX 专有，Windows 走 msvcrt 分支
        yield
    finally:
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]  # POSIX 专有，Windows 走 msvcrt 分支
        handle.close()


def view_line_epoch(workspace_dir: str | Path | None, subject: str, *, generation: int = 0) -> str:
    """线的身份：``{workspace_dir}::{subject}``（重建过的线带 ``#世代`` 后缀）。

    端上按它判断「还是同一条线吗」——epoch 不变 ⇒ view_seq 连续可比（去重/gap
    判定成立）；epoch 变了 ⇒ 这是新线，本地缓存一律丢弃、按全量重取。
    """
    base = f"{str(workspace_dir or '')}::{subject}"
    return base if generation <= 0 else f"{base}#{generation}"


def resolve_web_view_path(
    workspace_dir: str | Path,
    *,
    coara_home: Path | None,
    subject: str,
    session_id: str,
) -> Path:
    """视图文件路径。

    - 主对话（subject="root"）：一个空间一条线——``web_views/conversation.jsonl``。
      空间内一切（用户输入/内核回复/新会话分隔/模型切换分隔）按时间追加进同一
      文件；``/new`` 只是线上一个分隔标记，不产生新文件。显示即按序回放这条线，
      刷新与实时天然一致（永久连续）。session_id 不再参与命名。
    - 模块/flow 会话：``web_views/{subject}__{session_id}.jsonl``（flow 走系统带），
      仍按会话分文件——它们是独立模块现场，不进主对话这条线。
    """
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

    单调契约（2026-09-11 钉死）：**同一条线内 view_seq 永久单调、不重置**。
    分配序号取「jsonl 尾部最大序号」与「sidecar 高水位」的较大者再 +1，因此文件
    被归档/清空/截断都不会让序号回退；确需从头开始只能走 ``reset_line``，它会把
    世代 +1（epoch 随 ``view_line_epoch`` 变化），端上据此判定「新线，全量重取」。
    """

    def __init__(self) -> None:
        self._writer = _AsyncFileWriter()
        self._seq_lock = threading.Lock()
        # 每文件已分配到的最大 view_seq（首次经尾部 + sidecar 校准）。
        self._seq_cache: dict[Path, int] = {}
        # 每文件元数据内存镜像：{"gen": int, "latest_seq": int, "dirty": bool, "written_ts": float}
        self._line_meta: dict[Path, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 写路径
    # ------------------------------------------------------------------

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
        """分配 view_seq 并异步落一帧，返回分配到的 view_seq（失败返回 0）。

        返回值是实时帧与快照对账的凭据：TurnStream 拿到它写进广播帧，
        端侧才能拿帧上的 view_seq 与 hydrate 的 latest_seq 比对（去重 / gap 检测）。
        序号在同一条线内永久单调（见类 docstring），文件缺失/清空也不回退。
        """
        ts_now = time.time()
        # 分隔线只由显式动作（新会话）落帧；web 端没有任何自动/隐式分隔线。
        # 时间只作为消息自身的属性存在（需要时间感时由消息行自己的时间承担）。
        try:
            view_seq = self._next_view_seq(path)
            frame = {
                "view_seq": view_seq,
                "seq": int(seq),
                "turn_id": turn_id,
                "ts": ts_now,
                "source": source,
                "subject": subject,
                "session_id": session_id,
                "kind": kind,
                "payload": payload or {},
            }
            self._writer.append_jsonl(path, frame)
            return view_seq
        except Exception as exc:  # noqa: BLE001 — 视图存储故障绝不中断回合
            logger.warning(f"web view persist failed (path={path}, kind={kind}): {exc}")
            return 0

    def make_persist(self, workspace_dir: str | Path, *, coara_home: Path | None) -> Any:
        """构造注入 TurnStream 的落盘回调：帧 dict（_record 已补 seq/turn_id/source/subject）→ 视图文件。

        返回分配到的 view_seq；不落带的帧（子智能体过程帧）与落盘失败返回 None
        ——该类帧没有可对账的序号，端侧按「丢弃 + 记日志」处理。
        """

        def persist(frame: dict[str, Any]) -> int | None:
            try:
                session_id = str(frame.get("session_id") or "")
                subject = str(frame.get("subject") or "root")
                if not session_id:
                    return None
                path = resolve_web_view_path(
                    workspace_dir, coara_home=coara_home, subject=subject, session_id=session_id
                )
                kind = str(frame.get("type") or "")
                if kind == "subagent_chunk":
                    # 子智能体的中间产出是过程信息：只做实时投递，不进视图带
                    # （落了带，hydrate 就会把它当主会话正文复现出来）。
                    # 最终答复（subagent_result）落带，但 build_messages 不把它投影
                    # 成消息——只经快照的 subagent_results 映射回到 delegate 折叠里。
                    return None
                if kind == "diff":
                    # 落盘统一 payload={"diff": canonical diff_lines}（build_messages
                    # 唯一读取键）。广播帧走顶层字段（display_blocks/diff_lines），
                    # 落盘与广播分离——这里负责映射。
                    diff_lines = frame.get("diff_lines")
                    payload: dict[str, Any] = {"diff": diff_lines} if isinstance(diff_lines, dict) else {}
                    # 产生它的那次工具调用 id（与同工具 tool 行同值）：端上至此
                    # 不靠相邻关系猜位置。主会话 diff 也一并落（同一帧字段）。
                    tool_call_id = str(frame.get("tool_call_id") or "")
                    if tool_call_id:
                        payload["tool_call_id"] = tool_call_id
                    # 子智能体产生的 diff：另存父标识与折叠区渲染材料——build_messages
                    # 不把它投影成主流消息，而是归集进 subagent_diffs[父 call_id]。
                    parent_tool_call_id = str(frame.get("parent_tool_call_id") or "")
                    if parent_tool_call_id:
                        payload["parent_tool_call_id"] = parent_tool_call_id
                        payload["display_blocks"] = frame.get("display_blocks")
                        payload["tool_name"] = str(frame.get("tool_name") or "")
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

        高水位取「jsonl 尾部最大序号」与「sidecar 记录高水位」的较大者：正常情形
        文件是权威（进程重启/外部追加都能续上）；文件被归档/清空/截断、尾部读不到
        序号时，sidecar 接住继续递增。真要回到 1 只能走 ``reset_line``（世代 +1 →
        epoch 变化，端上全量重取）。

        分配在跨进程文件锁内完成，并同步落盘 sidecar 高水位——交棒窗口旧实例
        与新实例不得各自从同一水位各取 +1 撞号（跨进程一致性以 sidecar 为准）。
        """
        with self._seq_lock, _view_seq_file_lock(resolve_view_seq_lock_path(path)):
            # 他进程可能刚推进过高水位：丢掉内存镜像里的 latest_seq，从盘重读
            # （世代 gen 除外——见下方 merge 说明）。
            stale = self._line_meta.pop(path, None)
            meta = self._load_line_meta(path)
            if stale is not None:
                if stale.get("reset_baseline"):
                    # reset_line 的 latest_seq=0 是重估基准：不接回（接回会挡住
                    # jsonl 旧帧水位的重估）；gen 已随 reset_line 同步落盘。
                    pass
                elif bool(stale["write_pending"]):
                    # 已投递 worker 尚未执行：该写晚于本次盘读快照，执行后盘上
                    # 高水位 ≥ stale 值——接回防撞号。已执行完的盘上已读到，
                    # 未投递的（dirty）当前不可能存在（序号路径同步落盘）。
                    meta["latest_seq"] = max(int(meta["latest_seq"]), int(stale["latest_seq"] or 0))
            disk_hi = max(read_latest_view_seq(path), int(meta["latest_seq"] or 0))
            cached = self._seq_cache.get(path)
            base = disk_hi if cached is None else max(int(cached), disk_hi)
            nxt = base + 1
            self._seq_cache[path] = nxt
            meta["latest_seq"] = nxt
            meta["dirty"] = True
            payload = {"gen": int(meta["gen"]), "latest_seq": nxt, "updated_ts": time.time()}
            self._write_line_meta_sync(path, meta, payload)
            return nxt

    # ------------------------------------------------------------------
    # 线身份（世代）与元数据
    # ------------------------------------------------------------------

    def _write_line_meta_sync(self, path: Path, meta: dict[str, Any], payload: dict[str, Any]) -> None:
        """在调用线程内原子落盘 sidecar 并同步镜像（失败只记日志）。

        序号分配与 reset 路径用：跨进程文件锁内依赖 sidecar 真实落盘，不走
        worker 队列。其余调用方（flush 等后台路径）应投递到 worker 线程
        （见 _persist_line_meta）。
        """
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
        """读该线的元数据（内存镜像优先；sidecar 缺失/损坏按新线处理）。

        调用方须持 ``_seq_lock``。已弹出的旧镜像是否接回由调用方决定
        （见 _next_view_seq 的 dirty 门）。
        """
        meta = self._line_meta.get(path)
        if meta is not None:
            return meta
        data: dict[str, Any] = {}
        try:
            parsed = json.loads(resolve_view_meta_path(path).read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        meta = {
            "gen": int(data.get("gen") or 0),
            "latest_seq": int(data.get("latest_seq") or 0),
            "dirty": False,
            # sidecar 之前是否存在：只读路径（快照 epoch 读世代）会加载元数据，
            # 但绝不能因为 flush/close 就往别的空间的目录里凭空写一个 sidecar。
            "existing": bool(data),
            "written_ts": 0.0,
            # sidecar 写已异步化（worker 线程）：序号分配路径投递后不等待落盘，
            # 中途退出会丢最后几帧高水位。重启/交棒读路径因此按「高水位可能滞后」
            # 兜底——从盘重读，与 jsonl 尾部取 max，序号单调不回退。
            "write_pending": False,
        }
        self._line_meta[path] = meta
        return meta

    def _persist_line_meta(self, path: Path, *, force: bool = False) -> None:
        """投递 sidecar 落盘（节流；失败只记日志——元数据缺一帧不影响落帧）。

        写盘移到 worker 线程（``_AsyncFileWriter.enqueue``）——同目录临时文件 + 双
        fsync 的原子写在网络盘/机械盘上会拖慢流式输出所在的回合协程。重启校准窗
        口：``read_latest_view_seq`` 读的是 jsonl 尾部（权威），sidecar 只是文件读
        不到序号时的兜底高水位；旧实现里 sidecar 本就可能滞后 jsonl 至多
        ``_META_WRITE_MIN_INTERVAL_S``（1s，节流），异步投递只把这个既存窗口再放宽
        约一两帧——单调取 max 的校准语义不变，台账认可为无害。线程内保序：写按投递
        顺序执行，后投的更新值不会先落盘。
        注：序号分配（_next_view_seq）与 reset_line 的 sidecar 写是同步的（跨进程
        锁内要求真实落盘），本函数只服务非关键路径的兜底写。
        """
        meta = self._line_meta.get(path)
        if meta is None:
            return
        if not meta["dirty"] and (not force or not meta["existing"]):
            return
        if meta.get("reset_baseline"):
            # reset_line 的 latest_seq=0 是重估基准而非高水位：永不经本函数落盘
            # （会盖住 FIFO 里已投递的更高水位）；gen 推进由 reset_line 同步落盘。
            return
        now = time.time()
        if not force and now - float(meta["written_ts"] or 0.0) < _META_WRITE_MIN_INTERVAL_S:
            return
        payload = {"gen": int(meta["gen"]), "latest_seq": int(meta["latest_seq"]), "updated_ts": now}
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
        """等待 worker 执行完此刻之前已投递的全部 op（sentinel 屏障）。

        不等待之后新投递的写；若 worker 此前故障堆积，屏障照样排在它们后面，
        超时只记日志不升级（与落盘失败语义一致）。
        """
        done = threading.Event()
        self._writer.enqueue(_FileWriteOp(path=Path(), text="", append=False, done=done))
        if not done.wait(timeout=timeout):
            logger.debug("web view writer barrier wait timeout")

    def line_generation(self, path: Path) -> int:
        """该线当前世代（0 = 从未重建）。快照的 epoch 由它参与构造。"""
        with self._seq_lock:
            return int(self._load_line_meta(path)["gen"])

    def reset_line(self, path: Path) -> int:
        """重建这条线：世代 +1（epoch 随之变化）、序号基准归零重估。

        **这是声明「线已重建」的唯一入口。** 序号本身不会因此回退——重估取「文件
        尾部最大序号」与「sidecar 高水位」的较大者（见类 docstring 单调契约），
        文件还在就继续往上走，只有文件确已清空才从 1 起。本仓现有归档/清理路径都
        不重建视图线（归档＝保留原文件，序号自然保留），故当前没有调用方；将来新增
        此类路径必须走这里，绝不允许把文件清空后让序号静默从 1 重来——端上会把它当
        同一条线的旧序号前缀，去重与 gap 判定全部失效。

        sidecar 写异步化后的处理：latest_seq=0 是重估基准不是高水位，不落盘；
        gen 推进直接由本函数单独投递（值取「盘上已有高水位」，FIFO 保序不会被
        覆盖成 0），并等待落盘——重启读到的 epoch 必须立即反映新世代。
        """
        with self._seq_lock:
            meta = self._load_line_meta(path)
            meta["gen"] = int(meta["gen"]) + 1
            meta["latest_seq"] = 0
            meta["dirty"] = True
            meta["written_ts"] = 0.0
            meta["reset_baseline"] = True
            self._seq_cache.pop(path, None)
            # 等此前在飞的写落盘，取盘上真实高水位写「gen 推进」帧；0 永不出盘。
            self._wait_writer_turn()
            disk_hi = 0
            try:
                parsed = json.loads(resolve_view_meta_path(path).read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    disk_hi = int(parsed.get("latest_seq") or 0)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                disk_hi = 0
            payload = {"gen": int(meta["gen"]), "latest_seq": disk_hi, "updated_ts": time.time()}
            self._write_line_meta_sync(path, meta, payload)
            logger.info(f"web view line reset (gen={meta['gen']}, path={path})")
            return int(meta["gen"])

    # ------------------------------------------------------------------
    # 生命周期（flush/close 收口：等待已投递的 meta 写落盘）
    # ------------------------------------------------------------------

    def _wait_meta_writes(self, timeout: float = 2.0) -> None:
        """等待已投递的 sidecar 写全部落盘（flush/close 收口用，尽力而为）。

        ``_AsyncFileWriter`` 按 worker 排空即置 drained，此时刚投的 op 可能还在
        队列里——shutdown 收口若只看 drained 会丢掉最后几帧高水位。这里等待所有
        镜像 settled（dirty=False 且 write_pending=False），再确认一次 drained。
        write_pending 由 worker 线程在落盘后清除；观察侧不持 ``_seq_lock`` 等待，
        避免「writer 已 closed → 提交协程在持锁 inline 执行 callback → 等待协程
        持锁观察」的死锁。worker 队列 FIFO 保序，write_pending 只增置信度；镜像已
        被 pop 而 callback 尚未执行完的瞬间视作已投到 FIFO，drained 兜底落盘。
        deadline 内未 settled 不升级——失败语义与落盘故障一致（只记日志）。
        """
        deadline = time.monotonic() + timeout
        settled_once = False
        while time.monotonic() < deadline:
            settled = all(not bool(m["dirty"]) and not bool(m.get("write_pending")) for m in self._line_meta.values())
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

    # ------------------------------------------------------------------
    # 读路径（常态 hydrate）
    # ------------------------------------------------------------------

    @staticmethod
    def iter_frames(path: Path) -> list[dict[str, Any]]:
        """按落盘顺序读全部可解析帧（坏行跳过）。文件不存在返回空。"""
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

    @classmethod
    def build_messages(
        cls,
        path: Path,
        *,
        limit: int,
        merge_chunks: bool = False,
        extra_paths: list[Path] | None = None,
        since_seq: int = 0,
        before_seq: int = 0,
        source_filter: str | None = None,
        subagent_out: dict[str, str] | None = None,
        subagent_diffs_out: dict[str, list[dict[str, Any]]] | None = None,
        subagent_briefs_out: dict[str, str] | None = None,
        fold_truncated_out: dict[str, int] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """从视图帧聚合聊天行：(messages, total, latest_view_seq)。

        - user_message → user 行（含 source=web 主会话行；它端来源的回合
          帧本就不写 web 视图——attach 端不挂 persist）。
        - ``source_filter`` 非空时（仅 root 线用）：只聚合 ``source`` 等于它的帧；
          缺 ``source`` 字段的历史帧按放行处理（老数据没标来源，按本端算）。
          他端帧（matrix/cli 落带）仍是线上事实——``latest_seq`` 照常覆盖它们，
          只是不投影成 web 聊天区的行。显示与录制分离：录制全端统一进带，
          显示每个端只看自己的。
        - chunk → assistant 行。主会话（merge_chunks=False）每条 chunk 帧
          独立一行，与实时渲染（store 每条 chunk 帧独立气泡）逐帧一致；
          模块会话（merge_chunks=True）保持同回合合并（其实时渲染为追加
          同一气泡）。
        - diff / files 帧 → 独立 assistant 行（diff 块 / 附件卡）。
        - error 帧 → assistant 行（错误样式由前端按 role/kind 承担，不加标识符前缀）。
        - subagent_result 帧 → **不投影成消息**（子智能体最终答复不属于主会话
          正文，落了气泡就是刷新后凭空多一条回复）。传 ``subagent_out`` 时，
          该帧按 ``{tool_call_id: text}`` 收进这张映射，由快照响应带给端上，
          端上并进子智能体折叠输出（刷新后展开 delegate 工具行仍能看到）。
        - 带 ``parent_tool_call_id`` 的 tool / diff 帧（子智能体自己的工具行与
          改动）→ **不投影成消息**，传 ``subagent_diffs_out`` 时按
          ``{父 call_id: [帧…]}`` 归集，端上渲染在那条 delegate 工具行的展开区。
          没有该字段的老帧照原样投影（历史兼容，不改写旧数据）。
        - delegate 任务指令帧（``delegate_brief`` 标记，或老数据的
          ``delegate_task`` 标记 / 正文以 ``<任务指令>`` 开头）→ **不投影成消息**
          （否则刷新后冒出一条 role=user 气泡），传 ``subagent_briefs_out`` 时按
          ``{父 call_id: 指令全文}`` 归集，端上折进那条 delegate 工具行。
          老帧缺 ``parent_tool_call_id`` 时按所在回合反查：同 ``turn_id`` 内
          最近的 delegate 工具帧（含该行本身被判为指令跟话时的下一条）认领；
          反查不到保持现状（归空 key，端上忽略、不冒气泡）。
        - 内核注入信封（``<子智能体消息>`` / ``<途中消息>`` 开头，见
          ``_is_injected_user_frame``）→ **不投影成消息**，也不进折叠映射
          （它们没有对应的父工具行），纯消隐。

        三张折叠映射都只收 ``view_seq > since_seq`` 的帧（增量刷新不重传旧内容），
        且按 ``_FOLD_MAX_CALLS`` / ``_FOLD_MAX_FRAMES_PER_CALL`` 封顶——长线上
        历史子智能体不随会话长度无界回传。传 ``fold_truncated_out`` 时，封顶
        实际裁掉的条数写进该 dict（键 results/diffs/briefs，值为条数），由快照
        响应带给端上作显式提示（封顶是边界不是静默丢失）。

        「上次回合中断」不在此猜测：进程活着响应 hydrate 时，无 turn_end 的
        最新回合是进行中而非崩溃。崩溃恢复由启动路径读 in-flight 标记注入
        注记承载（workspace_state.INTERRUPTED_SESSION_NOTE）。

        ``extra_paths``：附加的视图文件（同空间历史 session 的 ``root__*.jsonl``）。
        空间一条线把「该端在这个空间展示过的全部消息」聚成一条连续流——各文件
        的帧按 ``(ts, view_seq)`` 全局排序后聚合。文件内 ``ts`` 单调（落盘顺序），
        且不同 session 不同时刻活跃、无并发写，故 ts 排序与真实时间线一致。
        """
        frames = cls.iter_frames(path)
        if extra_paths:
            for p in extra_paths:
                if p != path:
                    frames.extend(cls.iter_frames(p))
            frames.sort(key=lambda f: (float(f.get("ts") or 0.0), int(f.get("view_seq") or 0)))
        latest_seq = 0
        for frame in frames:
            try:
                latest_seq = max(latest_seq, int(frame.get("view_seq") or 0))
            except (TypeError, ValueError):
                continue

        turns: dict[str, dict[str, Any]] = {}
        order: list[dict[str, Any]] = []
        # 折叠映射的排名（{call_id: 最新 view_seq}）：只服务末尾裁剪，不对外暴露
        # ——对外形状仍是 {call_id: text} / {call_id: [帧…]}。
        result_rank: dict[str, int] = {}
        brief_rank: dict[str, int] = {}
        # brief 反查缓存：帧按时间线单遍扫描，「(session_id, turn_id) → 最近的
        # delegate 工具行 call_id」。delegate 行与它的指令跟话同回合时，行先落带
        # （指令帧晚一拍）；行晚于指令帧（历史数据落盘次序不齐）时靠预扫兜底认
        # 下一行。键带 session_id：主对话一个空间一条线（多 session 共线），
        # turn_id 跨 session 可能撞名，不隔离会串认别会话的发起行。
        delegate_calls_by_turn: dict[tuple[str, str], str] = {}
        # 分隔标记帧（新会话/模型切换）：无 turn_id，直接按落盘顺序占一行，
        # 渲染成时间线分隔线（role=assistant + divider 字段，前端识别成分隔线）。
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
            # 主会话 delegate 工具行登记到所在 turn：老指令帧缺父行标识时
            # 按它反查归集（单遍按时间线推进，最新一行胜出——同行内多次
            # delegate 时指令认最近那条发起行）。
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
                # 子智能体最终答复：不进消息流（否则刷新后复活成 assistant 气泡），
                # 只收进 {tool_call_id: text} 供端上并进折叠输出。帧无 turn_id
                # （回合已收尾时落带）也要收，故在此处先于回合归属判定处理。
                if subagent_out is not None and view_seq > since_seq:
                    tc_id = str(payload.get("tool_call_id") or "")
                    text = str(payload.get("text") or "")
                    if tc_id and text.strip():
                        subagent_out[tc_id] = text
                        result_rank[tc_id] = view_seq
                continue
            if _is_brief_frame(kind, payload):
                # delegate 任务指令：不是主会话正文（否则刷新后冒出 role=user 气泡），
                # 归集进 subagent_briefs[父行 call_id] 供端上折进那条 delegate 工具行。
                # 老帧没有标记：文本判据（以 <任务指令> 开头）与历史 delegate_task
                # 标记都算——读端识别，不改写用户数据。
                if subagent_briefs_out is not None and view_seq > since_seq:
                    call_id = str(payload.get("parent_tool_call_id") or "")
                    text = str(payload.get("content") or "")
                    if not call_id:
                        # 老帧缺父行标识：按所在回合反查 delegate 工具行认领——
                        # 先看这帧之前已登记的（工具行先落带的常态），没有就
                        # 预扫本回合之后最近的 delegate 行（落盘次序不齐的历史）。
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
                # 子智能体自己的工具行 / 改动：不进主会话正文流，按父 call_id
                # 归集给端上的 delegate 折叠区（同样先于回合归属判定处理）。
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
                # 同回合多条跟话：按 view_seq 保留每一条，禁止覆盖成单槽；
                # 附件随用户消息落盘，回放时重建气泡缩略图/文件卡片。
                atts = payload.get("attachments")
                turn["users"].append(
                    (
                        view_seq,
                        str(payload.get("content") or ""),
                        atts if isinstance(atts, list) and atts else None,
                        bool(payload.get("delegate_task")),  # delegate 指令跟话标记
                        str(payload.get("client_msg_id") or ""),  # 端上标识（认领精确键）
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
                # 落盘统一格式 payload={"diff": canonical diff_lines}（主回合/
                # 唤醒/跟话三路一致，均含 diff 键）。旧的不一致帧不兼容——破相就破相。
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
                # 工具行（✓ tool(...)）：聊天流内联一行，插在正文段落之间——
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
        # 「上次回合中断」由启动恢复路径的 in-flight 注记承载（INTERRUPTED_SESSION_NOTE），
        # 不在此处猜测——进程活着响应 hydrate 时，无 turn_end 的最新回合是进行中而非崩溃。
        for turn in order:
            # 用户行与助手产出按 view_seq 交织：前段输出 → 跟话 → 后段输出
            # 孤儿 delegate 指令 turn（历史他端 delegate 泄漏帧——只有伪造的
            # user_message、无任何正文落盘）不渲染：无正文说明该回合正文根本
            # 不在这份 web 视图里（attach/matrix 回合不写 web），仅剩的指令
            # 气泡是假 web 输入。有正文的 turn 保留 delegate 指令行（web 发起
            # delegate 的刷新回放需要它重建气泡）。
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
                        # 端上标识随行回落：刷新后的 hydrate 对账按它精确配对本端
                        # 那条实时气泡（文本可能被服务端改写、序号那时可能还没轮到）
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

        # 分隔标记帧按 view_seq 归位到消息序列的对应时间点（新会话/模型切换
        # 分隔线出现在它发生的位置，而非堆在末尾）。
        if dividers:
            combined: list[tuple[int, dict[str, Any]]] = [(int(m.get("seq") or 0), m) for m in messages]
            combined.extend(dividers)
            combined.sort(key=lambda x: x[0])
            messages = [m for _, m in combined]

        # 聚合多文件时各文件 view_seq 互相冲突（各自从 1 起编），直接拿它做前端
        # seq 对账会乱（切页/刷新跳变的根因）。重编一条严格单调的全局序号——
        # 消息此刻已按真实时间线排好序，按位置赋 1..N 即可。单文件时序号本就
        # 单调，重编不改变相对关系，行为等价。
        if extra_paths:
            for idx, m in enumerate(messages, start=1):
                m["seq"] = idx

        # 上传附件（ref 落盘）在出口补 inline 直出 URL，前端气泡直接渲染。
        for m in messages:
            atts = m.get("attachments")
            if not isinstance(atts, list):
                continue
            for att in atts:
                if isinstance(att, dict) and not att.get("url") and att.get("ref"):
                    att["url"] = f"/api/workspace/file-raw?path={UPLOAD_DIR_NAME}/{att['ref']}"

        # 多文件聚合时 seq 是时间线位置序号（各文件 view_seq 会互相冲突）：
        # 游标 = 末条位置 = 全量行数。必须在增量过滤之前算——过滤后尾部可能为空，
        # 那时拿不到位置号，游标会回退成某个文件的原始 view_seq，后续增量全乱。
        if extra_paths and messages:
            latest_seq = int(messages[-1].get("seq") or 0)

        # 增量 hydrate：只回游标之后的帧。端侧把游标之前的部分原样保留，刷新因此
        # 只做「补后缀」而不是整表重建——缓存视图与权威视图只是同一条线的两个长度。
        # total 恒为「这条线的全量行数」：增量只影响返回的切片，不改这个量纲，
        # 否则同一个字段在带游标与不带游标时是两种含义。
        total = len(messages)
        if since_seq > 0:
            messages = [m for m in messages if int(m.get("seq") or 0) > since_seq]
        # 向前翻页：只回 before_seq 之前的帧，limit 取该窗口的最近 N 条——
        # 端侧 prepend 到头部，与尾部增量（since_seq）互为镜像，互不相交。
        if before_seq > 0:
            messages = [m for m in messages if int(m.get("seq") or 0) < before_seq]
        if len(messages) > limit:
            messages = messages[-limit:]
        # 游标同源：latest_seq 只覆盖**实际返回切片**的最后一帧——截断前的全量游标
        # 会让端侧把被裁掉的帧当成「已送达」永不再补，屏幕历史出现永久静默空洞
        # （被裁帧 ≤ 旧游标、不触发 gap 检测）。单文件与多文件（重编位置序号）通用。
        if messages:
            latest_seq = int(messages[-1].get("seq") or 0)
        # 折叠映射在出口处裁剪：只留最近若干 call_id / 每个 call_id 最近若干帧。
        # 不做这一步，长线的子智能体产出会随会话长度无界回传（每次刷新/切空间全量）。
        # 裁剪计数随 fold_truncated_out 透出：快照据此带 subagent_truncated 显式
        # 标记——封顶是边界，不是静默丢弃。
        dropped_results = _trim_fold_text_map(subagent_out, result_rank)
        dropped_briefs = _trim_fold_text_map(subagent_briefs_out, brief_rank)
        dropped_frames = _trim_fold_frame_map(subagent_diffs_out)
        if fold_truncated_out is not None:
            fold_truncated_out["results"] = dropped_results
            fold_truncated_out["diffs"] = dropped_frames
            fold_truncated_out["briefs"] = dropped_briefs
        return messages, total, latest_seq


def _next_delegate_call_id(frames: list[dict[str, Any]], current: dict[str, Any], session_id: str, turn_id: str) -> str:
    """brief 反查兜底：同 session 同 turn_id 内当前帧之后最近的 delegate 工具行 call_id。

    仅用于老指令帧缺 ``parent_tool_call_id`` 且该帧之前没有登记过 delegate 行
    的场景（历史数据落盘次序不齐）；找不到返回空串（保持现状：归空 key，
    端上忽略、不冒气泡）。主对话多 session 共线，反查必须限同 session。
    """
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
    """delegate 任务指令帧判据（只认这一类，普通 user_message 一律不碰）。

    - 新数据：``delegate_brief`` 标记（base.process_message 的实时 trace 与
      delegate._persist_delegate_task_view 的落盘帧都带）。
    - 老数据：``delegate_task`` 标记（同一条落盘路径的旧标记），或正文以
      ``<任务指令>`` 开头（读端兜底判据，不改写用户数据）。

    与另外两处消隐判据的关系（读端兜底共三处，刻意不合成一个函数）：本判据只认
    ``user_message`` 且**有折叠去处**（折进 delegate 工具行）；``_is_injected_user_frame``
    同样只认 ``user_message`` 但**无折叠去处**（纯跳过）；``_LEGACY_SUBAGENT_BUBBLE_RE``
    作用于 ``chunk`` 帧的历史合成气泡。三者共用的只有标签真源
    （``src.core.message_tags``）——合成一处会把「帧类型 × 处置方式」两维压成一维。
    """
    if kind != "user_message":
        return False
    if payload.get("delegate_brief") or payload.get("delegate_task"):
        return True
    return str(payload.get("content") or "").lstrip().startswith(TASK_INSTRUCTION_OPEN)


def user_frame_display_text(text: str) -> str:
    """Web 用户行的**显示正文**：剥掉 ``<仅模型可见>`` 段（显示层专用）。

    内核注入信封（如 ``<后台结果>`` 完成通知）被当作新回合输入开回合时，正文会
    经本模块的 ``user_message`` 帧上屏并落视图带。信封里的行为指令只给模型看，
    显示前必须剥净——否则用户在自己的气泡里读到「非必要不准读此日志」这类内部
    约束。调用方只能拿它当显示/落带内容，**绝不能**回写 ``message_history``。
    """
    return strip_llm_only(text)


def _is_injected_user_frame(kind: str, payload: dict[str, Any]) -> bool:
    """内核注入信封帧判据：以 ``<子智能体消息>``/``<途中消息>`` 开头的 user_message。

    这两类文本是内核给 LLM 看的信封（`interact` 推给主会话、主会话推给子智能体），
    从来不是用户敲进来的输入。写端门控挡不住全部路径（见 ``_INJECTED_USER_PREFIXES``
    注释），读端据此兜底：既不冒 role=user 气泡，也不进任何折叠映射。
    """
    if kind != "user_message":
        return False
    return str(payload.get("content") or "").lstrip().startswith(_INJECTED_USER_PREFIXES)


def _trim_fold_text_map(out: dict[str, str] | None, rank: dict[str, int]) -> int:
    """折叠文本映射裁剪：只留最近 ``_FOLD_MAX_CALLS`` 个 call_id（原地修改）。

    返回被裁掉的 call_id 数（0 = 无裁剪）——封顶是行为边界不是静默丢弃，
    调用方据此给快照带 ``subagent_truncated`` 显式标记。
    """
    if out is None or len(out) <= _FOLD_MAX_CALLS:
        return 0
    dropped = len(out) - _FOLD_MAX_CALLS
    keep = set(sorted(out, key=lambda k: rank.get(k, 0), reverse=True)[:_FOLD_MAX_CALLS])
    for key in [k for k in out if k not in keep]:
        del out[key]
    return dropped


def _trim_fold_frame_map(out: dict[str, list[dict[str, Any]]] | None) -> int:
    """折叠帧映射裁剪：每个 call_id 只留最近 N 条，call_id 数也封顶（原地修改）。

    返回被裁掉的帧总数（0 = 无裁剪），用途同 ``_trim_fold_text_map``。
    """
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
    """折叠区条目的帧形状：与实时 WS 帧一致（端上同一套渲染路径）。

    - tool → ``{type:"tool", text, ok, tool_name, tool_call_id, duration_ms}``
    - diff → ``{type:"diff", display_blocks, diff_lines, tool_name}``
    两者都带 ``view_seq``：刷新回放与实时帧按同一序号去重。
    """
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
