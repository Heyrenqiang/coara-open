/** 轨迹视图数据层：录像带分页读取 + 行分组。 */

import { tokenQuery } from "../../lib/auth";
import { isHiddenToolLineFields } from "../../lib/displayRules.generated";
import type { ServerMessage } from "../../lib/ws";

export interface TrajectoryTool {
  name: string;
  call_id: string;
  is_error: boolean;
  running: boolean;
  duration_ms: number | null;
  /** 调用参数（executor 压缩摘要，录像带详情面板用） */
  arguments?: Record<string, unknown>;
  /** 结果全文（已截断到服务端上限，录像带详情面板用） */
  output?: string;
  /** spill 落盘引用：详情可直拉全文，免扫 trace */
  output_ref?: string;
  /** 结果在落带时已截断（需按 output_ref 补全文） */
  output_truncated?: boolean;
}

export interface TrajectoryRow {
  seq: number;
  ts: number;
  source: string;
  turn_id: string;
  role: string;
  text?: string;
  parent?: string;
  subagent?: boolean;
  /** 外挂录像带标记（janitor/daily 维护流）：投影层按它聚成折叠组 */
  actor?: string;
  /** inject 帧的注入子类（系统消息/系统提醒/情境/后台结果/子智能体消息） */
  tag?: string;
  tool?: TrajectoryTool;
  diff?: unknown;
  attachments?: unknown[];
  files?: unknown[];
  /** context_module 帧的模块清单 */
  modules?: Array<{ id?: string; label?: string; file?: string; scope?: string }>;
  /** prompt 帧的快照 */
  prompt?: { hash: string; bytes: number; changed: boolean; text: string };
}

export interface TrajectoryWindow {
  rows: TrajectoryRow[];
  has_older: boolean;
  oldest_offset: number;
  file_size: number;
}

export interface TrajectoryWorkspace {
  name: string;
  workspace_dir: string;
  has_tape: boolean;
}

export async function fetchTrajectoryWorkspaces(): Promise<TrajectoryWorkspace[]> {
  const res = await fetch(`/api/v1/trajectory/workspaces${tokenQuery()}`);
  if (!res.ok) return [];
  const data = (await res.json()) as { workspaces?: TrajectoryWorkspace[] };
  return Array.isArray(data.workspaces) ? data.workspaces : [];
}

export async function fetchTrajectoryWindow(
  workspaceDir: string,
  beforeOffset: number | null,
): Promise<TrajectoryWindow> {
  const params = new URLSearchParams({ workspace_dir: workspaceDir });
  if (beforeOffset !== null) params.set("before_offset", String(beforeOffset));
  const sep = tokenQuery().startsWith("?") ? "&" : "?";
  const res = await fetch(`/api/v1/trajectory?${params}${sep}${tokenQuery().slice(1)}`);
  if (!res.ok) return { rows: [], has_older: false, oldest_offset: 0, file_size: 0 };
  return (await res.json()) as TrajectoryWindow;
}

/* ---- 行分组：delegate 组目录树式内嵌 ---- */

export interface TrajectoryGroup {
  /** 组头 delegate 行的 call_id；无组归属的行为空串 */
  key: string;
  header: TrajectoryRow | null;
  children: TrajectoryRow[];
}

export interface TrajectoryLine {
  kind: "row" | "group";
  row?: TrajectoryRow;
  group?: TrajectoryGroup;
}

/** 把扁平行序列整理成「普通行 + delegate 组 + janitor 外挂组」的显示序列。
 *  parent 指向某 delegate spawn 行 call_id 的行归入该组（保持时间序）；
 *  组内按 seq 排序，形成内嵌小录像带。孤儿行（父标识找不到组头）降级为普通行。
 *  actor=janitor 的连续帧聚成外挂录像带组（合成折叠头，无 delegate 父行）。
 *  diff_detail 行不进显示序列——吸进对应工具行的 diff 字段，详情面板展示。 */
