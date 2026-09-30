/** Thinking 行轮换短语（web 端）——对齐 CLI loading_phrases 抽样/轮换。
 *
 * 词库来自 src/records/loading_phrases.json（打包为静态资源，与 CLI 同源同步）。
 * 有当日定制：定制 N ↔ 常驻抽 N（1:1 上屏）；无定制：常驻抽固定袋 + 小技巧。
 * 60s 窗口顺序轮换；新会话、切工作空间时 reshuffle。
 * daily 定制词经 /api/loading-phrases/custom 下发（页加载 + 每回合开始，后端一日抛）。
 */
import library from "./loading_phrases.json";
import { fetchCustomLoadingPhrases } from "./api";

const WITTY_POOL: string[] = library.witty ?? [];
const QUOTE_POOL: string[] = library.quotes ?? [];

/** 使用提示（对齐 CLI TIPS；web 端快捷键与 CLI 不同，略去终端专属条目）。 */
const TIPS: string[] = [
  "小技巧：Shift+Enter 换行",
  "小技巧：点击左侧 spinner 可停止当前回合",
  "小技巧：顶栏「新会话」开新对话",
  "小技巧：顶栏切换模型",
  "小技巧：/compact 压缩过长会话",
];

const ROTATE_SECONDS = 60;
/** 无定制词时的常驻抽样上限（对齐 CLI `_FALLBACK_RESIDENT`）。 */
const FALLBACK_RESIDENT = 40;

/** 当日 daily 定制词池（后端校验日期后下发；空则批次回落常驻抽样）。 */
let customPool: string[] = [];
let lastRefreshAt = 0;
const REFRESH_DEBOUNCE_MS = 30_000;

function residentPool(): string[] {
  return [...WITTY_POOL, ...QUOTE_POOL];
}

function sampleResident(n: number): string[] {
  const pool = residentPool();
  if (n <= 0) return [];
  if (n >= pool.length) return pool;
  // Fisher–Yates partial shuffle
  const copy = [...pool];
  for (let i = copy.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy.slice(0, n);
}

function shuffleInPlace(arr: string[]): string[] {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function sampleBatch(): string[] {
  // 有定制：定制 N ↔ 常驻抽 N（真 1:1）；无定制：常驻抽固定袋 + 小技巧
  let batch: string[];
  if (customPool.length > 0) {
    batch = [...customPool, ...sampleResident(customPool.length)];
  } else {
    batch = [...sampleResident(FALLBACK_RESIDENT), ...TIPS];
  }
  if (batch.length === 0) return ["思考中……"];
  return shuffleInPlace(batch);
}

let batch: string[] = sampleBatch();

/** /new、切工作空间时换一批（对齐 CLI reshuffle 时机）。 */
export function reshufflePhrases(): void {
  batch = sampleBatch();
}

/** 更新当日定制词并立即重抽一批（后端返回空时清空定制池）。 */
export function setCustomPhrases(phrases: string[]): void {
  const next = Array.isArray(phrases)
    ? phrases.filter((p) => typeof p === "string" && p.trim() !== "")
    : [];
  if (next.join("\u0000") !== customPool.join("\u0000")) {
    customPool = next;
    reshufflePhrases();
  }
}

/** 从后端拉取当日定制词（防抖；静默失败沿用当前池）。 */
export async function refreshCustomPhrases(force = false): Promise<void> {
  const now = Date.now();
  if (!force && now - lastRefreshAt < REFRESH_DEBOUNCE_MS) return;
  if (typeof window === "undefined" || typeof fetch !== "function") return;
  try {
    const data = await fetchCustomLoadingPhrases();
    setCustomPhrases(data.phrases);
    // 成功才计时：模块加载时若 token 未就绪而失败，下一回合会自然重试。
    lastRefreshAt = now;
  } catch {
    // 拉取失败不打扰：保持当前词池
  }
}

// 页面加载即拉一次（覆盖 daily 已写定制词、页面后打开的场景）。
if (typeof window !== "undefined" && typeof fetch === "function") {
  void refreshCustomPhrases(true);
}

/** 按墙钟每 60s 轮换一条（无状态，可测试）。 */
export function currentPhrase(nowSeconds?: number): string {
  const t = nowSeconds ?? Date.now() / 1000;
  return batch[Math.floor(t / ROTATE_SECONDS) % batch.length];
}
