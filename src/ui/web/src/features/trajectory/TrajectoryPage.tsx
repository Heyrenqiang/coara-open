import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getWS, type ServerMessage } from "../../lib/ws";
import { fetchToolCallById, fetchToolOutput, type ToolCallLookupActivity } from "../../lib/api";
import {
  fetchTrajectoryWindow,
  flattenTrajectoryLines,
  groupTrajectoryRows,
  isDelegateRow,
  mergeTrajectoryRows,
  projectLiveFrame,
  type TrajectoryLine,
  type TrajectoryRow,
} from "./trajectoryData";
import {
  EnrichCache,
  SPILL_PAGE_LINES,
  shouldAutoWantSpill,
  shouldCacheEnrichment,
  tapeDetailSufficient,
} from "./trajectoryToolDetail";
import { TrajectoryRowView } from "./TrajectoryRowView";
import { TrajectoryTimeline } from "./TrajectoryTimeline";
import { DiffBlocksView } from "../../components/DiffBlocksView";
import type { CanonicalDiffLines } from "../../lib/ws";
import "./trajectory.css";

const DETAIL_MIN = 280;
const DETAIL_MAX = 720;
/** 窄带：轨迹流列宽上限（详情常驻时主区不再铺满）。 */
const TAPE_MAX_WIDTH = 720;

const _enrichCache = new EnrichCache();

/** 详情面板标题：按行角色给可读的名称。 */
function detailTitle(row: TrajectoryRow): string {
  if (row.role === "tool") return row.tool?.name || "工具调用";
  const titles: Record<string, string> = {
    user: "用户输入",
    continuation: "接续输入",
    assistant: "助手正文",
    thinking: "思考块",
    inject: "系统注入",
    context_module: "上下文模块",
    prompt: "系统提示词",
    subagent: "子智能体",
    janitor: "janitor 维护",
    background: "后台任务",
    divider: "分割线",
  };
  return titles[row.role] ?? row.role ?? "详情";
}

export interface TrajectoryPageProps {
  /** 目标工作空间目录（就地切换的结果，由父组件 TapeView 持有）。 */
  workspaceDir: string;
  /** 父页点「刷新」时递增；非 0 时重新拉落带尾窗（与切空间同路径）。 */
  reloadToken?: number;
}

