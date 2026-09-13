/** delegate 工具行展开面板的分组（手风琴）判据。
 *
 * 为什么单独一个纯模块：组序、组内计数、默认展开态、「哪一组算有内容」这些是
 * 判断而不是渲染——放在组件里就只能靠肉眼看，抽成纯函数（不引 React、不碰 store）
 * 才能被 selftest 直接断言，也不会随组件重构漂移。
 *
 * 组序固定：任务指令 → 过程 → 最终结果。没有内容的组不出现。
 * 「过程」组装的是子智能体干活途中的全部痕迹，三种来源合成一条时间线：
 *   ① 落带帧（子智能体自己的工具行 / diff，按 view_seq 升序）
 *   ② 活动树里「帧还没有的行」（在跑的工具：帧还没落带，树里已是 ◌）
 *   ③ 过程输出正文（`subagent_chunk` 累积的那段；排最后、不带标签）
 * ①② 是同一件事的两个来源（工作树实时态 vs 落带权威帧），所以按 call 去重、
 * 以帧为准——否则同一个工具会显示两遍（这正是「工作行」与「过程」两组被合并的原因）。
 * 「最终结果」是收官答复，独立成组并带标签。
 */
import { pairDiffsWithTools } from "./diffPairing";
import { isHiddenToolLine } from "./toolVisibility";
import type { ChatMessage } from "./store";
import type { TreeRow } from "./subagentTree";

/** rows 里以 rootId 为根的后代行（不含 rootId 自己）。 */
export function descendantsOf(rows: TreeRow[], rootId: string): TreeRow[] {
  const idx = rows.findIndex((r) => r.nodeId === rootId);
  if (idx < 0) return [];
  const base = rows[idx].depth;
  const out: TreeRow[] = [];
  for (let i = idx + 1; i < rows.length; i++) {
    if (rows[i].depth <= base) break;
    out.push(rows[i]);
  }
  return out;
}

/** delegate 行的候选工作行：后代行里去掉「子智能体自己那一行」——rootId 的直接子行若是
 *  subagent / background 节点，它与本条 delegate 行同义（都在说「派了谁去做」），
 *  重复一遍没有信息量；它们的后代（真正的工具行）保留，按 depth 平铺缩进。 */
export function workRowsOf(rows: TreeRow[], rootId: string): TreeRow[] {
  const root = rows.find((r) => r.nodeId === rootId);
  if (!root) return [];
  const base = root.depth;
  return descendantsOf(rows, rootId).filter(
    (r) => !(r.depth === base + 1 && (r.kind === "subagent" || r.kind === "background")),
  );
}

/** 过程组里的一条：落带帧（工具行 / diff）或活动树补进来的行（帧还没到）。 */
export interface ProcessEntry {
  frame?: ChatMessage;
  row?: TreeRow;
}

/** 过程组条目合并：① 落带帧（调用方已按 view_seq 排好序）在前，② 活动树里
 *  「其 call_id 不在帧集合里」的行按树序补在后。
 *
 *  去重键：帧的工具行带 `tool.tool_call_id`（＝发起它的那次工具调用 id），树行带
 *  `nodeId`（同一次调用）。同一工具两边都有时**只显示一次、以帧为准**：帧是落带的
 *  权威版本（带耗时、最终 ✓/✗），树行只是「帧还没到」时的实时替身。
 *  diff 帧本身没有 call_id（它对应的是同一次 edit/write 调用），因此那次调用的去重
 *  由它的工具行帧完成，diff 只作为额外一条内容块追加，不会造成重复行。 */
