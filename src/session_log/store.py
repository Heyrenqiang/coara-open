"""Session event log storage — append-only JSONL 读写（分段轮转）。

文件按工作空间一个（``<coara_home>/workspaces/<id>/session_events.jsonl``），
会话靠事件内 ``session_id`` 字段区分。写线程锁保护；读是只读迭代。

分段轮转：active 写前超过 ``_ROTATE_THRESHOLD_BYTES`` 即重命名为
``<stem>.<YYYYmmdd-HHMMSS>.jsonl`` 归档段（保留全部不删除）再建新 active；
读路径按文件名序先 yield 归档段再 yield active，消费方无感。
轮转失败（如 Windows 读方句柄未关）只记 warning 跳过，本次照常追加。

单进程假设：seq 分配点（``_seq_cache``）是进程级的，两个 coara 进程同写
一个文件会产生重复 seq（引擎进程因此写独立的 engine 录像带）。
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.coara_home import resolve_coara_home, workspace_id_for

_LOG_FILE_NAME = "session_events.jsonl"
_JSONL_SUFFIX = ".jsonl"
# active 超过该阈值即轮转（写前锁内检查）
_ROTATE_THRESHOLD_BYTES = 32 * 1024 * 1024
# last_seq 快速路径只读文件尾部窗口
_TAIL_READ_BYTES = 256 * 1024

_write_lock = threading.Lock()
# 进程内每文件已分配到的最大 seq：同进程多 recorder / 归档共用一个分配点，
# 避免各自缓存 seq 造成的重复（首次未命中时经 last_seq 尾部快路径校准；
# 轮转不清缓存——seq 空间跨段连续）。
_seq_cache: dict[Path, int] = {}
# 写失败去抖：按文件只 warning 一次（成功后复位），避免刷屏；后续失败仍静默丢
_write_failure_logged: set[Path] = set()
# 轮转失败去抖：每文件只 warning 一次（成功后复位），失败不丢事件下次写再试
_rotate_failure_logged: set[Path] = set()
# 冷压缩在飞守卫：同一 active 带的归档清扫只跑一个线程
_compress_inflight: set[Path] = set()
_compress_lock = threading.Lock()


def resolve_session_log_path(
    workspace_dir: str | Path,
    *,
    coara_home: Path | None = None,
) -> Path:
    """``<coara_home>/workspaces/<workspace_id>/session_events.jsonl``。"""
    workspace_path = Path(workspace_dir).expanduser().resolve()
    home = resolve_coara_home(workspace_path, coara_home)
    workspace_id = workspace_id_for(workspace_path)
    path = home / "workspaces" / workspace_id / _LOG_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def workflow_session_log_path(
    kind: str = "flow",
    *,
    coara_home: Path | None = None,
) -> Path:
    """工作流主体的系统级录像带（与工作空间无关）。

    - ``flow``：构建对话（FlowRoot）+ 会话内 flow 节点共用一条
      ``session_events.jsonl``（同进程，写锁安全，agent_kind 区分）；
    - ``engine``：引擎进程节点写独立的 ``engine_session_events.jsonl``
      （跨进程不共享 seq 分配点，避免写冲突）。
    """
    from src.workflow.paths import workflow_assets_root

    name = _LOG_FILE_NAME if kind == "flow" else "engine_session_events.jsonl"
    path = workflow_assets_root(coara_home=coara_home) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_tail_newline(handle: Any, path: Path) -> None:
    """追加写前隔离崩溃半行：文件磁盘尾字节不是换行时先补一个换行。

    进程在上次 write 中途被杀（断电/强杀）会留下无换行半行残骸。读路径虽
    容忍半行，但若新事件直接续写在残骸尾部，整行无法解析被静默跳过——
    事件永久丢失且 seq 出现幽灵缺口。追加前把残骸与正式事件用换行隔开：
    残骸行仍是坏行被读路径跳过（隔离救灾现场），新事件完整可投影。

    用独立二进制只读句柄读尾字节（可靠定位），append 文本句柄只负责补写——
    Windows 文本模式 ``open("a")`` 的 tell/seek 报缓冲位置，读不到真实尾字节。
    空文件零成本跳过。
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return
        with path.open("rb") as probe:
            probe.seek(-1, 2)
            tail = probe.read(1)
        if tail != b"\n":
            handle.write("\n")
    except OSError:
        # 检查失败不阻断追加：宁可冒险粘连也不丢本批事件
        return