export function groupTrajectoryRows(rows: TrajectoryRow[]): TrajectoryLine[] {
  const delegateIds = new Set<string>();
  for (const row of rows) {
    if (row.role === "tool" && row.tool?.call_id && isDelegateSpawnRow(row)) {
      delegateIds.add(row.tool.call_id);
    }
  }
  const childrenByParent = new Map<string, TrajectoryRow[]>();
  for (const row of rows) {
    const parent = row.parent;
    if (!parent || !delegateIds.has(parent)) continue;
    const list = childrenByParent.get(parent) ?? [];
    list.push(row);
    childrenByParent.set(parent, list);
  }

  const lines: TrajectoryLine[] = [];
  const absorbed = new Set<TrajectoryRow>();
  for (const children of childrenByParent.values()) {
    for (const child of children) absorbed.add(child);
  }

  // diff 行：按 tool_call_id 吸进对应工具行的 diff 字段（不单独成行），
  // 顶层行与组内子行统一处理。role=diff（服务端旧投影）与 diff_detail 同口径兜底。
  const diffByCallId = new Map<string, unknown>();
  for (const row of rows) {
    if ((row.role === "diff_detail" || row.role === "diff") && row.tool?.call_id && row.diff != null) {
      diffByCallId.set(row.tool.call_id, row.diff);
    }
  }
  const attachDiff = (row: TrajectoryRow) => {
    const callId = row.tool?.call_id ?? "";
    if (callId && row.diff == null && diffByCallId.has(callId)) {
      row.diff = diffByCallId.get(callId);
    }
  };

  for (const row of rows) {
    if (row.role === "diff_detail" || row.role === "diff") continue;
    if (absorbed.has(row)) continue;
    attachDiff(row);
    const callId = row.tool?.call_id ?? "";
    if (callId && childrenByParent.has(callId)) {
      const children = (childrenByParent.get(callId) ?? []).slice().sort((a, b) => a.seq - b.seq);
      children.forEach(attachDiff);
      lines.push({ kind: "group", group: { key: callId, header: row, children } });
      continue;
    }
    lines.push({ kind: "row", row });
  }

  // 外挂录像带：连续的 actor=janitor|daily 行聚成折叠组，合成折叠头
  // （无 delegate 父行——维护流不是谁 spawn 的）。
  // 合成头 seq 用负值独占，避免与首子行同 seq 撞 seqToFlat / data-flat-index / 选中 id。
  const result: TrajectoryLine[] = [];
  const MAINT_ACTORS = new Set(["janitor", "daily"]);
  let maintRun: TrajectoryRow[] = [];
  let maintActor = "";
  const flushMaint = () => {
    if (maintRun.length === 0) return;
    const first = maintRun[0];
    const actor = maintActor || "janitor";
    const header: TrajectoryRow = {
      ...first,
      seq: -1 - first.seq,
      role: "janitor",
      actor,
      text: `${actor} 维护 · ${maintRun.length} 项`,
      parent: undefined,
      tool: undefined,
    };
    result.push({
      kind: "group",
      group: { key: `${actor}:${first.seq}`, header, children: maintRun },
    });
    maintRun = [];
    maintActor = "";
  };
  for (const line of lines) {
    const actor = line.kind === "row" ? line.row?.actor ?? "" : "";
    if (line.kind === "row" && line.row && MAINT_ACTORS.has(actor)) {
      if (maintRun.length > 0 && maintActor !== actor) flushMaint();
      maintActor = actor;
      maintRun.push(line.row);
      continue;
    }
    flushMaint();
    result.push(line);
  }
  flushMaint();
  return result;
}

export function isDelegateRow(row: TrajectoryRow): boolean {
  return row.role === "tool" && /^delegate\b/.test(row.text ?? "");
}

/** 显示序列 → 扁平行（组头+子行全展开；时间线 / 合帧重归组共用）。
 *  ``omitSyntheticHeaders``：合帧时丢掉 janitor 合成头，避免重归组时把头再吸进 children。 */
export function flattenTrajectoryLines(
  lines: TrajectoryLine[],
  opts?: { omitSyntheticHeaders?: boolean },
): TrajectoryRow[] {
  const omitSynthetic = Boolean(opts?.omitSyntheticHeaders);
  const out: TrajectoryRow[] = [];
  for (const line of lines) {
    if (line.kind === "group" && line.group) {
      const header = line.group.header;
      const skipHeader = omitSynthetic && header?.role === "janitor";
      if (header && !skipHeader) out.push(header);
      for (const child of line.group.children) out.push(child);
    } else if (line.row) {
      out.push(line.row);
    }
  }
  return out;
}

