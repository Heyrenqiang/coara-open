import { tokenQuery } from "./auth";

/**
 * Tab presence — 告诉内核「这个标签还在」。
 *
 * 为什么需要：打开/唤起 Web UI 的判定要看「浏览器里有没有标签」，而 WebSocket 会
 * 因为内核重启、标签被浏览器后台节流而断掉，只凭 WS 会误判成「没有标签」，于是
 * 又开一个新标签（新标签随即被单标签守卫关掉）。这里补一条低频 HTTP 心跳：
 *
 * - 可见时 30s 一次、隐藏时 60s 一次（后台 fetch 会被节流，继续发即可）
 * - 页面重新可见 / 窗口获得焦点 / 从 bfcache 恢复（pageshow）时立刻补一次——被唤醒即报到
 * - 关闭标签时用 sendBeacon 发一次 bye，内核据此立刻知道标签离开（不必等 TTL）
 *
 * 心跳只是信号，失败一律静默：它不该影响任何界面行为，也不整页刷新。
 * 整页刷新会拆掉当前 WebSocket，进行中的对话会断。前端构建更新留给用户自己刷新。
 */

/** 可见时的心跳间隔。 */
const VISIBLE_PING_MS = 30_000;
/** 隐藏时的心跳间隔（浏览器会进一步节流，能发出去就行）。 */
const HIDDEN_PING_MS = 60_000;

export function startTabPresence(): () => void {
  let timer: number | null = null;
  let stopped = false;

  const ping = () => {
    if (stopped) return;
    try {
      void fetch(`/api/ui/presence/ping${tokenQuery()}`, {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
      }).catch(() => {});
    } catch {
      /* ignore */
    }
  };

  const schedule = () => {
    if (stopped) return;
    if (timer !== null) window.clearTimeout(timer);
    const delay = document.visibilityState === "hidden" ? HIDDEN_PING_MS : VISIBLE_PING_MS;
    timer = window.setTimeout(() => {
      ping();
      schedule();
    }, delay);
  };

  const onWake = () => {
    ping();
    schedule();
  };

  const onLeave = () => {
    try {
      const url = `/api/ui/presence/bye${tokenQuery()}`;
      if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
        navigator.sendBeacon(url);
      } else {
        void fetch(url, { method: "POST", keepalive: true }).catch(() => {});
      }
    } catch {
      /* ignore */
    }
  };

  ping();
  schedule();
  document.addEventListener("visibilitychange", onWake);
  window.addEventListener("focus", onWake);
  window.addEventListener("pageshow", onWake);
  window.addEventListener("pagehide", onLeave);

  return () => {
    stopped = true;
    if (timer !== null) window.clearTimeout(timer);
    document.removeEventListener("visibilitychange", onWake);
    window.removeEventListener("focus", onWake);
    window.removeEventListener("pageshow", onWake);
    window.removeEventListener("pagehide", onLeave);
  };
}