def append_events(path: Path, events: list[dict[str, Any]]) -> None:
    """追加一批事件（线程安全）。文件不存在时创建。失败仅记日志，不抛给调用方。"""
    if not events:
        return
    try:
        lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
        with _write_lock:
            _maybe_rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                _ensure_tail_newline(handle, path)
                handle.write(lines)
            _advance_seq_cache(path, events)
    except OSError:
        _log_write_failure(path, len(events))


def _stem_of(path: Path) -> str:
    """``session_events.jsonl`` → ``session_events``（归档段名前缀）。"""
    name = path.name
    return name[: -len(_JSONL_SUFFIX)] if name.endswith(_JSONL_SUFFIX) else name


def _archive_segments(path: Path) -> list[Path]:
    """path 对应的归档段（按文件名升序 = 时间序）。

    只匹配 ``<stem>.<YYYYmmdd-HHMMSS>.jsonl[.gz]`` 时间戳段：active 自身与
    .bak 等非时间戳文件不会混入；.gz 是冷压缩段，读路径透明解压。
    """
    pattern = re.compile(rf"^{re.escape(_stem_of(path))}\.20\d{{6}}-\d{{6}}{re.escape(_JSONL_SUFFIX)}(?:\.gz)?$")
    try:
        names = [n for n in os.listdir(path.parent) if pattern.match(n)]
    except OSError:
        return []
    return [path.parent / n for n in sorted(names)]


