import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { tokens } from "../../theme/tokens";
import { flattenTrajectoryLines, type TrajectoryLine, type TrajectoryRow } from "./trajectoryData";

/** 顶部时间线：录像带的导航轴（不是信息展示层——展示交给录像带行与详情面板）。
 *
 * 单泳道（串行本质）：每行录像带按真实时间 ts 落一个刻度点，类型只靠颜色粗分；
 * 刻度带上方是日期/时刻刻度标签（昨天/前天/具体日期/分钟级，随缩放自动换挡）。
 * 两层视口：滚轮缩放（时间域放大缩小）+ 左键拖选（刷选一段区间定位主带）；
 * 窗口块对应主带当前可见区间，滚动时跟随。
 */

interface TrajectoryTimelineProps {
  lines: TrajectoryLine[];
  visibleStartIndex: number;
  visibleCount: number;
  /** 单击：滚到该行并打开详情 */
  onSeek: (flatIndex: number) => void;
  onNeedOlder: () => void;
  hasOlder: boolean;
}

/** 左端「还有更早」箭头热区宽度（px）；只有点这里才触发加载更早 */
const OLDER_HIT_PX = 18;

const HEIGHT = 52;
const AXIS_Y = 38; // 刻度带中线
const LABEL_Y = 14; // 时间标签带
const MIN_SPAN_MS = 60_000; // 最小可视跨度 1 分钟
const MAX_SPAN_PAD = 1.15; // 最大跨度留边

interface ViewWindow {
  /** 可见时间域 [start, end)（ms epoch） */
  start: number;
  end: number;
}

function rowColor(row: TrajectoryRow): string {
  if (row.role === "divider") return "__divider__";
  if (row.role === "user" || row.role === "continuation") return tokens.accent;
  if (row.role === "assistant" || row.role === "thinking") return tokens.tapeAssistant;
  if (row.role === "tool") {
    if (row.tool?.running) return tokens.progress;
    if (row.tool?.is_error) return tokens.error;
    return tokens.warning;
  }
  if (row.role === "inject" || row.role === "context_module") return tokens.success;
  if (row.actor === "janitor" || row.actor === "daily") return tokens.tapeJanitor;
  return tokens.border;
}

/** 时间刻度标签：按可视跨度自动换挡（分钟/小时/日期）。 */
function axisTicks(start: number, end: number, width: number): Array<{ x: number; label: string }> {
  const span = end - start;
  if (span <= 0 || width <= 0) return [];
  const target = Math.max(2, Math.floor(width / 110));
  const stepMs = span / target;
  // 候选步长：1m 5m 15m 30m 1h 3h 6h 12h 1d 3d 7d 30d
  const steps = [
    60_000, 300_000, 900_000, 1_800_000, 3_600_000, 10_800_000, 21_600_000, 43_200_000,
    86_400_000, 259_200_000, 604_800_000, 2_592_000_000,
  ];
  const step = steps.find((s) => s >= stepMs) ?? steps[steps.length - 1];
  const ticks: Array<{ x: number; label: string }> = [];
  const first = Math.ceil(start / step) * step;
  const today = new Date();
  const dayMs = 86_400_000;
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  for (let t = first; t < end; t += step) {
    const x = ((t - start) / span) * width;
    if (x < 20 || x > width - 20) continue;
    const d = new Date(t);
    const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const two = (v: number) => String(v).padStart(2, "0");
    let label: string;
    if (step >= dayMs) {
      const diffDays = Math.round((todayStart - dayStart) / dayMs);
      label =
        diffDays === 0
          ? "今天"
          : diffDays === 1
            ? "昨天"
            : diffDays === 2
              ? "前天"
              : `${d.getMonth() + 1}/${d.getDate()}`;
    } else if (step >= 3_600_000) {
      label = `${two(d.getHours())}:00`;
    } else {
      label = `${two(d.getHours())}:${two(d.getMinutes())}`;
    }
    ticks.push({ x, label });
  }
  return ticks;
}

