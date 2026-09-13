"""CLI 子智能体折叠块：一次 delegate 运行只在滚动区留一行摘要，明细收进可回看的折叠块。

口径对齐 web 端折叠区（`src/ui/web/src/lib/toolLineGroups.ts` + `docs/消息渲染契约.md` §0.3）：
组序固定 **任务指令 → 过程 → 最终结果**，diff 按 `tool_call_id` 钉在对应工具行之后；
没有内容的组不出现；主会话自己的工具行/正文/diff 不受影响。

**Ctrl+O** 把明细画在输入区上方可重绘层（与 Thinking 同层）：展开出现、收起消失，
不往滚动区打字、不刷状态提醒。终端滚动区仍是追加式的——需要永久落滚动区用 ``/detail [关键词]``。

数据来源全部是既有帧/事件（不加任何端上帧字段）：
  - 任务指令 ← 内核 ``user_message`` 帧（``delegate_brief`` + ``parent_tool_call_id``）
  - 过程工具行 ← 活动树完成块（``ActivityLiveTracker``，按 delegate 祖先归属；CLI 工具行
    本就不走 attach 帧，``_attach_output_frame`` 对 kind=tool 一律不投）
  - diff     ← attach ``diff`` 帧（``parent_tool_call_id``）
  - 过程正文 ← attach ``subagent_chunk`` 帧（``tool_call_id`` = 发起它的 delegate 行 call_id）；
    末轮答复与 ``subagent_result`` 同文时从正文尾部剥掉，避免展开双显
  - 最终结果 ← attach ``subagent_result`` 帧；缺帧时回退 ``[前台子智能体已完成]`` 回显行
  - 类型/任务/耗时 ← ``subagent_start`` / ``background_agent_start`` / ``tool_start`` 事件

有界环形缓冲：只留最近 ``DETAIL_CAPACITY`` 块，超出丢最旧（摘要行标注可回看范围）。
**边界**：更长的历史不在这里回读——二期可从录像带（``traces/``）或 spill 的
``tool_outputs`` 按 call_id 回读；本轮只保证「最近 N 块」可回看，超出的明确丢弃。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# 本地保留的折叠块上限（有界环形缓冲）。再多的历史靠录像带回读（二期）。
DETAIL_CAPACITY = 24
# 单块条目上限（工具行 + diff 合计）：delegate 可能跑上千次工具，明细必须有界。
# 超限只累加计数、不再收条目——摘要行与过程组的项数仍保真，明细里标注省略多少条。
MAX_ENTRIES_PER_BLOCK = 500
# 单块 diff 数据的粗估上限（字节）：diff 帧未经裁剪，密集 diff 会把内存撑起来。
MAX_DIFF_BYTES_PER_BLOCK = 512 * 1024
# 过程正文 / 最终结果的内存上限：子智能体可能吐很长的中间正文，长线会话不无限涨。
_MAX_BODY_CHARS = 20000
_MAX_RESULT_CHARS = 20000
# 摘要行里任务描述的字数上限（一行放得下）
_TASK_CLIP = 30
# 过程工具行的缩进（与 web 展开区同款平铺）
_INDENT = "  "


def _clip(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    if len(one) <= limit:
        return one
    return one[:limit] + "…"


def _clip_tail(text: str, limit: int) -> str:
    """按字符数截断（保留原换行），用于过程正文内存上限。"""
    if len(text) <= limit:
        return text
    return text[:limit]


def strip_result_echo_from_body(body: str, result: str) -> str:
    """去掉过程正文末尾与最终结果重复的那段。

    末轮答复既走 ``subagent_chunk``（累进 body）又走 ``subagent_result``（result）：
    不裁的话展开时过程组与最终结果组会各打一遍同一段。只剥尾部完全匹配；
    中间过程正文保留。
    """
    body_s = str(body or "").rstrip()
    result_s = str(result or "").strip()
    if not body_s or not result_s:
        return str(body or "")
    if body_s == result_s:
        return ""
    if body_s.endswith(result_s):
        return body_s[: -len(result_s)].rstrip()
    return str(body or "")


def format_char_count(chars: int) -> str:
    """字数提示的紧凑写法（过千用 k）——与 web 折叠区同一读法。"""
    return f"{chars / 1000:.1f}k" if chars >= 1000 else f"{chars}"


def format_elapsed_short(seconds: float) -> str:
    """耗时短读法：``6.2s`` / ``45s`` / ``1m02s``。"""
    total = max(0.0, float(seconds))
    if total < 10:
        return f"{total:.1f}s"
    if total < 60:
        return f"{int(total)}s"
    minutes, secs = divmod(int(total), 60)
    return f"{minutes}m{secs:02d}s"


def format_task_summary(text: str, *, limit: int = _TASK_CLIP) -> str:
    """任务指令摘要：压成一行、超长截断；空文本返回空串（由调用方走退化写法）。"""
    return _clip(text, limit)


def brief_task_summary(text: str, *, limit: int = _TASK_CLIP) -> str:
    """从任务指令全文里取一行摘要：跳过信封行（``<任务指令>`` / ``</任务指令>``）。"""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<"):
            continue
        return format_task_summary(stripped, limit=limit)
    return ""


@dataclass(slots=True)
class FoldEntry:
    """折叠块「过程」组的一条：子智能体自己的工具行或它产生的 diff。"""

    kind: str  # "tool" | "diff"
    tool_call_id: str
    label: str = ""
    is_error: bool = False
    depth: int = 0
    display_blocks: Any = None
    tool_name: str = ""


@dataclass(slots=True)
class DetailItem:
    """明细渲染的一段：文本行，或一块 diff 渲染件（二者互斥）。

    ``style`` 是渲染用的语义标签（title=块标题 / group=组标题 / entry=工具行 /
    body=正文），颜色由渲染方查主题令牌决定——数据层不写色值。
    """

    text: str = ""
    display_blocks: Any = None
    style: str = "body"


@dataclass(slots=True)
class SubagentFoldBlock:
    """一次 delegate 运行的折叠块（一块一键：key = delegate 行的 tool_call_id）。"""

    key: str
    seq: int
    agent_type: str = ""
    background: bool = False
    task: str = ""
    brief: str = ""
    body: str = ""
    result: str = ""
    status: str = "running"  # running | complete | failed
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    entries: list[FoldEntry] = field(default_factory=list)
    expanded: bool = False
    summary_printed: bool = False
    # 明细是否已补打过滚动区（`/detail` 落滚动区后置位；终端追加式，打过就擦不掉）
    detail_printed: bool = False
    # 结果来源：frame（attach subagent_result 帧，权威）或 echo（[前台子智能体已完成] 回显）
    result_source: str = ""
    # 有界明细丢弃的条目数：计数保真（摘要行报真实条数），明细里标注省略。
    dropped_tools: int = 0
    dropped_diffs: int = 0
    # diff 帧累计粗估体积（字节）；超 MAX_DIFF_BYTES_PER_BLOCK 后不再收 diff。
    diff_bytes: int = 0

    @property
    def tool_entries(self) -> list[FoldEntry]:
        return [e for e in self.entries if e.kind == "tool"]

    @property
    def tool_count(self) -> int:
        """过程工具行数（含因有界而丢弃的）——摘要行报的是真实调用数。"""
        return len(self.tool_entries) + self.dropped_tools

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    def title(self) -> str:
        """类型标签：``coaras子智能体``（后台任务带「后台」前缀）。缺类型时退化为「子智能体」。"""
        core = f"{self.agent_type.strip()}子智能体" if self.agent_type.strip() else "子智能体"
        return f"后台 {core}" if self.background else core

    def status_text(self) -> str:
        return {"complete": "完成", "failed": "失败", "cancelled": "已取消"}.get(self.status, "运行中")

    def task_summary(self) -> str:
        """任务摘要：先用 delegate 的 description，再退到指令首行，都没有给显式占位。"""
        summary = format_task_summary(self.task)
        if summary:
            return summary
        return brief_task_summary(self.brief) or "（任务未记录）"


def format_summary_line(
    block: SubagentFoldBlock,
    *,
    capacity: int = 0,
    evicted: bool = False,
) -> str:
    """折叠块的默认唯一可见产出（一行）。

    形如 ``▸ coaras子智能体 · 排查持久化与调度竞态 · 12 项工具 · 完成 · 6.2s``；
    缺值退化：类型未知 → ``子智能体``；任务未知 → ``（任务未记录）``；未收尾 →
    ``运行中``（耗时按「到现在」算）。**没有过程条目时不报项数**——与 web 折叠头
    同口径（没有过程就不提项数，那行本也不可展开），避免出现无意义的「0 项工具」。
    明细缓冲溢出过（evicted）时追加 ``· 可回看最近 {capacity} 块``。
    """
    parts = [
        block.title(),
        block.task_summary(),
    ]
    if block.tool_count:
        parts.append(f"{block.tool_count} 项工具")
    parts += [
        block.status_text(),
        format_elapsed_short(block.elapsed),
    ]
    line = "▸ " + " · ".join(parts)
    if evicted and capacity > 0:
        line += f" · 可回看最近 {capacity} 块"
    return line


def detail_items(block: SubagentFoldBlock, *, capacity: int = 0, evicted: bool = False) -> list[DetailItem]:
    """折叠块的明细（三组 + diff），供终端「展开」逐段补打。

    组序与判空口径与 web 折叠区一致：任务指令 → 过程 → 最终结果，空组不出现；
    过程组内工具行按到达顺序，diff 排在同一次调用的工具行**紧后面**（按
    ``tool_call_id`` 配对；没有 call_id 的老帧退回原顺序，不丢）。
    """
    items: list[DetailItem] = [
        DetailItem(
            text=format_summary_line(block, capacity=capacity, evicted=evicted),
            style="title",
        )
    ]
    brief = block.brief.strip()
    if brief:
        items.append(DetailItem(text=f"{_INDENT}任务指令 · {format_char_count(len(brief))} 字", style="group"))
        items.extend(DetailItem(text=_INDENT * 2 + line) for line in brief.splitlines())
    entries = _paired_entries(block)
    # 末轮答复常同时进 body 与 result：展示时从过程正文剥掉与结果重复的尾部。
    body = strip_result_echo_from_body(block.body, block.result).strip()
    if entries or body:
        hint = f"{block.tool_count} 项"
        if body:
            hint = f"{hint} · {format_char_count(len(body))} 字"
        items.append(DetailItem(text=f"{_INDENT}过程 · {hint}", style="group"))
        for entry in entries:
            if entry.kind == "diff":
                items.append(DetailItem(display_blocks=entry.display_blocks))
            else:
                mark = "✗" if entry.is_error else "✓"
                items.append(DetailItem(text=f"{_INDENT * 2}{mark} {entry.label}", style="entry"))
        if body:
            items.extend(DetailItem(text=_INDENT * 2 + line) for line in body.splitlines())
        dropped = block.dropped_tools + block.dropped_diffs
        if dropped:
            items.append(DetailItem(text=f"{_INDENT * 2}…（过程过长，已省略 {dropped} 项）", style="entry"))
    result = block.result.strip()
    if result:
        items.append(DetailItem(text=f"{_INDENT}最终结果 · {format_char_count(len(result))} 字", style="group"))
        items.extend(DetailItem(text=_INDENT * 2 + line) for line in result.splitlines())
    return items


def _approx_diff_bytes(display_blocks: Any) -> int:
    """diff 帧的粗估体积：按块数与 diff 行数估算，不序列化整块（大 diff 上 str() 很贵）。"""
    try:
        if isinstance(display_blocks, (list, tuple)):
            lines = 0
            for item in display_blocks:
                raw = getattr(item, "diff_lines", None)
                lines += len(raw) if isinstance(raw, (list, tuple)) else 1
            return lines * 120 + 64
    except Exception:  # noqa: BLE001 - 估算失败按保守常量处理
        pass
    return 4096


def _paired_entries(block: SubagentFoldBlock) -> list[FoldEntry]:
    """过程组条目：工具行按到达序，diff 贴到产生它的那次调用之后。

    精确钉法要 diff 携带「产生它的工具调用 id」，但 attach 的 diff 帧没透出
    ``tool_call_id``（``_attach_output_frame`` 只带 display_blocks / diff_lines /
    tool_name / parent_tool_call_id；本轮不加端上帧字段）→ 退一步按**工具名**回贴：
    会产 diff 的 edit/write 不会被活动树归并（归并只对 read/glob/grep），
    同名工具行从后往前一一配对，顺序与内容都对得上；帧真带 call_id 时优先按 call_id。
    配不上的（工具名缺失/同名行已用尽）排在过程组末尾——**不丢**，只是位置近似。
    """
    tools = [e for e in block.entries if e.kind == "tool"]
    diffs = [e for e in block.entries if e.kind == "diff"]
    if not diffs:
        return list(tools)
    slots: dict[int, list[FoldEntry]] = {}
    used: set[int] = set()
    leftovers: list[FoldEntry] = []
    for diff in diffs:
        target: int | None = None
        if diff.tool_call_id:
            target = next(
                (i for i, tool in enumerate(tools) if tool.tool_call_id == diff.tool_call_id),
                None,
            )
        if target is None and diff.tool_name:
            target = next(
                (i for i in range(len(tools) - 1, -1, -1) if i not in used and tools[i].tool_name == diff.tool_name),
                None,
            )
        if target is None:
            leftovers.append(diff)
            continue
        used.add(target)
        slots.setdefault(target, []).append(diff)
    out: list[FoldEntry] = []
    for index, tool in enumerate(tools):
        out.append(tool)
        out.extend(slots.get(index, ()))
    out.extend(leftovers)
    return out


class SubagentFoldStore:
    """有界折叠块仓库（一块一键；同一子智能体的过程与结果落同一块）。"""

    def __init__(self, capacity: int = DETAIL_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._blocks: deque[SubagentFoldBlock] = deque()
        # 别名表：subagent_id / coara_id / task_id / delegate call_id → 块 key
        self._aliases: dict[str, str] = {}
        self._seq = 0
        # 被挤出去的块的最大序号（>0 表示明细缓冲已溢出，摘要行据此标注可回看范围）
        self._evicted_through = 0

    # ── 写入 ────────────────────────────────────────────────

    def ensure(
        self,
        key: str,
        *,
        agent_type: str = "",
        task: str = "",
        background: bool = False,
    ) -> SubagentFoldBlock | None:
        """取（或建）一块；key 为空时返回 None（无从归集宁可不落）。"""
        key = str(key or "").strip()
        if not key:
            return None
        # 别名（subagent_id / coara_id / task_id / call_id）也认：任何一路标识
        # 进来都落到同一块（帧与事件乱序到达时不会各建一块）。
        key = self.resolve(key) or key
        existing = self._lookup(key)
        if existing is not None:
            if agent_type and not existing.agent_type:
                existing.agent_type = agent_type
            if task and not existing.task:
                existing.task = task
            existing.background = existing.background or background
            return existing
        self._seq += 1
        block = SubagentFoldBlock(
            key=key,
            seq=self._seq,
            agent_type=str(agent_type or ""),
            task=str(task or ""),
            background=bool(background),
        )
        self._blocks.append(block)
        self._aliases[key] = key
        self._evict()
        return block

    def note_start(
        self,
        *,
        tool_call_id: str = "",
        subagent_id: str = "",
        coara_id: str = "",
        agent_type: str = "",
        task: str = "",
        background: bool = False,
    ) -> SubagentFoldBlock | None:
        """一次子智能体启动：以 delegate 行 call_id 为 key（缺则退到 subagent_id）。"""
        key = str(tool_call_id or subagent_id or "").strip()
        block = self.ensure(key, agent_type=agent_type, task=task, background=background)
        if block is None:
            return None
        for alias in (tool_call_id, subagent_id, coara_id):
            alias = str(alias or "").strip()
            if alias:
                self._aliases[alias] = block.key
        return block

    def note_brief(self, key: str, text: str) -> SubagentFoldBlock | None:
        """任务指令全文（delegate_brief 帧）。"""
        block = self.ensure(key, task=brief_task_summary(text))
        if block is None:
            return None
        if text.strip():
            block.brief = text
        return block

    def add_tool(self, key: str, entry: FoldEntry) -> SubagentFoldBlock | None:
        block = self.ensure(key)
        if block is None:
            return None
        if len(block.entries) >= MAX_ENTRIES_PER_BLOCK:
            # 有界：超限只累加计数（项数保真），明细里标注省略了多少条。
            if entry.kind == "diff":
                block.dropped_diffs += 1
            else:
                block.dropped_tools += 1
            return block
        block.entries.append(entry)
        return block

    def add_diff(
        self,
        key: str,
        *,
        tool_call_id: str = "",
        display_blocks: Any = None,
        tool_name: str = "",
    ) -> SubagentFoldBlock | None:
        if not display_blocks:
            return None
        block = self.ensure(key)
        if block is None:
            return None
        size = _approx_diff_bytes(display_blocks)
        if block.diff_bytes + size > MAX_DIFF_BYTES_PER_BLOCK:
            block.dropped_diffs += 1
            return block
        block.diff_bytes += size
        return self.add_tool(
            key,
            FoldEntry(
                kind="diff",
                tool_call_id=str(tool_call_id or ""),
                display_blocks=display_blocks,
                tool_name=tool_name,
            ),
        )

    def append_body(self, key: str, text: str) -> SubagentFoldBlock | None:
        """过程正文（subagent_chunk 累积）。"""
        if not str(text or "").strip():
            return None
        block = self.ensure(key)
        if block is None:
            return None
        block.body = _clip_tail(block.body + text, _MAX_BODY_CHARS)
        return block

    def set_result(self, key: str, text: str, *, source: str = "") -> SubagentFoldBlock | None:
        """最终结果。已有结果时不覆盖（帧优先，回显只补缺）。

        写入后从过程正文剥掉与结果重复的尾部——末轮答复既走 chunk 又走 result。
        """
        body = str(text or "").strip()
        if not body:
            return None
        block = self.ensure(key)
        if block is None:
            return None
        if block.result and not (block.result_source == "echo" and source == "frame"):
            return block
        block.result = _clip_tail(body, _MAX_RESULT_CHARS)
        block.result_source = source or block.result_source
        block.body = strip_result_echo_from_body(block.body, block.result)
        return block

    def finish(self, key: str, *, failed: bool = False, cancelled: bool = False) -> SubagentFoldBlock | None:
        """收尾一块（终态只置一次）。key 可以是块 key，也可以是它的任一名义。

        ``cancelled``：被中断/取消（内核在中断前台 delegate 时发的也是
        ``subagent_failed``，只在 error 里标 cancelled）——与真失败分开显示。
        """
        block = self._block_for(key)
        if block is None:
            return None
        if block.status == "running":
            if cancelled:
                block.status = "cancelled"
            else:
                block.status = "failed" if failed else "complete"
            block.finished_at = time.monotonic()
        return block

    # ── 读取 ────────────────────────────────────────────────

    def resolve(self, *identifiers: str) -> str:
        """任一标识（call_id / subagent_id / coara_id / task_id）→ 块 key；未知返回空串。"""
        for raw in identifiers:
            alias = str(raw or "").strip()
            if not alias:
                continue
            key = self._aliases.get(alias)
            if key and self._lookup(key) is not None:
                return key
        return ""

    def latest(self) -> SubagentFoldBlock | None:
        return self._blocks[-1] if self._blocks else None

    def expanded_blocks(self) -> list[SubagentFoldBlock]:
        """当前标记为展开的块（时间升序）。Ctrl+O 收起优先扫这里，不依赖 latest。"""
        return [block for block in self._blocks if block.expanded]

    def recent(self, count: int = 1) -> list[SubagentFoldBlock]:
        """最近 count 块（按时间升序返回，便于按顺序回看）。"""
        take = max(1, int(count or 1))
        blocks = list(self._blocks)
        return blocks[-take:]

    def find_block(self, keyword: str = "") -> SubagentFoldBlock | None:
        """按关键词定位一块折叠块（空关键词＝最近一块）。

        匹配范围＝``agent_type`` + 任务摘要 + 摘要行标题拼成的 haystack，``casefold()``
        子串匹配（忽略大小写）；由近及远遍历，多命中取**最近**的一块。
        """
        needle = str(keyword or "").strip().casefold()
        for block in reversed(self._blocks):
            if not needle:
                return block
            haystack = "\n".join(
                part for part in (block.agent_type, block.task_summary(), block.title()) if part
            ).casefold()
            if needle in haystack:
                return block
        return None

    def detail_candidates(self) -> list[SubagentFoldBlock]:
        """`/detail` 补全候选：全部块，最近在前。"""
        return list(reversed(self._blocks))

    @property
    def available(self) -> int:
        """当前可回看的块数。"""
        return len(self._blocks)

    @property
    def evicted(self) -> bool:
        """是否已有明细被挤出（摘要行据此标注可回看范围）。"""
        return self._evicted_through > 0

    def summary_line(self, block: SubagentFoldBlock) -> str:
        return format_summary_line(block, capacity=self.capacity, evicted=self.evicted)

    def detail_items(self, block: SubagentFoldBlock) -> list[DetailItem]:
        return detail_items(block, capacity=self.capacity, evicted=self.evicted)

    # ── 内部 ────────────────────────────────────────────────

    def _lookup(self, key: str) -> SubagentFoldBlock | None:
        for block in self._blocks:
            if block.key == key:
                return block
        return None

    def _block_for(self, identifier: str) -> SubagentFoldBlock | None:
        """按块 key 或任一名义取块（别名先归一到 key）。"""
        raw = str(identifier or "").strip()
        if not raw:
            return None
        return self._lookup(self.resolve(raw) or raw)

    def _evict(self) -> None:
        while len(self._blocks) > self.capacity:
            dropped = self._blocks.popleft()
            self._evicted_through = max(self._evicted_through, dropped.seq)
            for alias, target in list(self._aliases.items()):
                if target == dropped.key:
                    self._aliases.pop(alias, None)


class DelegateLineFilter:
    """把 delegate 自己的 ``✓/✗`` 行挡在滚动区外——摘要行取代它（web 同款口径）。

    为什么不按「已知 call_id 的标签」精确匹配：``tool_start`` 事件走 100ms 微批、
    回合 ✓ 行走即时通道，快工具（delegate spawn 是 ack 即返）会先到 → 精确匹配
    会漏。改为**形态判定**（与 web ``toolVisibility`` 同一取向）：

    - ``delegate wait`` — 同步点不是工作，web 聊天流与活动树都不画
    - spawn / resume 形态（``delegate <类型>: <任务>``）—— 折叠块的摘要行取代它
    - ``delegate message`` / ``delegate stop`` —— 不是一次运行，保持原样可见

    非工具行一律原样透传；**半行留在缓冲里等下一块**（跨 chunk 切断的 ✓ 行不漏网）。
    只有「半行看起来像工具行」时才暂扣：普通流式正文照旧即时上屏，pending 行不延迟。
    """

    _TOOL_MARKS = ("✓", "✗")
    _KEEP_ACTIONS = ("message", "stop")

    def __init__(self) -> None:
        self._held = ""

    @classmethod
    def should_hide_line(cls, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped[0] not in cls._TOOL_MARKS:
            return False
        rest = stripped[1:].lstrip()
        if not rest.startswith("delegate"):
            return False
        tail = rest[len("delegate") :]
        if tail and not tail[0].isspace() and tail[0] not in "：:(":
            return False  # delegate_xxx：不是 delegate 工具行
        action = tail.split(maxsplit=1)[0].strip().lower() if tail.split() else ""
        return action not in cls._KEEP_ACTIONS

    def feed(self, text: str) -> str:
        """喂入一段流式文本，返回可以上屏的部分（完整行按行判定）。"""
        if not text:
            return ""
        self._held += text
        out: list[str] = []
        while True:
            idx = self._held.find("\n")
            if idx < 0:
                break
            line, self._held = self._held[: idx + 1], self._held[idx + 1 :]
            if not self.should_hide_line(line):
                out.append(line)
        # 半行：像工具行就先扣住（等成行判定），普通文本立即放行（pending 行不延迟）
        if self._held and not self._looks_like_tool_line(self._held):
            out.append(self._held)
            self._held = ""
        return "".join(out)

    def flush(self) -> str:
        """回合收尾：吐出暂扣的半行（若它整体就是 delegate 行则丢弃）。"""
        held, self._held = self._held, ""
        if not held or self.should_hide_line(held):
            return ""
        return held

    @classmethod
    def _looks_like_tool_line(cls, partial: str) -> bool:
        stripped = partial.lstrip()
        return bool(stripped) and stripped[0] in cls._TOOL_MARKS