export function buildProcessEntries(frames: ChatMessage[], work: TreeRow[]): ProcessEntry[] {
  // 同步点（`delegate wait`）不画：与聊天流同一条规则（见 lib/toolVisibility）。
  // 只在这里过滤 → 计数提示（项数）也随之把它排除，数据本身不动。
  const entries: ProcessEntry[] = frames
    .filter((frame) => !isHiddenToolLine(frame.tool))
    .map((frame) => ({ frame }));
  const seenCalls = new Set<string>();
  for (const frame of frames) {
    const callId = frame.tool?.tool_call_id;
    if (callId) seenCalls.add(String(callId));
  }
  for (const row of work) {
    if (row.nodeId && seenCalls.has(row.nodeId)) continue;
    entries.push({ row });
  }
  // diff 挂到产生它的工具行**紧后面**（按 `tool_call_id` 配对，不靠投递相邻——
  // 两者之间可能插了正文/其他 diff）。同一工具多个 diff 按 view_seq 升序；
  // 没有 call_id 的老帧退回原顺序（不丢）。规则与主消息流共用 lib/diffPairing。
  return pairDiffsWithTools(entries, {
    callId: (e) => String(e.frame?.tool?.tool_call_id || e.frame?.tool_call_id || e.row?.nodeId || ""),
    isTool: (e) => e.frame?.tool !== undefined || e.row !== undefined,
    isDiff: (e) => e.frame?.diff !== undefined,
    seq: (e) => e.frame?.seq,
  });
}

export type ToolLineGroupId = "brief" | "process" | "result";

export interface ToolLineGroup {
  id: ToolLineGroupId;
  title: string;
  /** 标题栏右侧的计数/字数（组存在就一定有值） */
  hint: string;
  /** 默认展开态：任务指令是长文本、需要时才有看，默认收起；其余默认展开 */
  defaultOpen: boolean;
  /** 文本正文：任务指令 / 最终结果各一组；**过程组**的正文是过程输出（`subagent_chunk`
   *  累积的那段），它与过程条目并列展示、由渲染方排在这组最后，不带标签。 */
  text?: string;
  /** 过程组的过程条目（落带帧 + 活动树补给行），仅 `id === "process"` 有值。 */
  entries?: ProcessEntry[];
}

/** 字数提示的紧凑写法（过千用 k）：长指令/长产出不要把标题栏撑开。 */
function formatCharCount(chars: number): string {
  return chars >= 1000 ? `${(chars / 1000).toFixed(1)}k` : `${chars}`;
}

export function buildToolLineGroups(input: {
  brief: string;
  work: TreeRow[];
  frames: ChatMessage[];
  body: string;
  result: string;
}): ToolLineGroup[] {
  const groups: ToolLineGroup[] = [];
  // 判空一律按 trim 后的文本：只有空白等于没有这条内容（与旧面板的判据一致），
  // 但展示时保留原文（指令/产出常带换行与缩进，pre-wrap 原样呈现）。
  const brief = input.brief.trim();
  if (brief) {
    groups.push({
      id: "brief",
      title: "任务指令",
      hint: `${formatCharCount(brief.length)} 字`,
      defaultOpen: false,
      text: input.brief,
    });
  }
  // 过程组＝子智能体干活途中的全部痕迹（落带帧 + 活动树补给行 + 过程正文），
  // 三者任一有内容即算有内容；项数＝条目数（帧数 + 补进来的树行数），
  // 另有正文时追加字数——合成一条提示，同一件事不报两次。
  const body = input.body.trim();
  const entries = buildProcessEntries(input.frames, input.work);
  if (entries.length > 0 || body) {
    const parts: string[] = [];
    if (entries.length > 0) parts.push(`${entries.length} 项`);
    if (body) parts.push(`${formatCharCount(body.length)} 字`);
    groups.push({
      id: "process",
      title: "过程",
      hint: parts.join(" · "),
      defaultOpen: true,
      entries,
      ...(body ? { text: input.body } : {}),
    });
  }
  const result = input.result.trim();
  if (result) {
    groups.push({
      id: "result",
      title: "最终结果",
      hint: `${formatCharCount(result.length)} 字`,
      defaultOpen: true,
      text: input.result,
    });
  }
  return groups;
}
