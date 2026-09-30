// 消息行处理纯函数（从 store.ts 抽出：无状态、不碰 store，只读写入参行数组）。
// store.ts 经 re-export 保持既有 import 路径不变。

import type { CanonicalDiffLines, CommandResult } from "./ws";
import type { ChatFileAttachment, ChatMessage, ChatToolLine } from "./chatTypes";

/** 时间线标签：今天/昨天/M月d日；当天也带「今天」防跨天歧义。 */
export function formatTimelineTimeLabel(ms: number): string {
  const d = new Date(ms);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (sameDay(now, d)) return `今天 ${hm}`;
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameDay(yesterday, d)) return `昨天 ${hm}`;
  const weekday = ["日", "一", "二", "三", "四", "五", "六"][d.getDay()];
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  if (now.getFullYear() === d.getFullYear()) return `${md} 周${weekday} ${hm}`;
  return `${d.getFullYear()}年${md} 周${weekday} ${hm}`;
}

/** 手机端同款融合：会话/模型切换分隔线与时间隔断合并到一行（「新会话  09:12」）。
 *  分隔线时间取「下一行的内容时间」——即分隔发生的位置，而非分隔帧写入时间
 *  （系统深夜写分隔帧时后者会失真）。后端已下发 divider_time 时直接用。 */
export function withTimelineTimes(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m) => {
    if (!m.dividerLabel) return m;
    const explicit = m.dividerTime ?? "";
    if (explicit) return { ...m, dividerTime: explicit };
    if (m.dividerTs && m.dividerTs > 0) {
      return { ...m, dividerTime: formatTimelineTimeLabel(m.dividerTs * 1000) };
    }
    return m;
  });
}

/** 相邻分隔线融合：一条分隔线紧跟着另一条时，只留信息更全的一条
 *  （连续 /new 不产生两行紧贴的分隔线）。 */
export function mergeAdjacentDividers(messages: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (m.dividerLabel && prev?.dividerLabel) {
      // 后到的标签信息更新，覆盖前者；时间保留后者（更近）
      out[out.length - 1] = {
        ...m,
        dividerTime: m.dividerTime ?? prev.dividerTime,
      };
      continue;
    }
    out.push(m);
  }
  return out;
}

/** 向前翻页一页的行数：与服务端快照窗口同一把尺（loadEarlier 每次取一页，
 *  「返回不足一页」＝历史到头的判据；hydrate 满页 ⇒ 头部之前还有更早历史）。 */
export const HISTORY_PAGE_LIMIT = 200;

/** hydrate 快照融合（手机端 buildChatListItems 同款）：
 *  连续相邻的分隔线合并为一条；分隔线缺时间标签时取下一行的内容时间
 *  （分隔发生的位置），有则沿用后端下发的 divider_time。 */
export function fuseDividersWithTime(messages: ChatMessage[]): ChatMessage[] {
  return mergeAdjacentDividers(withTimelineTimes(messages));
}

/** Map model/workspace switch command results to phone-style divider labels.
 *  Other slash outputs stay as the monospace command card. */
