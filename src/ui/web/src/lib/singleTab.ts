/**
 * Single-tab arbitration for the Web UI.
 *
 * 同一浏览器同源下只保留一个「活着」的 WebUI 标签：后来者自我退出；退出前广播
 * focus，让主标签置前——配合服务端「有活跃连接不开新窗」，减少闪一下。
 *
 * 僵死标签（被挤掉 / 无 token）标 ``live: false``，不占坑。
 * 带 token 新开的标签标 ``freshOpen: true``：优先接管，旧标签（哪怕还在重连）
 * 必须让路——否则用户会看到被抬起的旧「断开」页，新开页被挤掉。
 *
 * BroadcastChannel 不支持时静默跳过（单例保护失效但功能不受影响）。
 */

interface TabPing {
  type: "hello" | "ack" | "focus";
  ts?: number;
  id?: string;
  /** false = 本标签已放弃连接（superseded / 无 token），不参与占坑 */
  live?: boolean;
  /** true = 本加载从入口 URL 吃到了新 token（托盘/自动打开） */
  freshOpen?: boolean;
}

const CHANNEL_NAME = "coara-webui-tab";

function tryFocusWindow(): void {
  try {
    window.focus();
  } catch {
    /* ignore */
  }
}

export type SingleTabArbiterOptions = {
  /**
   * 本标签是否仍在占坑。默认 true。
   * 应在「已放弃自动重连」（superseded / 鉴权失败 / 无 token）时返回 false；
   * 启动中尚未连上 WS 仍应返回 true，以免新标签误让路。
   */
  isClaimingLive?: () => boolean;
  /** 本加载是否入口带 token 新开（优先接管）。 */
  isFreshOpen?: () => boolean;
};

/**
 * 注册单例仲裁器。返回清理函数；onDuplicate 在判定本标签为后来者时触发。
 */
export function createSingleTabArbiter(
  onDuplicate: () => void,
  options?: SingleTabArbiterOptions,
): () => void {
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(CHANNEL_NAME);
  } catch {
    return () => {};
  }

  const claimingLive = () => {
    try {
      return options?.isClaimingLive?.() !== false;
    } catch {
      return true;
    }
  };

  const freshOpen = () => {
    try {
      return options?.isFreshOpen?.() === true;
    } catch {
      return false;
    }
  };

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
    const otherLive = d.live !== false;
    const otherFresh = d.freshOpen === true;

    if (d.type === "hello") {
      // 后来者是入口新开：旧标签一律让路（含「还在重连」的占坑旧页）
      if (otherFresh && !isEarlier(d)) {
        fire();
        return;
      }
      if (isEarlier(d) && otherLive) {
        // 对方更早且占坑：我若是新开则留下（对方收到我的 freshOpen hello 会让路）
        if (freshOpen()) return;
        fire();
      } else if (isEarlier(d) && !otherLive) {
        return;
      } else if (!claimingLive()) {
        fire();
      } else {
        channel!.postMessage({
          type: "ack",
          ts: myTs,
          id: myId,
          live: true,
          freshOpen: freshOpen(),
        } satisfies TabPing);
      }
    } else if (d.type === "ack") {
      if (isEarlier(d) && otherLive && !freshOpen()) fire();
    }
  };

  channel.postMessage({
    type: "hello",
    ts: myTs,
    id: myId,
    live: claimingLive(),
    freshOpen: freshOpen(),
  } satisfies TabPing);

  return () => {
    try {
      channel?.close();
    } catch {
      /* ignore */
    }
  };
}