def _maybe_rotate(path: Path) -> None:
    """active 超过阈值时轮转为时间戳归档段（调用方须持 _write_lock）。

    失败（如 Windows 上读方句柄未关致 rename 被拒）只记 warning 跳过，
    本次照常追加、下次写再试——绝不因轮转丢事件或抛给调用方。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return  # 文件不存在/不可达：不轮转，追加路径自行容错
    if size < _ROTATE_THRESHOLD_BYTES:
        return
    archive = path.with_name(f"{_stem_of(path)}.{datetime.now():%Y%m%d-%H%M%S}{_JSONL_SUFFIX}")
    if archive.exists() or archive.with_name(archive.name + ".gz").exists():
        return  # 同秒已轮转过（含已冷压缩的同名段）：跳过，下次写再试
    try:
        path.rename(archive)
        _rotate_failure_logged.discard(path)
        _compress_archives_async(path)
    except OSError as exc:
        if path not in _rotate_failure_logged:
            _rotate_failure_logged.add(path)
            from src.core.logger import logger

            logger.warning("session event log rotate skipped, will retry on next write (path=%s): %s", path, exc)


def _compress_archives(path: Path) -> None:
    """gzip 全部未压缩归档段（含历史遗留段）；失败保留原段只记 warning。

    归档段不可变，压缩是安全的离线动作。同步函数，供后台线程与测试共用。
    """
    for segment in _archive_segments(path):
        if segment.name.endswith(".gz"):
            continue
        tmp = segment.with_name(segment.name + ".gz.tmp")
        try:
            with segment.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            tmp.replace(segment.with_name(segment.name + ".gz"))
            segment.unlink()
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            from src.core.logger import logger

            logger.warning("session event log archive compress skipped (%s): %s", segment, exc)


def _compress_archives_async(path: Path) -> None:
    """轮转成功后后台跑冷压缩：同一条带单飞，daemon 线程不拖进程退出。"""
    with _compress_lock:
        if path in _compress_inflight:
            return
        _compress_inflight.add(path)

    def _sweep() -> None:
        try:
            _compress_archives(path)
        finally:
            with _compress_lock:
                _compress_inflight.discard(path)

    threading.Thread(target=_sweep, name=f"tape-gzip-{_stem_of(path)}", daemon=True).start()


def _advance_seq_cache(path: Path, events: list[dict[str, Any]]) -> None:
    """append_events 落盘后同步 seq 分配缓存（调用方须持 _write_lock）。

    批次带 seq 时把已建立的缓存推进到 max(缓存, 批次最大 seq)，防止后续
    append_with_seq 分配出重复 seq；缓存未建立时不设，下次分配重扫校准。
    """
    cached = _seq_cache.get(path)
    if cached is None:
        return
    batch_max = 0
    for event in events:
        try:
            batch_max = max(batch_max, int(event.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    if batch_max > cached:
        _seq_cache[path] = batch_max


def _log_write_failure(path: Path, count: int) -> None:
    """底稿丢失可见化：写失败记 warning + 投递 /message 系统消息（按文件去抖，只报首次）。"""
    if path in _write_failure_logged:
        return
    _write_failure_logged.add(path)

    from src.core.logger import logger

    logger.warning(
        "session event log write failed, events lost (path=%s, batch=%d)",
        path,
        count,
    )
    # 投递到 /message（用户打开命令即可见）；此时磁盘可能已异常，自身失败仅静默
    try:
        from src.core.system_messages import add_system_message

        add_system_message(
            path.parents[2],
            kind="warning",
            title="会话事件日志写入失败",
            body=f"底稿可能有事件丢失（{path.name}，批次 {count} 条）。请检查磁盘空间与权限。",
            dedupe_key="session_log_write_failure",
        )
    except Exception:
        return


def append_event(path: Path, event: dict[str, Any]) -> None:
    append_events(path, [event])


def append_with_seq(path: Path, factory: Any) -> bool:
    """锁内分配连续 seq 并追加一批事件（线程安全）。

    ``factory(start_seq) -> list[event]`` 收到本批起始 seq（已有最大 seq + 1），
    返回要追加的事件（可为空，此时不写也不消耗 seq）。分配点全局唯一，
    同进程内多个 recorder / 归档并发写同一文件不会产生重复 seq。
    Returns ``True`` on success, ``False`` when the batch was **not** written
    (caller keeps its cursor so a later sync retries the same diff).
    失败可见化（warning + /message 系统消息，去抖）由 ``_log_write_failure`` 负责。
    """
    written = False
    events: list[dict[str, Any]] = []
    try:
        with _write_lock:
            last = _seq_cache.get(path)
            if last is None or not path.is_file() or path.stat().st_size == 0:
                # 首次，或文件被外部删除/清空/刚轮转：经 last_seq 尾部快路径校准
                # （active 读不到 seq 时回退归档末段，不全量扫历史）
                last = last_seq(path)
                _seq_cache[path] = last
            events = factory(last + 1)
            if not events:
                return True
            lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
            _maybe_rotate(path)
            with path.open("a", encoding="utf-8") as handle:
                _ensure_tail_newline(handle, path)
                handle.write(lines)
            _seq_cache[path] = last + len(events)
            _write_failure_logged.discard(path)
            written = True
            return True
    except OSError:
        _log_write_failure(path, len(events))
        return False
    finally:
        if not written:
            # 分配点未消费（seq 未落盘）：回退缓存，避免下次分配重复 seq
            _seq_cache.pop(path, None)


def last_seq(path: Path) -> int:
    """当前日志最后一条事件的 seq；空文件/无 seq 返回 0。

    快速路径只读各段尾部窗口（active 优先，按段从新到旧回退），
    避免轮转后首次分配全量扫历史；窗口读不到再整段扫。
    """
    for segment in reversed([*_archive_segments(path), path]):
        seq = _segment_last_seq(segment)
        if seq is not None:
            return seq
    return 0


def _last_seq_in_tail(tail: bytes) -> int | None:
    """尾部窗口反序找第一条可解析且 seq>0 的事件；窗口首行截断返回 None。"""
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            seq = int(event.get("seq") or 0)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, AttributeError):
            return None  # 行被窗口截断：无法确认，交给调用方加倍窗口或整段扫
        if seq > 0:
            return seq
        return None
    return None


def _segment_last_seq(segment: Path) -> int | None:
    """单段最后一条事件的 seq；段缺失/为空/无 seq 返回 None。"""
    if segment.name.endswith(".gz"):
        # gzip 流不支持廉价尾部 seek：整段扫（归档段不大，冷路径可接受）
        seq = 0
        for event in _iter_segment_events(segment):
            try:
                seq = int(event.get("seq") or 0)
            except (TypeError, ValueError):
                continue
        return seq or None
    try:
        size = segment.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    # 尾部窗口自适应：长行（>256KB）会截断窗口首行，逐步加倍重读（上限 2MB），
    # 避免一截断就直接整段扫（27MB active 上每次 last_seq 0.1–0.3s 的冷延迟）。
    for window in (_TAIL_READ_BYTES, _TAIL_READ_BYTES * 4, _TAIL_READ_BYTES * 8):
        try:
            with segment.open("rb") as handle:
                handle.seek(max(0, size - window))
                tail = handle.read()
        except OSError:
            return None
        hit = _last_seq_in_tail(tail)
        if hit is not None:
            return hit
        if window >= size:
            break  # 已读满全段仍无有效 seq：跳出走整段扫（其实等价）
    # 尾部窗口没拿到（行超长被截断/尾行损坏）：整段扫兜底
    seq = 0
    for event in _iter_segment_events(segment):
        try:
            seq = int(event.get("seq") or 0)
        except (TypeError, ValueError):
            continue
    return seq or None


def _iter_segment_events(segment: Path) -> Iterator[dict[str, Any]]:
    """逐行迭代单段事件；损坏行跳过，段缺失为空迭代。.gz 段透明解压。"""
    if not segment.is_file():
        return
    if segment.name.endswith(".gz"):
        try:
            with gzip.open(segment, "rt", encoding="utf-8") as handle:
                yield from _parse_event_lines(handle)
        except OSError:
            return
        return
    with segment.open(encoding="utf-8") as handle:
        yield from _parse_event_lines(handle)


def _parse_event_lines(lines: Any) -> Iterator[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """按落盘顺序迭代事件（归档段按时间序在前，active 在后）；损坏行跳过。"""
    for segment in [*_archive_segments(path), path]:
        yield from _iter_segment_events(segment)


def read_events(path: Path, *, session_id: str | None = None) -> list[dict[str, Any]]:
    """读取全部事件（可按会话过滤），按 seq 升序。"""
    events = list(iter_events(path))
    if session_id:
        events = [e for e in events if str(e.get("session_id") or "") == session_id]
    events.sort(key=lambda e: int(e.get("seq") or 0))
    return events


def count_events(path: Path) -> int:
    return sum(1 for _ in iter_events(path))


def _iter_segment_events_reverse(segment: Path) -> Iterator[dict[str, Any]]:
    """倒序迭代单段事件（新→旧）。active 直接倒序读行；.gz 冷段解压后内存反转
    （归档段不可变，反转安全）。损坏行跳过，段缺失为空迭代。"""
    if not segment.is_file():
        return
    if segment.name.endswith(".gz"):
        try:
            with gzip.open(segment, "rt", encoding="utf-8") as handle:
                events = list(_parse_event_lines(handle))
        except OSError:
            return
        yield from reversed(events)
        return
    # active：倒序读行（避免大段全量正读）。行很长（含 tool 结果全文），
    # 用尾部窗口逐块反读。
    with segment.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        buf = b""
        pos = size
        chunk = 1024 * 1024
        while pos > 0:
            read_size = min(chunk, pos)
            pos -= read_size
            handle.seek(pos)
            buf = handle.read(read_size) + buf
            lines = buf.split(b"\n")
            buf = lines[0]  # 首行可能不完整（跨块），留给下一块拼接
            for raw in reversed(lines[1:]):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
        if buf.strip():
            line = buf.decode("utf-8", errors="replace").strip()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict):
                yield event


def read_session_events_tail(path: Path, session_id: str, *, min_seq: int = 0) -> list[dict[str, Any]]:
    """倒序快读：只收集目标 session 的事件段，按 seq 升序返回。

    追加写保证同一 session 的事件在磁带上连续成段（新 session 起点即旧段结尾）。
    从最新段倒序扫，收齐目标 session 事件后，一遇更早 session 的 session/meta
    （上一个会话的收尾标记）即停——前面的旧段（含 .gz 冷段）不读。恢复结果与
    全量 ``read_events`` 一致，但大磁带冷启动只读尾部一段（7.8s → 亚秒）。
    录像带永久保存，本函数只读不删。

    ``min_seq`` > 0 时只收集 seq > min_seq 的事件（检查点增量读：水位之后的
    新增事件）。倒序扫到 seq <= min_seq 的目标事件即停（更早的已在基线里）。
    """
    target = str(session_id or "")
    if not target:
        return []
    segments = [*_archive_segments(path), path]
    collected: list[dict[str, Any]] = []
    stopped = False
    for segment in reversed(segments):
        if stopped:
            break
        for event in _iter_segment_events_reverse(segment):
            sid = str(event.get("session_id") or "")
            if sid == target:
                seq = int(event.get("seq") or 0)
                if min_seq > 0 and seq <= min_seq:
                    # 增量模式：扫到水位及更早的目标事件即停（基线已含）
                    stopped = True
                    break
                collected.append(event)
                continue
            if not collected:
                # 尚未进入目标段（段尾可能是更新 session 的事件），继续倒扫
                continue
            # 已收集到目标段的事件：越过其上界的判定须保守——目标段内部可能
            # 因影子重写穿插其它 session 的单条事件，不能见异就停。只有遇到
            # 其它 session 的 session/meta（上一个会话的收尾标记，其下即旧段）
            # 才确认离开目标段。这样既不漏目标段开头，也不多读更早的旧段。
            if event.get("kind") == "session/meta":
                stopped = True
                break
            # 目标段内穿插的其它 session 零星事件：跳过继续（不收集也不停）
    collected.sort(key=lambda e: int(e.get("seq") or 0))
    return collected


__all__ = [
    "append_event",
    "append_events",
    "append_with_seq",
    "count_events",
    "iter_events",
    "last_seq",
    "read_events",
    "read_session_events_tail",
    "resolve_session_log_path",
]