export function TrajectoryPage({ workspaceDir, reloadToken = 0 }: TrajectoryPageProps) {
  const [lines, setLines] = useState<TrajectoryLine[]>([]);
  const [hasOlder, setHasOlder] = useState(false);
  const [oldestOffset, setOldestOffset] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // 选中身份：存行身份（call_id 优先、否则 seq），prepend 历史后对象引用变了也不漂
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(new Set());
  const [detailWidth, setDetailWidth] = useState(400);
  const [resizing, setResizing] = useState(false);
  const [visibleRange, setVisibleRange] = useState({ start: 0, count: 0 });
  const [searchQuery, setSearchQuery] = useState("");
  const streamRef = useRef<HTMLDivElement>(null);

  /** 行身份：工具行用 call_id（稳定），其它用 seq。 */
  const rowId = useCallback((row: TrajectoryRow): string => {
    return row.tool?.call_id || `seq:${row.seq}`;
  }, []);

  /** 选中行：按身份从当前 lines 反查（prepend 历史后身份不变、对象引用更新）。 */
  const selected = useMemo(() => {
    if (!selectedId) return null;
    for (const line of lines) {
      const rows = line.kind === "group" && line.group
        ? [line.group.header, ...line.group.children]
        : [line.row];
      for (const row of rows) {
        if (row && rowId(row) === selectedId) return row;
      }
    }
    return null;
  }, [lines, selectedId, rowId]);

  // 工具详情：点开才补全；带子够用则零请求；瘦帧才回查 trace（带 LRU）
  const [toolEnrichment, setToolEnrichment] = useState<ToolCallLookupActivity | null>(null);
  const [toolEnrichState, setToolEnrichState] = useState<"idle" | "loading" | "miss" | "skipped">("idle");
  const [spillContent, setSpillContent] = useState<string | null>(null);
  const [spillArgs, setSpillArgs] = useState<Record<string, unknown> | null>(null);
  const [spillLoading, setSpillLoading] = useState(false);
  const [spillNext, setSpillNext] = useState<number | null>(null);
  const [spillError, setSpillError] = useState<string | null>(null);
  /** 用户主动点「加载全文」后才拉 spill（截断预览已够用时不自动打大包）。 */
  const [spillWanted, setSpillWanted] = useState(false);

  const selectedCallId =
    selected?.role === "tool" ? selected.tool?.call_id?.trim() || "" : "";
  const selectedRunning = Boolean(selected?.role === "tool" && selected.tool?.running);
  const tapeSpillRef =
    selected?.role === "tool" ? selected.tool?.output_ref?.trim() || "" : "";
  const tapeInlineOutput =
    selected?.role === "tool" ? selected.tool?.output || "" : "";
  const hasTapeOutput = Boolean(tapeInlineOutput.trim());
  const tapeTruncated =
    selected?.role === "tool" ? Boolean(selected.tool?.output_truncated) : false;
  const tapeSufficient =
    selected?.role === "tool" && selected.tool ? tapeDetailSufficient(selected.tool) : false;

  // 网络边界只用稳定标量：避免把整段 output 放进 deps（live 换行对象会误触发重拉）
  useEffect(() => {
    setToolEnrichment(null);
    setToolEnrichState("idle");
    setSpillContent(null);
    setSpillArgs(null);
    setSpillNext(null);
    setSpillError(null);
    setSpillWanted(false);
    if (!selectedCallId) return;

    // 仍在跑：只看落带摘要，不扫 trace（完成后再决定）
    if (selectedRunning) {
      setToolEnrichState("skipped");
      return;
    }

    const inlineForSpill = hasTapeOutput ? "1" : "";

    if (tapeSufficient) {
      setToolEnrichState("skipped");
      if (
        shouldAutoWantSpill({
          spillRef: tapeSpillRef,
          truncated: tapeTruncated,
          inlineOutput: inlineForSpill,
        })
      ) {
        setSpillWanted(true);
      }
      return;
    }

    const cached = _enrichCache.get(selectedCallId);
    if (cached) {
      setToolEnrichment(cached);
      setToolEnrichState("idle");
      if (
        shouldAutoWantSpill({
          spillRef: cached.tool_output_ref || "",
          truncated: Boolean(cached.tool_output_truncated),
          inlineOutput: cached.tool_output || inlineForSpill,
        })
      ) {
        setSpillWanted(true);
      }
      return;
    }

    let cancelled = false;
    setToolEnrichState("loading");
    void fetchToolCallById(selectedCallId)
      .then((row) => {
        if (cancelled) return;
        if (shouldCacheEnrichment(row) && row) {
          _enrichCache.set(selectedCallId, row);
        }
        if (row) {
          setToolEnrichment(row);
          setToolEnrichState("idle");
          if (
            shouldAutoWantSpill({
              spillRef: row.tool_output_ref || "",
              truncated: Boolean(row.tool_output_truncated),
              inlineOutput: row.tool_output || inlineForSpill,
            })
          ) {
            setSpillWanted(true);
          }
        } else {
          setToolEnrichState("miss");
        }
      })
      .catch(() => {
        if (!cancelled) setToolEnrichState("miss");
      });
    return () => {
      cancelled = true;
    };
  }, [
    selectedCallId,
    selectedRunning,
    tapeSufficient,
    tapeSpillRef,
    tapeTruncated,
    hasTapeOutput,
  ]);

  const spillRef =
    tapeSpillRef || toolEnrichment?.tool_output_ref?.trim() || "";

  // spill：仅 spillWanted 时拉首页；翻页另走按钮
  useEffect(() => {
    setSpillContent(null);
    setSpillArgs(null);
    setSpillNext(null);
    setSpillError(null);
    if (!spillRef || !spillWanted) return;
    let cancelled = false;
    setSpillLoading(true);
    void fetchToolOutput(spillRef, { offset: 1, limit: SPILL_PAGE_LINES })
      .then((res) => {
        if (cancelled) return;
        setSpillContent(res.content ?? "");
        if (res.arguments && typeof res.arguments === "object" && Object.keys(res.arguments).length > 0) {
          setSpillArgs(res.arguments);
        }
        setSpillNext(
          res.next_offset != null
            ? res.next_offset
            : res.has_more
              ? (res.offset ?? 1) + (res.limit ?? SPILL_PAGE_LINES)
              : null,
        );
      })
      .catch((err: Error) => {
        if (!cancelled) setSpillError(err.message || "加载完整输出失败");
      })
      .finally(() => {
        if (!cancelled) setSpillLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [spillRef, spillWanted]);

  const loadMoreSpill = useCallback(async () => {
    if (!spillRef) return;
    if (!spillWanted) {
      setSpillWanted(true);
      return;
    }
    if (spillNext == null) return;
    setSpillLoading(true);
    setSpillError(null);
    try {
      const res = await fetchToolOutput(spillRef, { offset: spillNext, limit: SPILL_PAGE_LINES });
      setSpillContent((prev) => (prev || "") + (res.content ?? ""));
      setSpillNext(
        res.next_offset != null
          ? res.next_offset
          : res.has_more
            ? spillNext + (res.limit ?? SPILL_PAGE_LINES)
            : null,
      );
    } catch (err) {
      setSpillError((err as Error).message || "加载失败");
    } finally {
      setSpillLoading(false);
    }
  }, [spillRef, spillWanted, spillNext]);

  /** 详情用：落带 ∪ trace ∪ spill，能显示的全显示。 */
  const detailTool = useMemo(() => {
    if (!selected || selected.role !== "tool" || !selected.tool) return null;
    const base = selected.tool;
    const enrich = toolEnrichment;
    const pickArgs = (...candidates: Array<Record<string, unknown> | null | undefined>) => {
      for (const c of candidates) {
        if (c && typeof c === "object" && Object.keys(c).length > 0) return c;
      }
      return undefined;
    };
    const arguments_ = pickArgs(base.arguments, enrich?.args, spillArgs);
    const inlineOutput = (base.output || enrich?.tool_output || "").trim();
    const spillText = (spillContent || "").trim();
    const output = spillText || inlineOutput || undefined;
    const diff =
      selected.diff ??
      (enrich?.diff_lines && typeof enrich.diff_lines === "object" ? enrich.diff_lines : null);
    const summaryExtra = (enrich?.summary || "").trim();
    const durationMs =
      base.duration_ms != null
        ? base.duration_ms
        : typeof enrich?.duration_ms === "number"
          ? enrich.duration_ms
          : null;
    const truncated = Boolean(base.output_truncated || enrich?.tool_output_truncated);
    return {
      callId: base.call_id || enrich?.call_id || "",
      arguments: arguments_,
      output,
      diff,
      summaryExtra,
      durationMs,
      is_error: base.is_error || Boolean(enrich?.is_error) || enrich?.ok === false,
      truncated,
      hasSpillRef: Boolean(spillRef),
      spillWanted,
      loading: toolEnrichState === "loading",
      spillLoading,
      spillNext,
      spillError,
    };
  }, [
    selected,
    toolEnrichment,
    toolEnrichState,
    spillContent,
    spillArgs,
    spillRef,
    spillWanted,
    spillLoading,
    spillNext,
    spillError,
  ]);
  // 行的扁平序（与时间线一致）：组头+子行全展开，用于 seek/可见区间换算
  const flatRows = useMemo(() => flattenTrajectoryLines(lines), [lines]);
  // row.seq → 扁平序号：与时间线 seek 的换算桥。折叠不影响序号——子行无论
  // 是否在 DOM 都占序号位（此前渲染游标只在展开时递增，折叠态序号整体错位）。
  const seqToFlat = useMemo(() => {
    const map = new Map<number, number>();
    flatRows.forEach((row, i) => map.set(row.seq, i));
    return map;
  }, [flatRows]);

  const applyTipWindow = useCallback(
    (workspace: string, opts?: { scrollToEnd?: boolean; merge?: boolean }) => {
      void fetchTrajectoryWindow(workspace, null).then((win) => {
        if (opts?.merge) {
          // 与 need_topup 同策：并入现有带，保留已加载更早历史与展开态
          setLines((prev) =>
            mergeTrajectoryRows(flattenTrajectoryLines(prev, { omitSyntheticHeaders: true }), win.rows),
          );
          setHasOlder((prev) => prev || win.has_older);
        } else {
          setLines(groupTrajectoryRows(win.rows));
          setHasOlder(win.has_older);
          setOldestOffset(win.oldest_offset);
        }
        if (!opts?.scrollToEnd) return;
        requestAnimationFrame(() => {
          const el = streamRef.current;
          if (el) el.scrollTop = el.scrollHeight;
        });
      });
    },
    [],
  );

  useEffect(() => {
    if (!workspaceDir) return;
    setSelectedId(null);
    setLines([]);
    setExpandedGroups(new Set());
    applyTipWindow(workspaceDir, { scrollToEnd: true });
  }, [workspaceDir, applyTipWindow]);

  // 顶栏「刷新」：并入尾窗（不整表替换，避免冲掉更早历史与实时超前帧）
  useEffect(() => {
    if (!workspaceDir || !reloadToken) return;
    setSelectedId(null);
    applyTipWindow(workspaceDir, { scrollToEnd: true, merge: true });
  }, [reloadToken, workspaceDir, applyTipWindow]);

  /* ---- 实时流：订阅 WS 广播帧，微批合帧进轨迹 ---- */
  const liveBufferRef = useRef<TrajectoryRow[]>([]);
  const liveTimerRef = useRef<number>(0);
  const liveSeqRef = useRef(1_000_000_000);

  const flushLive = useCallback(() => {
    liveTimerRef.current = 0;
    const batch = liveBufferRef.current;
    liveBufferRef.current = [];
    if (batch.length === 0) return;
    // 整表重归组：子行挂回组头，默认仍折叠（不因实时帧摊平顶层）
    setLines((prev) => mergeTrajectoryRows(flattenTrajectoryLines(prev, { omitSyntheticHeaders: true }), batch));
    const el = streamRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }, []);

  useEffect(() => {
    if (!workspaceDir) return;
    const ws = getWS();
    const unsub = ws.onMessage((msg: ServerMessage) => {
      if (msg.type === "need_topup") {
        // 背压丢帧后增量补尾窗：并入现有带（保留已加载的更早历史与展开态）
        void fetchTrajectoryWindow(workspaceDir, null).then((win) => {
          setLines((prev) =>
            mergeTrajectoryRows(flattenTrajectoryLines(prev, { omitSyntheticHeaders: true }), win.rows),
          );
          setHasOlder((prev) => prev || win.has_older);
        });
        return;
      }
      const wsDir = String(msg.workspace_dir ?? "").trim();
      // 与主聊天 _isFrameInWorkspace 同尺：缺 workspace_dir 或不匹配一律丢，
      // 缺目录放行会吃到别空间的空戳帧（TurnStream 只在非空时才打戳）。
      if (!wsDir || wsDir !== workspaceDir) return;
      const row = projectLiveFrame(msg);
      if (row === null) return;
      // 有 view_seq 则与落带同号，合帧可去重；否则私有计数（旧帧/无落带路径）
      if (!(row.seq > 0)) {
        row.seq = liveSeqRef.current++;
      }
      liveBufferRef.current.push(row);
      if (liveTimerRef.current === 0) {
        liveTimerRef.current = window.setTimeout(flushLive, 50);
      }
    });
    return () => {
      unsub();
      if (liveTimerRef.current !== 0) {
        window.clearTimeout(liveTimerRef.current);
        liveTimerRef.current = 0;
      }
      liveBufferRef.current = [];
    };
  }, [workspaceDir, flushLive, applyTipWindow]);

  /* ---- 断线重连回补：断连窗口内的实时帧已丢，重连后并入尾窗 ---- */
  useEffect(() => {
    if (!workspaceDir) return;
    const ws = getWS();
    let wasDisconnected = false;
    return ws.onConnection((connected) => {
      if (!connected) {
        wasDisconnected = true;
        return;
      }
      if (!wasDisconnected) return; // 注册即触发的当前态不算重连
      wasDisconnected = false;
      void fetchTrajectoryWindow(workspaceDir, null).then((win) => {
        setLines((prev) =>
          mergeTrajectoryRows(flattenTrajectoryLines(prev, { omitSyntheticHeaders: true }), win.rows),
        );
        setHasOlder((prev) => prev || win.has_older);
      });
    });
  }, [workspaceDir]);

  const loadOlder = useCallback(() => {
    if (!workspaceDir || loadingOlder || !hasOlder) return;
    setLoadingOlder(true);
    const el = streamRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    void fetchTrajectoryWindow(workspaceDir, oldestOffset)
      .then((win) => {
        setLines((prev) =>
          mergeTrajectoryRows(win.rows, flattenTrajectoryLines(prev, { omitSyntheticHeaders: true })),
        );
        setHasOlder(win.has_older);
        setOldestOffset(win.oldest_offset);
        requestAnimationFrame(() => {
          if (el) el.scrollTop += el.scrollHeight - prevHeight;
        });
      })
      .finally(() => setLoadingOlder(false));
  }, [workspaceDir, hasOlder, loadingOlder, oldestOffset]);

  const onStreamScroll = useCallback(() => {
    const el = streamRef.current;
    if (!el) return;
    if (el.scrollTop < 60) loadOlder();
    // 可见区间上报给时间线：首条可见行的扁平序号 + 可见行数
    const containerTop = el.getBoundingClientRect().top;
    const children = Array.from(el.querySelectorAll<HTMLElement>("[data-flat-index]"));
    let first = -1;
    let count = 0;
    for (const node of children) {
      const rect = node.getBoundingClientRect();
      if (rect.bottom > containerTop && rect.top < containerTop + el.clientHeight) {
        const idx = Number(node.dataset.flatIndex);
        if (first < 0) first = idx;
        count++;
      }
    }
    if (first >= 0) setVisibleRange({ start: first, count: Math.max(count, 1) });
  }, [loadOlder]);

  /** 时间线 seek：按扁平序号滚到对应行并打开详情。目标行在折叠组内时先展开该组
   *  （折叠态子行不在 DOM 上，data-flat-index 断档会找不到节点）。
   *  janitor 子行无 parent，按 lines 里所属组 key 展开。 */
  const seekTo = useCallback(
    (flatIndex: number) => {
      const target = flatRows[flatIndex];
      if (!target) return;
      if (target.parent) {
        setExpandedGroups((prev) => (prev.has(target.parent as string) ? prev : new Set(prev).add(target.parent as string)));
      } else {
        for (const line of lines) {
          const g = line.kind === "group" ? line.group : undefined;
          if (!g) continue;
          if (g.header === target || g.children.includes(target)) {
            setExpandedGroups((prev) => (prev.has(g.key) ? prev : new Set(prev).add(g.key)));
            break;
          }
        }
      }
      setSelectedId(rowId(target));
      // 展开后行才进 DOM，等一帧再滚
      requestAnimationFrame(() => {
        const el = streamRef.current;
        if (!el) return;
        const node = el.querySelector<HTMLElement>(`[data-flat-index="${flatIndex}"]`);
        if (node) {
          el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - 40;
        }
      });
    },
    [flatRows, lines, rowId],
  );

  const toggleGroup = useCallback((key: string) => {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const onResizeStart = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      setResizing(true);
      const startX = e.clientX;
      const startWidth = detailWidth;
      const onMove = (ev: PointerEvent) => {
        const next = startWidth + (startX - ev.clientX);
        setDetailWidth(Math.min(DETAIL_MAX, Math.max(DETAIL_MIN, next)));
      };
      const onUp = () => {
        setResizing(false);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [detailWidth],
  );

  const rowCount = useMemo(() => flatRows.length, [flatRows]);

  // 搜索：行摘要匹配（大小写不敏感）。命中集用于行级高亮、未命中降透明。
  const searchHits = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return null;
    const hits = new Set<string>();
    for (const row of flatRows) {
      const hay = `${row.text ?? ""} ${row.tool?.name ?? ""} ${row.tag ?? ""}`.toLowerCase();
      if (hay.includes(q)) hits.add(rowId(row));
    }
    return hits;
  }, [flatRows, searchQuery, rowId]);

  /** 行显隐：无搜索全显；有搜索未命中降透明（不删行，保持带子完整感）。 */
  const rowDimmed = useCallback(
    (row: TrajectoryRow) => searchHits !== null && !searchHits.has(rowId(row)),
    [searchHits, rowId],
  );

  // 每行 DOM 上的扁平序号：从 seqToFlat 取（折叠组子行也占序号位，不断档）
  const flatOf = (row: TrajectoryRow) => seqToFlat.get(row.seq) ?? -1;

  return (
    <div
      className="tr-layout tr-with-detail"
      style={{ ["--tr-detail-width" as string]: `${detailWidth}px` }}
    >
      <section className="tr-main">
        <div className="tr-toolbar">
          <input
            type="search"
            className="tr-search"
            placeholder="搜索录像带（行摘要 / 工具名 / 注入类型）"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            aria-label="搜索录像带"
          />
          <span className="tr-toolbar-count">{rowCount} 行{searchHits !== null ? ` · ${searchHits.size} 命中` : ""}</span>
        </div>
        <TrajectoryTimeline
          lines={lines}
          visibleStartIndex={visibleRange.start}
          visibleCount={visibleRange.count}
          onSeek={seekTo}
          onNeedOlder={loadOlder}
          hasOlder={hasOlder}
        />
        <div className="tr-stream-wrap">
          <div className="tr-stream" ref={streamRef} onScroll={onStreamScroll} style={{ maxWidth: TAPE_MAX_WIDTH }}>
            {hasOlder && (
              <button type="button" className="tr-load-older" onClick={loadOlder} disabled={loadingOlder}>
                {loadingOlder ? "加载中…" : "加载更早"}
              </button>
            )}
            {lines.map((line) => {
              if (line.kind === "group" && line.group) {
                const g = line.group;
                const expanded = expandedGroups.has(g.key);
                return (
                  <div key={`g-${g.key}`}>
                    {g.header && (
                      <div data-flat-index={flatOf(g.header)} className={rowDimmed(g.header) ? "tr-row-dimmed" : ""}>
                        <TrajectoryRowView
                          row={g.header}
                          selected={selectedId !== null && rowId(g.header) === selectedId}
                          onSelect={(r) => {
                            setSelectedId(rowId(r));
                            // 子智能体带子默认折叠；点组头打开
                            if (!expanded) {
                              setExpandedGroups((prev) => new Set(prev).add(g.key));
                            }
                          }}
                          expandable
                          expanded={expanded}
                          onToggle={() => toggleGroup(g.key)}
                          childCount={g.children.length}
                        />
                      </div>
                    )}
                    {expanded && (
                      <div className="tr-group-children">
                        {g.children.map((child) => (
                          <div key={child.seq} data-flat-index={flatOf(child)} className={rowDimmed(child) ? "tr-row-dimmed" : ""}>
                            <TrajectoryRowView
                              row={child}
                              selected={selectedId !== null && rowId(child) === selectedId}
                              onSelect={(r) => setSelectedId(rowId(r))}
                              depth={1}
                            />
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              }
              const row = line.row;
              if (!row) return null;
              return (
                <div key={row.seq} data-flat-index={flatOf(row)} className={rowDimmed(row) ? "tr-row-dimmed" : ""}>
                  <TrajectoryRowView
                    row={row}
                    selected={selectedId !== null && rowId(row) === selectedId}
                    onSelect={(r) => setSelectedId(rowId(r))}
                    expandable={isDelegateRow(row)}
                  />
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <aside className="tr-detail">
        <div
          className={`tr-detail-resize${resizing ? " tr-resizing" : ""}`}
          onPointerDown={onResizeStart}
        />
        <div className="tr-detail-body">
          {selected ? (
            <>
              <div className="tr-detail-head">
                <span className="tr-detail-title">{detailTitle(selected)}</span>
                <button type="button" className="tr-detail-close" onClick={() => setSelectedId(null)} aria-label="清除选中">
                  ×
                </button>
              </div>
              <div className="tr-detail-meta">
                <span>seq {selected.seq}</span>
                <span>{new Date(selected.ts * 1000).toLocaleString()}</span>
                {selected.source && <span>{selected.source}</span>}
                {selected.turn_id ? <span title={selected.turn_id}>回合</span> : null}
                {selected.actor ? <span>{selected.actor}</span> : null}
                {selected.parent ? <span title={selected.parent}>父行</span> : null}
                {(detailTool?.durationMs ?? selected.tool?.duration_ms) != null && (
                  <span>{detailTool?.durationMs ?? selected.tool?.duration_ms}ms</span>
                )}
                {detailTool?.callId ? (
                  <span title={detailTool.callId} className="tr-detail-callid">
                    {detailTool.callId.slice(0, 12)}…
                  </span>
                ) : null}
                {(detailTool?.is_error ?? selected.tool?.is_error) && (
                  <span className="tr-detail-error-flag">失败</span>
                )}
                {selected.tag && <span>{selected.tag}</span>}
              </div>
              <div className="tr-detail-content">
                {selected.role === "prompt" && selected.prompt ? (
                  <>
                    <div className="tr-detail-section">
                      hash {selected.prompt.hash} · {selected.prompt.bytes} 字节
                      {selected.prompt.changed ? "（变更）" : "（会话首帧）"}
                    </div>
                    <pre>{selected.prompt.text || "（无快照正文）"}</pre>
                  </>
                ) : selected.role === "context_module" && selected.modules ? (
                  <>
                    <div className="tr-detail-section">{selected.modules.length} 个上下文模块</div>
                    <table className="tr-detail-table">
                      <tbody>
                        {selected.modules.map((m, i) => (
                          <tr key={m.id ?? i}>
                            <td>{m.label || m.id}</td>
                            <td className="tr-detail-table-dim">{m.file || "—"}</td>
                            <td className="tr-detail-table-dim">{m.scope || ""}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </>
                ) : selected.role === "tool" ? (
                  <>
                    {(selected.text || detailTool?.summaryExtra) && (
                      <>
                        <div className="tr-detail-section">摘要</div>
                        <pre>
                          {[selected.text, detailTool?.summaryExtra]
                            .filter((t) => t && t.trim())
                            .filter((t, i, arr) => arr.indexOf(t) === i)
                            .join("\n")}
                        </pre>
                      </>
                    )}
                    {detailTool?.arguments && Object.keys(detailTool.arguments).length > 0 && (
                      <>
                        <div className="tr-detail-section">参数</div>
                        <pre>{JSON.stringify(detailTool.arguments, null, 2)}</pre>
                      </>
                    )}
                    {detailTool?.diff != null && (
                      <>
                        <div className="tr-detail-section">变更</div>
                        <div className="tr-detail-diff">
                          <DiffBlocksView diff={detailTool.diff as CanonicalDiffLines} />
                        </div>
                      </>
                    )}
                    {(detailTool?.output ||
                      detailTool?.loading ||
                      detailTool?.spillLoading ||
                      detailTool?.hasSpillRef) && (
                      <>
                        <div className="tr-detail-section">结果</div>
                        {detailTool.truncated && (
                          <div className="tr-detail-note">
                            {detailTool.hasSpillRef
                              ? detailTool.spillWanted
                                ? "输出较长；以下为落盘全文"
                                : "输出较长，以下为截断预览"
                              : "输出较长，以下为截断预览"}
                          </div>
                        )}
                        {detailTool.output ? <pre>{detailTool.output}</pre> : null}
                        {detailTool.spillError ? (
                          <div className="tr-detail-note tr-detail-error-flag">{detailTool.spillError}</div>
                        ) : null}
                        {(detailTool.loading || detailTool.spillLoading) && (
                          <div className="tr-detail-note">正在补全…</div>
                        )}
                        {detailTool.hasSpillRef &&
                          (detailTool.truncated || !detailTool.output || detailTool.spillWanted) &&
                          (!detailTool.spillWanted || detailTool.spillNext != null) && (
                          <button
                            type="button"
                            className="tr-detail-more"
                            disabled={detailTool.spillLoading}
                            onClick={() => void loadMoreSpill()}
                          >
                            {!detailTool.spillWanted ? "加载全文" : "加载更多输出"}
                          </button>
                        )}
                      </>
                    )}
                    {!selected.text &&
                      !detailTool?.summaryExtra &&
                      !(detailTool?.arguments && Object.keys(detailTool.arguments).length > 0) &&
                      !detailTool?.output &&
                      detailTool?.diff == null &&
                      !detailTool?.loading &&
                      !detailTool?.spillLoading && <pre>（无内容）</pre>}
                  </>
                ) : (
                  <>
                    {selected.text ? <pre>{selected.text}</pre> : <pre>（无内容）</pre>}
                    {selected.diff != null && (
                      <>
                        <div className="tr-detail-section">变更</div>
                        <div className="tr-detail-diff">
                          <DiffBlocksView diff={selected.diff as CanonicalDiffLines} />
                        </div>
                      </>
                    )}
                  </>
                )}
                {selected.attachments && selected.attachments.length > 0 && (
                  <>
                    <div className="tr-detail-section">附件 {selected.attachments.length} 个</div>
                    <pre>{JSON.stringify(selected.attachments, null, 2)}</pre>
                  </>
                )}
                {selected.files && selected.files.length > 0 && (
                  <>
                    <div className="tr-detail-section">文件 {selected.files.length} 个</div>
                    <pre>{JSON.stringify(selected.files, null, 2)}</pre>
                  </>
                )}
              </div>
            </>
          ) : (
            <div className="tr-detail-empty">
              <span>{rowCount} 行</span>
              <p>点击左侧任意一行查看完整内容</p>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}