/** 把新行并入已有轨迹后整表重归组：子智能体带子始终挂在组头下（默认可折叠）。
 *  同 seq 覆盖；工具行另按 call_id 消双胞胎（live 私有序号与落带 view_seq 两套空间）。
 *  已有终态不被另一序号的 running 孪生顶掉。 */
export function mergeTrajectoryRows(existing: TrajectoryRow[], incoming: TrajectoryRow[]): TrajectoryLine[] {
  if (incoming.length === 0) return groupTrajectoryRows(existing);

  const existingByCall = new Map<string, TrajectoryRow>();
  for (const row of existing) {
    const callId = row.role === "tool" ? row.tool?.call_id ?? "" : "";
    if (callId) existingByCall.set(callId, row);
  }

  const dropExistingSeq = new Set<number>();
  const skipIncoming = new Set<TrajectoryRow>();
  for (const row of incoming) {
    const callId = row.role === "tool" ? row.tool?.call_id ?? "" : "";
    if (!callId) continue;
    const prev = existingByCall.get(callId);
    if (!prev) continue;
    const prevDone = Boolean(prev.tool && !prev.tool.running);
    const rowDone = Boolean(row.tool && !row.tool.running);
    if (prev.seq === row.seq) {
      if (prev.tool?.running && rowDone) {
        dropExistingSeq.add(prev.seq);
      } else if (prevDone) {
        skipIncoming.add(row);
      }
      continue;
    }
    // 跨序号空间的同 call_id
    if (prevDone && !rowDone) {
      skipIncoming.add(row);
    } else {
      dropExistingSeq.add(prev.seq);
    }
  }

  const bySeq = new Map<number, TrajectoryRow>();
  for (const row of existing) {
    if (!dropExistingSeq.has(row.seq)) bySeq.set(row.seq, row);
  }
  for (const row of incoming) {
    if (skipIncoming.has(row)) continue;
    bySeq.set(row.seq, row);
  }
  return groupTrajectoryRows([...bySeq.values()].sort((a, b) => a.seq - b.seq));
}

/** delegate 组头判定：只有 spawn（含描述）的行才当组头——wait / resume / stop
 * 等控制行同样以 delegate 开头，但没有子智能体生命周期，归组会把后续无关行
 * 错误吸进组里。spawn 行文本形如「delegate coaras: 描述」「delegate aide: …」。 */
export function isDelegateSpawnRow(row: TrajectoryRow): boolean {
  if (row.role !== "tool") return false;
  const text = (row.text ?? "").trim();
  return /^delegate\s+(coaras|aide|daily|janitor)\b/.test(text);
}

/** 实时广播帧 → 轨迹行（与 trajectory.py 的投影同形）。
 *  优先用落带 ``view_seq``，便于与 tip/need_topup 合帧去重；无序号时由调用方填私有计数。 */
