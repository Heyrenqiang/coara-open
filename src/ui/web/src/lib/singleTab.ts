/**
 * Single-tab arbitration for the Web UI.
 *
 * 同一浏览器同源下只保留一个 WebUI 标签：后来者自我退出；退出前广播
 * focus，让主标签置前——配合服务端「有活跃连接不开新窗」，减少闪一下。
 *
 * BroadcastChannel 不支持时静默跳过（单例保护失效但功能不受影响）。
 */

interface TabPing {
  type: "hello" | "ack" | "focus";
  ts?: number;
  id?: string;
}

const CHANNEL_NAME = "coara-webui-tab";

function tryFocusWindow(): void {
  try {
    window.focus();
  } catch {
    /* ignore */
  }
}

/**
 * 注册单例仲裁器。返回清理函数；onDuplicate 在判定本标签为后来者时触发。
 */
export function createSingleTabArbiter(onDuplicate: () => void): () => void {
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(CHANNEL_NAME);
  } catch {
    return () => {};
  }

  const myTs = Date.now();
  const myId =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${myTs}-${Math.random().toString(36).slice(2)}`;

  const isEarlier = (d: TabPing) =>
    typeof d.ts === "number" &&
    typeof d.id === "string" &&
    (d.ts < myTs || (d.ts === myTs && d.id < myId));

  let fired = false;
  const fire = () => {
    if (fired) return;
    fired = true;
    // 先请主标签置前，再让本标签退出（减轻「开一下又关」的体感）。
    try {
      channel?.postMessage({ type: "focus" } satisfies TabPing);
    } catch {
      /* ignore */
    }
    onDuplicate();
  };

  channel.onmessage = (e: MessageEvent) => {
    const d = e.data as TabPing | undefined;
    if (!d || typeof d !== "object" || typeof d.type !== "string") {
      return;
    }
    if (d.type === "focus") {
      tryFocusWindow();
      return;
    }
    if (typeof d.ts !== "number" || typeof d.id !== "string") {
      return;
    }
    if (d.type === "hello") {
      if (isEarlier(d)) {
        fire(); // 对方更早 → 我退出
      } else {
        channel!.postMessage({ type: "ack", ts: myTs, id: myId } satisfies TabPing);
      }
    } else if (d.type === "ack") {
      if (isEarlier(d)) fire();
    }
  };

  channel.postMessage({ type: "hello", ts: myTs, id: myId } satisfies TabPing);

  return () => {
    try {
      channel?.close();
    } catch {
      /* ignore */
    }
  };
}
