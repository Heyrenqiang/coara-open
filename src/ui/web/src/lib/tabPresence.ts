import { tokenQuery } from "./auth";

/**
 * Tab presence — 告诉内核「这个标签还在」，并盯住前端构建有没有更新。
 *
 * 为什么需要：打开/唤起 Web UI 的判定要看「浏览器里有没有标签」，而 WebSocket 会
 * 因为内核重启、标签被浏览器后台节流而断掉，只凭 WS 会误判成「没有标签」，于是
 * 又开一个新标签（新标签随即被单标签守卫关掉）。这里补一条低频 HTTP 心跳：
 *
 * - 可见时 30s 一次、隐藏时 60s 一次（后台 fetch 会被节流，继续发即可）
 * - 页面重新可见 / 窗口获得焦点 / 从 bfcache 恢复（pageshow）时立刻补一次——被唤醒即报到
 * - 关闭标签时用 sendBeacon 发一次 bye，内核据此立刻知道标签离开（不必等 TTL）
 *
 * 心跳响应上还带着内核当前的前端构建指纹（`X-Coara-UI-Build`）。标签页长驻时跑的是
 * 「加载那一刻」的 JS，前端重新构建后不会自动生效 —— 指纹一变就在安全时机自动重载，
 * 省掉手动强制刷新（开发期改完前端等几秒即可）。
 *
 * 心跳只是信号，失败一律静默：它不该影响任何界面行为。
 */

/** 可见时的心跳间隔。 */
const VISIBLE_PING_MS = 30_000;
/** 隐藏时的心跳间隔（浏览器会进一步节流，能发出去就行）。 */
const HIDDEN_PING_MS = 60_000;
/** 内核回传前端构建指纹的响应头。 */
const UI_BUILD_HEADER = "x-coara-ui-build";
/** 发现新构建后的复查节奏：等到安全时机就重载。 */
const RELOAD_RETRY_MS = 5_000;

/** 本页面加载时的构建指纹（首次心跳取得），null 表示还没拿到。 */
let baselineBuild: string | null = null;
/** 已发现新构建、等待安全时机重载。 */
let pendingReload = false;

function readUiBuild(res: Response): string | null {
  const value = res.headers.get(UI_BUILD_HEADER)?.trim();
  return value ? value : null;
}

/** 最近一次用户交互（点击 / 按键 / 滚动）的时间戳：前台要等它静默一会儿再重载，
 *  免得把正在读的内容顶掉。 */
let lastInteractionAt = Date.now();

function markInteraction(): void {
  lastInteractionAt = Date.now();
}

/** 什么时候允许自动重载：
 *  - 光标在输入框 / 可编辑区：绝不重载（会吞掉还没发出去的草稿）
 *  - 页面在后台：立即重载（用户看不到，切回来就是新版，零打扰）
 *  - 页面在前台：等用户安静 20 秒再换，避免把正在读的内容顶掉 */
function canReloadSafely(): boolean {
  if (typeof document === "undefined") return false;
  const active = document.activeElement as HTMLElement | null;
  const tag = active?.tagName?.toLowerCase() ?? "";
  if (tag === "input" || tag === "textarea") return false;
  if (active?.isContentEditable) return false;
  if (document.visibilityState !== "visible") return true;
  return Date.now() - lastInteractionAt > 20_000;
}

export function startTabPresence(): () => void {
  let timer: number | null = null;
  let reloadTimer: number | null = null;
  let stopped = false;

  const watchForReload = () => {
    if (stopped) return;
    if (pendingReload && canReloadSafely()) {
      window.location.reload();
      return;
    }
    reloadTimer = window.setTimeout(watchForReload, RELOAD_RETRY_MS);
  };

  const ping = () => {
    if (stopped) return;
    try {
      void fetch(`/api/ui/presence/ping${tokenQuery()}`, {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
      })
        .then((res) => {
          const build = readUiBuild(res);
          if (!build) return;
          if (baselineBuild === null) {
            baselineBuild = build;
            return;
          }
          if (build !== baselineBuild && !pendingReload) {
            pendingReload = true;
            watchForReload();
          }
        })
        .catch(() => {});
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
  // 交互打点：前台自动重载要等用户安静下来（见 canReloadSafely）
  window.addEventListener("pointerdown", markInteraction, { passive: true });
  window.addEventListener("keydown", markInteraction, { passive: true });
  window.addEventListener("wheel", markInteraction, { passive: true });

  return () => {
    stopped = true;
    if (timer !== null) window.clearTimeout(timer);
    if (reloadTimer !== null) window.clearTimeout(reloadTimer);
    document.removeEventListener("visibilitychange", onWake);
    window.removeEventListener("focus", onWake);
    window.removeEventListener("pageshow", onWake);
    window.removeEventListener("pagehide", onLeave);
    window.removeEventListener("pointerdown", markInteraction);
    window.removeEventListener("keydown", markInteraction);
    window.removeEventListener("wheel", markInteraction);
  };
}