export function projectLiveFrame(frame: ServerMessage | Record<string, unknown>): TrajectoryRow | null {
  const rec = frame as Record<string, unknown>;
  const kind = String(rec.kind ?? rec.type ?? "");
  const payload = (rec.payload as Record<string, unknown> | undefined) ?? rec;
  const rawSeq = Number(rec.view_seq ?? rec.seq ?? 0);
  const row: TrajectoryRow = {
    seq: Number.isFinite(rawSeq) && rawSeq > 0 ? Math.trunc(rawSeq) : 0,
    ts: Number(rec.ts ?? Date.now() / 1000),
    source: String(rec.source ?? ""),
    turn_id: String(rec.turn_id ?? ""),
    role: "",
  };
  // 顶层 desk 优先；落盘旧帧可能只在 payload.desk
  const desk = String(rec.desk ?? payload.desk ?? "");
  if (desk) row.actor = desk;
  const parent = String(payload.parent_tool_call_id ?? "");
  if (parent) row.parent = parent;

  if (kind === "user_message") {
    const text = String(payload.text ?? payload.content ?? "");
    if (!text.trim()) return null;
    row.role = "user";
    row.text = text;
    return row;
  }
  if (kind === "chunk" || kind === "subagent_chunk" || kind === "subagent_result") {
    const text = String(payload.text ?? "");
    if (!text.trim()) return null;
    row.role = "assistant";
    row.text = text;
    if (kind !== "chunk") row.subagent = true;
    return row;
  }
  if (kind === "tool") {
    let text = String(payload.text ?? payload.tool_label ?? "");
    const errIdx = text.indexOf(" 报错:");
    if (errIdx >= 0) text = text.slice(0, errIdx).trimEnd();
    // 噪音工具行不上带：与服务端 trajectory.py 同一份真源判据
    // （displayRules.generated.ts，含 send_file 按 tool_name 隐去等全规则）。
    if (isHiddenToolLineFields(String(payload.tool_name ?? ""), text)) {
      return null;
    }
    row.role = "tool";
    row.text = text;
    // 新帧带 is_error；旧/出站帧常写 ok
    const isError =
      payload.is_error !== undefined && payload.is_error !== null
        ? Boolean(payload.is_error)
        : payload.ok !== undefined
          ? !Boolean(payload.ok)
          : false;
    row.tool = {
      name: String(payload.tool_name ?? ""),
      call_id: String(payload.tool_call_id ?? ""),
      is_error: isError,
      running: Boolean(payload.running),
      duration_ms: (payload.duration_ms as number | null) ?? null,
    };
    // 参数与结果全文随帧（详情面板分段展示）
    if (payload.arguments && typeof payload.arguments === "object") {
      row.tool.arguments = payload.arguments as Record<string, unknown>;
    }
    const output = String(payload.tool_output ?? "");
    if (output) row.tool.output = output;
    const outputRef = String(payload.tool_output_ref ?? "").trim();
    if (outputRef) row.tool.output_ref = outputRef;
    if (payload.tool_output_truncated) row.tool.output_truncated = true;
    return row;
  }
  if (kind === "diff") {
    // 与 trajectory.py 投影同形：diff 不上带，吸进对应工具行详情。
    // 无有效 diff 数据（不是 object 或空）不上带——与服务端同尺，空行会把组撑成幽灵大块。
    const diff = payload.diff ?? payload.diff_lines;
    if (!diff || typeof diff !== "object" || Object.keys(diff as object).length === 0) {
      return null;
    }
    row.role = "diff_detail";
    row.tool = { call_id: String(payload.tool_call_id ?? "") } as TrajectoryRow["tool"];
    row.diff = diff;
    return row;
  }
  if (kind === "divider") {
    row.role = "divider";
    row.text = String(payload.label ?? payload.text ?? "");
    return row;
  }
  if (kind === "inject") {
    row.role = "inject";
    row.tag = String(payload.tag ?? "系统消息");
    row.text = String(payload.text ?? "");
    return row;
  }
  if (kind === "thinking") {
    const text = String(payload.text ?? "");
    if (!text.trim()) return null;
    row.role = "thinking";
    row.text = text;
    return row;
  }
  if (kind === "context_module") {
    row.role = "context_module";
    row.text = String(payload.text ?? "上下文模块");
    if (Array.isArray(payload.modules)) row.modules = payload.modules as TrajectoryRow["modules"];
    return row;
  }
  if (kind === "prompt") {
    row.role = "prompt";
    row.text = String(payload.summary ?? payload.text ?? "系统提示词快照");
    row.prompt = {
      hash: String(payload.hash ?? ""),
      bytes: Number(payload.bytes ?? 0),
      changed: Boolean(payload.changed),
      text: String(payload.text ?? ""),
    };
    return row;
  }
  // 与服务端 trajectory.py 投影同尺：files / delegate_brief / 未知 kind 兜底不丢
  if (kind === "files") {
    row.role = "files";
    row.text = String(payload.caption ?? "");
    if (Array.isArray(payload.files)) row.files = payload.files as unknown[];
    return row;
  }
  if (kind === "delegate_brief") {
    const text = String(payload.text ?? "");
    if (!text.trim()) return null;
    row.role = "brief";
    row.text = text;
    return row;
  }
  // 未知 kind：不丢，按通用行投出（宁可多一行，与服务端兜底同尺）
  const fallbackText = String(payload.text ?? "");
  if (!fallbackText.trim()) return null;
  row.role = kind || "unknown";
  row.text = fallbackText;
  return row;
}