export const TrajectoryTimeline = memo(function TrajectoryTimeline({
  lines,
  visibleStartIndex,
  visibleCount,
  onSeek,
  onNeedOlder,
  hasOlder,
}: TrajectoryTimelineProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => flattenTrajectoryLines(lines), [lines]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  const [win, setWin] = useState<ViewWindow | null>(null);
  const winRef = useRef(win);
  winRef.current = win;
  const [sel, setSel] = useState<{ start: number; end: number } | null>(null);
  const selRef = useRef(sel);
  selRef.current = sel;
  const dragRef = useRef<{ mode: "pan" | "select"; startX: number; startWin: ViewWindow; moved: boolean } | null>(null);

  // 初始窗口：覆盖全部行的时间域（留边），末次行变化时重算
  const fullSpan = useMemo(() => {
    const ts = rows.map((r) => r.ts).filter((t) => t > 0);
    if (ts.length === 0) return null;
    const lo = Math.min(...ts) * 1000;
    const hi = Math.max(...ts) * 1000;
    const span = Math.max(hi - lo, MIN_SPAN_MS);
    const pad = (span * (MAX_SPAN_PAD - 1)) / 2;
    return { start: lo - pad, end: hi + pad };
  }, [rows]);

  useEffect(() => {
    if (!win && fullSpan) setWin(fullSpan);
  }, [fullSpan, win]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    const w = winRef.current;
    if (!canvas || !container || !w) return;
    const dpr = window.devicePixelRatio || 1;
    const width = container.clientWidth;
    if (width <= 0) return;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(HEIGHT * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${HEIGHT}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, HEIGHT);

    const rs = rowsRef.current;
    const span = w.end - w.start;
    if (rs.length === 0 || span <= 0) {
      ctx.fillStyle = tokens.tapeEmpty;
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("暂无轨迹", width / 2, AXIS_Y);
      return;
    }
    const xOf = (ts: number) => ((ts - w.start) / span) * width;

    // 时间刻度标签
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    for (const tick of axisTicks(w.start, w.end, width)) {
      ctx.fillStyle = tokens.textTertiary;
      ctx.fillText(tick.label, tick.x, LABEL_Y);
      ctx.strokeStyle = tokens.tapeBaseline;
      ctx.beginPath();
      ctx.moveTo(tick.x, LABEL_Y + 4);
      ctx.lineTo(tick.x, AXIS_Y - 10);
      ctx.stroke();
    }

    // 基线
    ctx.strokeStyle = tokens.tapeBaseline;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, AXIS_Y);
    ctx.lineTo(width, AXIS_Y);
    ctx.stroke();

    // 刻度点（divider 画贯穿竖刻度）
    for (const row of rs) {
      if (row.ts <= 0) continue;
      const x = xOf(row.ts * 1000);
      if (x < -4 || x > width + 4) continue;
      const color = rowColor(row);
      if (color === "__divider__") {
        ctx.strokeStyle = tokens.tapeDivider;
        ctx.beginPath();
        ctx.moveTo(x, LABEL_Y + 4);
        ctx.lineTo(x, HEIGHT - 4);
        ctx.stroke();
        continue;
      }
      ctx.fillStyle = color || tokens.tapeFallback;
      const r = row.tool?.running ? 3 : 2;
      ctx.beginPath();
      ctx.arc(x, AXIS_Y, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // 刷选区间（拖出来的定位选区）
    if (sel) {
      const sx = xOf(sel.start);
      const ex = xOf(sel.end);
      ctx.fillStyle = tokens.tapeWindowFill;
      ctx.strokeStyle = tokens.tapeWindowStroke;
      ctx.lineWidth = 1.5;
      const rx = Math.max(Math.min(sx, ex), 0);
      const rw = Math.min(Math.max(sx, ex), width) - rx;
      if (rw > 0) {
        ctx.beginPath();
        ctx.roundRect(rx, LABEL_Y + 6, rw, HEIGHT - LABEL_Y - 10, 3);
        ctx.fill();
        ctx.stroke();
      }
    }

    // 主带可见区间窗口块（滚动跟随）
    const firstRow = rs[Math.max(0, Math.min(visibleStartIndex, rs.length - 1))];
    const lastRow = rs[Math.max(0, Math.min(visibleStartIndex + Math.max(visibleCount - 1, 0), rs.length - 1))];
    if (firstRow && lastRow && firstRow.ts > 0) {
      const wx = xOf(firstRow.ts * 1000);
      const ex = xOf((lastRow.ts > 0 ? lastRow.ts : firstRow.ts) * 1000);
      ctx.fillStyle = tokens.tapeWindowFill;
      ctx.strokeStyle = tokens.tapeWindowStroke;
      ctx.lineWidth = 1.5;
      const rx = Math.max(wx, 0);
      const rw = Math.max(Math.min(ex, width) - rx, 6);
      ctx.beginPath();
      ctx.roundRect(rx, 2, rw, HEIGHT - 4, 3);
      ctx.fill();
      ctx.stroke();
    }

    // 左端还有更早录像带的提示
    if (hasOlder) {
      ctx.fillStyle = tokens.tapeArrow;
      ctx.beginPath();
      ctx.moveTo(4, AXIS_Y);
      ctx.lineTo(12, AXIS_Y - 5);
      ctx.lineTo(12, AXIS_Y + 5);
      ctx.closePath();
      ctx.fill();
    }
  }, [hasOlder, sel, visibleCount, visibleStartIndex]);

  useEffect(() => {
    draw();
  }, [draw, lines, win]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(container);
    return () => ro.disconnect();
  }, [draw]);

  const xToTs = useCallback((clientX: number) => {
    const container = containerRef.current;
    const w = winRef.current;
    if (!container || !w) return 0;
    const rect = container.getBoundingClientRect();
    const x = clientX - rect.left;
    return w.start + (x / rect.width) * (w.end - w.start);
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      const w = winRef.current;
      if (!w) return;
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      // 主交互：左键拖 = 平移时间域；Shift+拖 = 刷选区间定位；
      // 原地松开（未拖动）= 单击定位到最近刻度行。
      const mode = e.shiftKey ? "select" : "pan";
      dragRef.current = { mode, startX: e.clientX, startWin: { ...w }, moved: false };
      if (mode === "select") {
        const ts = xToTs(e.clientX);
        setSel({ start: ts, end: ts });
      }
    },
    [xToTs],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = e.clientX - drag.startX;
      if (Math.abs(dx) > 3) drag.moved = true;
      if (drag.mode === "pan") {
        const container = containerRef.current;
        if (!container) return;
        const span = drag.startWin.end - drag.startWin.start;
        const dt = (-dx / container.clientWidth) * span;
        setWin({ start: drag.startWin.start + dt, end: drag.startWin.end + dt });
      } else {
        setSel((prev) => (prev ? { ...prev, end: xToTs(e.clientX) } : prev));
      }
    },
    [xToTs],
  );

  const onPointerUp = useCallback(
    () => {
      const drag = dragRef.current;
      dragRef.current = null;
      const curSel = selRef.current;
      if (!drag) return;
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const localX = drag.startX - rect.left;

      // 左键原地松开（未拖动）：单击 → 最近刻度行（时间距离最近）
      if (drag.mode === "pan" && !drag.moved) {
        // 只有点左端箭头热区才加载更早——以前「索引靠前就 loadOlder」会误触
        if (hasOlder && localX >= 0 && localX < OLDER_HIT_PX) {
          onNeedOlder();
          return;
        }
        const w = winRef.current;
        if (!w || rect.width <= 0) return;
        const ts = w.start + (localX / rect.width) * (w.end - w.start);
        const rs = rowsRef.current;
        let best = -1;
        let bestDt = Infinity;
        rs.forEach((row, i) => {
          if (row.ts <= 0) return;
          const dt = Math.abs(row.ts * 1000 - ts);
          if (dt < bestDt) {
            bestDt = dt;
            best = i;
          }
        });
        if (best >= 0) onSeek(best);
        return;
      }
      // Shift+拖刷选：跳到区间内第一行
      if (drag.mode === "select" && curSel) {
        const lo = Math.min(curSel.start, curSel.end);
        const hi = Math.max(curSel.start, curSel.end);
        setSel(null);
        if (hi - lo < 500) return; // 近点击的刷选不动作
        const rs = rowsRef.current;
        const idx = rs.findIndex((row) => row.ts > 0 && row.ts * 1000 >= lo);
        if (idx >= 0) onSeek(idx);
        return;
      }
    },
    [hasOlder, onNeedOlder, onSeek],
  );

  /** 滚轮缩放：以指针位置为锚点放大缩小时间域 */
  const onWheel = useCallback(
    (e: React.WheelEvent) => {
      e.preventDefault();
      const w = winRef.current;
      if (!w) return;
      const anchor = xToTs(e.clientX);
      const factor = e.deltaY < 0 ? 1 / 1.3 : 1.3;
      const span = w.end - w.start;
      const next = Math.min(Math.max(span * factor, MIN_SPAN_MS), 90 * 86_400_000);
      const ratio = next / span;
      setWin({
        start: anchor - (anchor - w.start) * ratio,
        end: anchor + (w.end - anchor) * ratio,
      });
    },
    [xToTs],
  );

  return (
    <div
      ref={containerRef}
      className="tr-timeline"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onWheel={onWheel}
      onContextMenu={(e) => e.preventDefault()}
      role="slider"
      aria-label="录像带时间线（单击定位最近行，拖动平移，Shift+拖刷选区间，滚轮缩放）"
      aria-valuenow={visibleStartIndex}
      aria-valuemin={0}
      aria-valuemax={rows.length}
      tabIndex={0}
    >
      <canvas ref={canvasRef} />
    </div>
  );
});
