/**
 * ModuleChatFloat — 模块级独立会话浮动窗。
 *
 * 收起态：coara logo 圆形 FAB — 短按打开；长按后拖动、松手不打开。
 * 展开态：拖动标题栏移动、右下角 resize。
 * 点击面板外收回 FAB。
 */

import { useCallback, useEffect, useRef, useState, Suspense, lazy, type PointerEvent as ReactPointerEvent } from "react";
import { Button, Tooltip } from "antd";
import { MinusOutlined, MessageOutlined, LoadingOutlined } from "@ant-design/icons";
import { useStore } from "../../lib/store";
import "./module-chat.css";

const ModuleChatPanel = lazy(() =>
  import("../chat/ModuleChatPanel").then((m) => ({ default: m.ModuleChatPanel })),
);

const DRAG_THRESHOLD_PX = 5;
/** FAB 按住超过该时长后才进入拖动（短按松开 = 打开） */
const FAB_LONG_PRESS_MS = 200;
const PANEL_DEFAULT = { w: 400, h: 520 };

interface FloatState {
  mode: "open" | "collapsed";
  x: number;
  y: number;
  w: number;
  h: number;
}

const DEFAULT_POS = { x: 16, y: 72 };

function loadState(key: string): FloatState {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { mode: "collapsed", ...DEFAULT_POS, ...PANEL_DEFAULT };
    const p = JSON.parse(raw);
    return {
      mode: "collapsed",
      x: typeof p.x === "number" ? p.x : DEFAULT_POS.x,
      y: typeof p.y === "number" ? p.y : DEFAULT_POS.y,
      w: typeof p.w === "number" ? Math.max(320, p.w) : PANEL_DEFAULT.w,
      h: typeof p.h === "number" ? Math.max(280, p.h) : PANEL_DEFAULT.h,
    };
  } catch {
    return { mode: "collapsed", ...DEFAULT_POS, ...PANEL_DEFAULT };
  }
}

function saveState(key: string, s: FloatState) {
  try {
    localStorage.setItem(key, JSON.stringify(s));
  } catch { /* ignore */ }
}

function clampPos(x: number, y: number, w: number, h: number) {
  const maxX = Math.max(8, window.innerWidth - 80);
  const maxY = Math.max(8, window.innerHeight - 48);
  return {
    x: Math.min(Math.max(8, x), maxX),
    y: Math.min(Math.max(8, y), maxY),
    w: Math.min(Math.max(320, w), window.innerWidth - 16),
    h: Math.min(Math.max(280, h), window.innerHeight - 16),
  };
}

interface ModuleChatFloatProps {
  subject: string;
  title: string;
  emptyHint?: string;
}

