/** delegate 展开手风琴：组序 brief→process→result；空组不出现。过程＝落带帧+树行补给+正文。 */
import { pairDiffsWithTools } from "./diffPairing";
import { isHiddenToolLine } from "./toolVisibility";
import type { ChatMessage } from "./store";
import type { TreeRow } from "./subagentTree";

/** 派生结果缓存（键＝rows 数组引用）：SubagentTreeTracker.rows() 在结构未变时返回
 *  同一个数组，于是这里的缓存命中——否则每个工具行每次树变动都要全表扫一遍。
 *  rows 一律不可变（树每次结构变化都重建新数组），缓存不会拿到过期结果。 */
const _descCache = new WeakMap<TreeRow[], Map<string, TreeRow[]>>();
const _childCache = new WeakMap<TreeRow[], Map<string, TreeRow[]>>();
const _workCache = new WeakMap<TreeRow[], Map<string, TreeRow[]>>();
const _runningCache = new WeakMap<TreeRow[], Map<string, boolean>>();

function _cachedRows(
  cache: WeakMap<TreeRow[], Map<string, TreeRow[]>>,
  rows: TreeRow[],
  rootId: string,
  build: () => TreeRow[],
): TreeRow[] {
  let byId = cache.get(rows);
  if (!byId) {
    byId = new Map<string, TreeRow[]>();
    cache.set(rows, byId);
  }
  const hit = byId.get(rootId);
  if (hit !== undefined) return hit;
  const built = build();
  byId.set(rootId, built);
  return built;
}

function descendantsOfUncached(rows: TreeRow[], rootId: string): TreeRow[] {
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

/** rootId 的后代行（不含自身）。 */
export function descendantsOf(rows: TreeRow[], rootId: string): TreeRow[] {
  return _cachedRows(_descCache, rows, rootId, () => descendantsOfUncached(rows, rootId));
}

function directChildRowsUncached(rows: TreeRow[], parentId: string): TreeRow[] {
  const idx = rows.findIndex((r) => r.nodeId === parentId);
  if (idx < 0) return [];
  const base = rows[idx].depth;
  const out: TreeRow[] = [];
  for (let i = idx + 1; i < rows.length; i++) {
    const row = rows[i];
    if (row.depth <= base) break;
    if (row.depth === base + 1) out.push(row);
  }
  return out;
}

/** parentId 的直接子行——编排的「节点层」就是它。 */
export function directChildRows(rows: TreeRow[], parentId: string): TreeRow[] {
  return _cachedRows(_childCache, rows, parentId, () => directChildRowsUncached(rows, parentId));
}

function workRowsOfUncached(rows: TreeRow[], rootId: string): TreeRow[] {
  const root = rows.find((r) => r.nodeId === rootId);
  if (!root) return [];
  const base = root.depth;
  return descendantsOfUncached(rows, rootId).filter(
    (r) => !(r.depth === base + 1 && (r.kind === "subagent" || r.kind === "background")),
  );
}

/** 工作行：后代去掉直接子 subagent/background（与本 delegate 同义）。 */
export function workRowsOf(rows: TreeRow[], rootId: string): TreeRow[] {
  return _cachedRows(_workCache, rows, rootId, () => workRowsOfUncached(rows, rootId));
}

/** 本 call 或其后代仍 active ⇒ 呼吸点亮（结果按 rows 引用 + callId 缓存）。 */
export function toolRunning(rows: TreeRow[], callId: string): boolean {
  if (callId === "") return false;
  let byId = _runningCache.get(rows);
  if (!byId) {
    byId = new Map<string, boolean>();
    _runningCache.set(rows, byId);
  }
  const hit = byId.get(callId);
  if (hit !== undefined) return hit;
  const running =
    rows.some((r) => r.nodeId === callId && r.active) ||
    descendantsOfUncached(rows, callId).some((r) => r.active);
  byId.set(callId, running);
  return running;
}

export interface ProcessEntry {
  frame?: ChatMessage;
  row?: TreeRow;
}

/** 过程条目：帧在前（按 call 去重以帧为准），树行 call 不在帧集则补后。 */
function buildProcessEntries(frames: ChatMessage[], work: TreeRow[]): ProcessEntry[] {
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
  return pairDiffsWithTools(entries, {
    callId: (e) => String(e.frame?.tool?.tool_call_id || e.frame?.tool_call_id || e.row?.nodeId || ""),
    isTool: (e) => e.frame?.tool !== undefined || e.row !== undefined,
    isDiff: (e) => e.frame?.diff !== undefined,
    seq: (e) => e.frame?.seq,
  });
}

import { FOLD_GROUP_DEFAULT_OPEN, FOLD_GROUP_TITLES } from "./displayRules.generated";

type ToolLineGroupId = "brief" | "process" | "result";

export interface ToolLineGroup {
  id: ToolLineGroupId;
  title: string;
  hint: string;
  /** brief 默认收起；其余默认展开 */
  defaultOpen: boolean;
  text?: string;
  entries?: ProcessEntry[];
}

/** 展开区「任务指令」组头已是标题，剥掉正文里的 ``<任务指令>`` 包裹，避免重复。 */
export function stripTaskInstructionTags(text: string): string {
  let t = text.trim();
  if (!t) return t;
  if (t.startsWith("<任务指令>")) {
    t = t.slice("<任务指令>".length).replace(/^\s+/, "");
  }
  if (t.endsWith("</任务指令>")) {
    t = t.slice(0, t.length - "</任务指令>".length).replace(/\s+$/, "");
  }
  return t.replace(/<\/?任务指令>/g, "").trim();
}

export function buildToolLineGroups(input: {
  brief: string;
  work: TreeRow[];
  frames: ChatMessage[];
  body: string;
  result: string;
}): ToolLineGroup[] {
  const groups: ToolLineGroup[] = [];
  // trim 判空；展示保留原文（pre-wrap）。组头只给标题与展开符号——规模数字属于噪音。
  const brief = stripTaskInstructionTags(input.brief);
  if (brief) {
    groups.push({
      id: "brief",
      title: FOLD_GROUP_TITLES.brief,
      hint: "",
      defaultOpen: FOLD_GROUP_DEFAULT_OPEN.brief,
      text: brief,
    });
  }
  const body = input.body.trim();
  const entries = buildProcessEntries(input.frames, input.work);
  if (entries.length > 0 || body) {
    groups.push({
      id: "process",
      title: FOLD_GROUP_TITLES.process,
      hint: "",
      defaultOpen: FOLD_GROUP_DEFAULT_OPEN.process,
      entries,
      ...(body ? { text: input.body } : {}),
    });
  }
  const result = input.result.trim();
  if (result) {
    groups.push({
      id: "result",
      title: FOLD_GROUP_TITLES.result,
      hint: "",
      defaultOpen: FOLD_GROUP_DEFAULT_OPEN.result,
      text: input.result,
    });
  }
  return groups;
}