export function timelineDividerLabelFromCommand(result: CommandResult): string | null {
  const data = (result?.data ?? {}) as Record<string, unknown>;
  const output = String(result?.output ?? "").trim();
  const provider = typeof data.provider === "string" ? data.provider.trim() : "";
  const model = typeof data.model === "string" ? data.model.trim() : "";
  const isModelAck =
    output.startsWith("已切换 →") ||
    output.startsWith("已切换 ->") ||
    output.startsWith("已标记切换 →") ||
    output.startsWith("已标记切换 ->");
  if (isModelAck) {
    const key =
      provider && model
        ? `${provider}·${model}`
        : output
            .replace(/^已标记切换\s*(?:→|->)\s*/, "")
            .replace(/^已切换\s*(?:→|->)\s*/, "")
            .replace(/\s*[（(].*$/, "")
            .trim()
            .replace(/\//g, "·");
    if (!key) return null;
    return data.deferred ? `将切换模型 ${key}` : `已切换模型 ${key}`;
  }
  if (result?.action === "switch_workspace") {
    const name = typeof data.name === "string" ? data.name.trim() : "";
    if (name) return `已切换到工作空间 ${name}`;
  }
  return null;
}

/** FNV-1a 32-bit：消息内容身份的轻量哈希（同步、无加密需求，碰撞域内
 *  由 reconcile 的组内下标双射消歧——碰撞只导致错误配对，不导致崩溃）。 */
export function fnv1a(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

function boundedJson(value: unknown): string {
  try {
    const s = JSON.stringify(value);
    return typeof s === "string" ? s : "";
  } catch {
    return "";
  }
}

/** diff 行的轻量身份：路径 / 增删计数 / 首 hunk 前 512 字符。
 *  原先这里是 `JSON.stringify(diff)`——大 diff 每次落定都要序列化整份文本，
 *  在「每帧一行」的热路径上是不必要的分配与 GC 压力。 */
export function diffIdentity(diff: CanonicalDiffLines): string {
  const hunks = Array.isArray(diff.hunks) ? (diff.hunks as unknown[]) : [];
  // 规范形态是「hunk 的数组的数组」；非规范/未来变体按平铺处理，照样给得出身份
  const head = hunks.length > 0 && Array.isArray(hunks[0]) ? (hunks[0] as unknown[]) : hunks;
  let sample = "";
  for (const entry of head) {
    if (sample.length >= 512) break;
    const line = entry as { kind?: unknown; oldNum?: unknown; newNum?: unknown; code?: unknown };
    if (line && typeof line === "object" && typeof line.code === "string") {
      sample += `${String(line.kind)}${String(line.oldNum)},${String(line.newNum)}:${line.code}`;
    } else {
      // 兜底：只序列化这一条（有界），不序列化整份 diff
      sample += boundedJson(line).slice(0, 128);
    }
  }
  return `${diff.path}|${diff.added}|${diff.removed}|${hunks.length}|${sample.slice(0, 512)}`;
}

/** 素材规则（双侧严格同源）： - 分隔线 `d|<label>`：同 label 即同一逻辑实体（融合的多对一塌缩天然吸收） - diff 块 `f|c:<tool_call_id>`（无 id 时退化为轻量结构哈希） - 用户气泡 `u|<trim 文本>|<附件 */
export function computeMsgKey(m: {
  role: "user" | "assistant";
  text: string;
  diff?: CanonicalDiffLines;
  /** 产出这条 diff 的工具调用 id（身份首选：同一次调用唯一，双侧同源） */
  tool_call_id?: string;
  dividerLabel?: string;
  attachments?: ChatFileAttachment[];
  tool?: ChatToolLine;
}): string {
  let raw: string;
  if (m.dividerLabel) {
    raw = `d|${m.dividerLabel}`;
  } else if (m.diff) {
    raw = `f|${m.tool_call_id ? `c:${m.tool_call_id}` : diffIdentity(m.diff)}`;
  } else if (m.tool) {
    raw = `t|${m.tool.label}|${m.tool.ok ? 1 : 0}`;
  } else if (m.role === "user") {
    const refs = (m.attachments ?? []).map((a) => a.file_id || a.filename).join(",");
    raw = `u|${m.text.trim()}|${refs}`;
  } else {
    raw = `a|${m.text.trim()}`;
  }
  return `k:${fnv1a(raw)}`;
}

// Random UUIDs keep ids unique across HMR reloads (module state is reset,
export function nextId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? `msg-${crypto.randomUUID()}`
    : `msg-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** 内容行 `s<view_seq>`、工具行 `t<tool_call_id>`、diff 行 `d<tool_call_id>`、 已带端上标识的用户行 `c<client_msg_id>`；本地 uuid（`l<id> */
export function chatRowKey(m: ChatMessage): string {
  if (m.seq !== undefined) return `s${m.seq}`;
  const callId = String(m.tool?.tool_call_id || m.tool_call_id || "");
  if (callId) return `${m.tool ? "t" : "d"}${callId}`;
  if (m.clientMsgId) return `c${m.clientMsgId}`;
  return `l${m.id}`;
}

export function nextTraceId(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? `tr-${crypto.randomUUID()}`
    : `tr-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function nowISO(): string {
  return new Date().toISOString();
}