export function ModuleChatFloat({ subject, title, emptyHint }: ModuleChatFloatProps) {
  const storageKey = `coara.moduleChat.float.${subject}`;
  const openEvent = `coara-module-chat-open-${subject}`;
  const [state, setState] = useState<FloatState>(() => loadState(storageKey));
  const panelRef = useRef<HTMLDivElement | null>(null);
  const turnActive = useStore((s) => s.moduleSessions[subject]?.turnActive ?? false);
  const dragRef = useRef<{ ox: number; oy: number; sx: number; sy: number; moved: boolean } | null>(null);
  const resizeRef = useRef<{ ox: number; oy: number; sw: number; sh: number } | null>(null);
  const draggingRef = useRef(false);

  const patch = useCallback(
    (partial: Partial<FloatState>) => {
      setState((prev) => {
        const next = { ...prev, ...partial };
        const clamped = clampPos(next.x, next.y, next.w, next.h);
        const saved = { ...next, ...clamped };
        saveState(storageKey, saved);
        return saved;
      });
    },
    [storageKey],
  );

  useEffect(() => {
    const onOpen = () => patch({ mode: "open" });
    window.addEventListener(openEvent, onOpen);
    return () => window.removeEventListener(openEvent, onOpen);
  }, [patch, openEvent]);

  // 点击面板外收回 FAB（拖动过程中不收起）
  useEffect(() => {
    if (state.mode !== "open") return;
    const onDown = (e: PointerEvent) => {
      if (draggingRef.current) return;
      const root = panelRef.current;
      if (root && e.target instanceof Node && !root.contains(e.target)) {
        patch({ mode: "collapsed" });
      }
    };
    window.addEventListener("pointerdown", onDown, true);
    return () => window.removeEventListener("pointerdown", onDown, true);
  }, [state.mode, patch]);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (dragRef.current) {
        const d = dragRef.current;
        const dx = e.clientX - d.ox;
        const dy = e.clientY - d.oy;
        if (!d.moved && Math.hypot(dx, dy) >= DRAG_THRESHOLD_PX) {
          d.moved = true;
          draggingRef.current = true;
        }
        if (d.moved) patch({ x: d.sx + dx, y: d.sy + dy });
      } else if (resizeRef.current) {
        draggingRef.current = true;
        const r = resizeRef.current;
        patch({ w: r.sw + (e.clientX - r.ox), h: r.sh + (e.clientY - r.oy) });
      }
    };
    const onUp = () => {
      dragRef.current = null;
      resizeRef.current = null;
      // 延迟清零，避免 pointerup 后误触 click
      requestAnimationFrame(() => {
        draggingRef.current = false;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [patch]);

  const beginPanelDrag = (e: ReactPointerEvent) => {
    if ((e.target as HTMLElement).closest("button")) return;
    dragRef.current = { ox: e.clientX, oy: e.clientY, sx: state.x, sy: state.y, moved: false };
  };

  const onFabPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLButtonElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();

      const el = e.currentTarget;
      el.setPointerCapture(e.pointerId);

      const ox = e.clientX;
      const oy = e.clientY;
      const sx = state.x;
      const sy = state.y;
      let armed = false;
      let dragged = false;

      const arm = () => {
        armed = true;
        el.classList.add("flow-chat-fab-dragging");
      };

      const longPressTimer = window.setTimeout(arm, FAB_LONG_PRESS_MS);

      const onMove = (ev: PointerEvent) => {
        if (!armed) return;
        const dx = ev.clientX - ox;
        const dy = ev.clientY - oy;
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
        dragged = true;
        draggingRef.current = true;
        const clamped = clampPos(sx + dx, sy + dy, state.w, state.h);
        setState((prev) => ({ ...prev, x: clamped.x, y: clamped.y }));
      };

      const onUp = (ev: PointerEvent) => {
        window.clearTimeout(longPressTimer);
        el.classList.remove("flow-chat-fab-dragging");
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        try {
          el.releasePointerCapture(ev.pointerId);
        } catch {
          /* already released */
        }

        const dist = Math.hypot(ev.clientX - ox, ev.clientY - oy);
        if (dragged) {
          setState((prev) => {
            const saved = { ...prev, ...clampPos(prev.x, prev.y, prev.w, prev.h) };
            saveState(storageKey, saved);
            return saved;
          });
          requestAnimationFrame(() => {
            draggingRef.current = false;
          });
        } else if (!armed && dist < DRAG_THRESHOLD_PX) {
          patch({ mode: "open" });
        }
      };

      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    },
    [state.x, state.y, state.w, state.h, storageKey, patch],
  );

  if (state.mode === "collapsed") {
    return (
      <button
        type="button"
        className="flow-chat-fab"
        style={{ left: state.x, top: state.y }}
        onPointerDown={onFabPointerDown}
      >
        <img src="/static/dist/coara-logo.png" alt={title} className="flow-chat-fab-logo" />
        {turnActive && <LoadingOutlined className="flow-chat-fab-spin" />}
      </button>
    );
  }

  return (
    <div
      ref={panelRef}
      className="flow-chat-float"
      style={{ left: state.x, top: state.y, width: state.w, height: state.h }}
    >
      <div className="flow-chat-float-bar" onPointerDown={beginPanelDrag}>
        <div className="flow-chat-float-title">
          <MessageOutlined />
          <span>{title}</span>
          {turnActive && <LoadingOutlined style={{ fontSize: 12, color: "var(--coara-accent)" }} />}
        </div>
        <div className="flow-chat-float-actions">
          <Tooltip title="收起">
            <Button type="text" size="small" icon={<MinusOutlined />} onClick={() => patch({ mode: "collapsed" })} />
          </Tooltip>
        </div>
      </div>
      <div className="flow-chat-float-body">
        <Suspense fallback={<div style={{ padding: 16, color: "var(--coara-text-faint)" }}>加载…</div>}>
          <ModuleChatPanel subject={subject} emptyHint={emptyHint} />
        </Suspense>
      </div>
      <div
        className="flow-chat-float-resize"
        onPointerDown={(e) => {
          e.stopPropagation();
          resizeRef.current = { ox: e.clientX, oy: e.clientY, sw: state.w, sh: state.h };
        }}
      />
    </div>
  );
}
