/** Thinking 行轮换短语（web 端）——复刻 CLI witty_phrases 的采样/轮换逻辑。
 *
 * 词库来自 src/records/loading_phrases.json（打包为静态资源，与 CLI 同源同步）。
 * 整池洗牌袋对齐 CLI：常驻（witty+quotes）全量 + daily 定制 + 小技巧一并进袋，
 * 60s 窗口顺序轮换（往一轮穷尽 ≈6 小时）；新会话、切工作空间时 reshuffle 换一袋。
 * daily 定制词（一日抛）经后端 /api/loading-phrases/custom 下发：页面加载与
 * 每回合开始时拉取，日期校验由后端做，过期自动回落常驻词库。
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

// 每批＝整池洗牌袋（对齐 CLI）：全池打乱后顺序播放，穷尽一遍才重复。
const ROTATE_SECONDS = 60;

/** 当日 daily 定制词池（后端校验日期后下发；空则批次回落常驻词库）。 */
let customPool: string[] = [];
let lastRefreshAt = 0;
const REFRESH_DEBOUNCE_MS = 30_000;

function sampleBatch(): string[] {
  // 整池洗牌袋：全池打乱后顺序播放，穷尽一遍才重复（≈6 小时），展现在
  // 用户面前的就是随机序列，而不是「同一小批反复循环」。
  const batch = [...WITTY_POOL, ...QUOTE_POOL, ...customPool, ...TIPS];
  for (let i = batch.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [batch[i], batch[j]] = [batch[j], batch[i]];
  }
  return batch.length > 0 ? batch : ["思考中……"];
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
