/** Run: npx tsx src/features/trajectory/trajectoryData.selftest.ts（cwd = src/ui/web） */
import {
  flattenTrajectoryLines,
  groupTrajectoryRows,
  mergeTrajectoryRows,
  projectLiveFrame,
  type TrajectoryRow,
} from "./trajectoryData";

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg);
}

function tool(seq: number, callId: string, text: string, opts?: Partial<TrajectoryRow>): TrajectoryRow {
  return {
    seq,
    ts: seq,
    source: "web",
    turn_id: "t1",
    role: "tool",
    text,
    tool: { name: "delegate", call_id: callId, is_error: false, running: false, duration_ms: null },
    ...opts,
  };
}

function child(seq: number, parent: string, text: string): TrajectoryRow {
  return {
    seq,
    ts: seq,
    source: "web",
    turn_id: "t1",
    role: "assistant",
    text,
    parent,
    subagent: true,
  };
}

const spawn = tool(1, "c1", "delegate coaras: 探路");
const a = child(2, "c1", "子步骤 A");
const b = child(3, "c1", "子步骤 B");

const grouped = groupTrajectoryRows([spawn, a, b]);
assert(grouped.length === 1 && grouped[0].kind === "group", "spawn+children → one group");
assert(grouped[0].group?.children.length === 2, "two children under spawn");

// 默认折叠由 UI 管；合帧必须把后到的子行挂回已有组头，而不是摊成顶层
const live = mergeTrajectoryRows(flattenTrajectoryLines(grouped, { omitSyntheticHeaders: true }), [
  child(4, "c1", "子步骤 C"),
]);
assert(live.length === 1 && live[0].kind === "group", "live child merges into existing group");
assert(live[0].group?.children.length === 3, "three children after live merge");

const older = mergeTrajectoryRows(
  [tool(0, "c0", "delegate aide: 更早"), child(-1, "c0", "旧子行")],
  flattenTrajectoryLines(live, { omitSyntheticHeaders: true }),
);
assert(older.length === 2, "older window + current → two groups");
assert(older.every((l) => l.kind === "group"), "both are groups");

// live 私有序号与落带 view_seq：同 call_id 不得双胞胎
const privateLive = tool(1_000_000_001, "cx", "delegate coaras: X", {
  tool: { name: "delegate", call_id: "cx", is_error: false, running: true, duration_ms: null },
});
const tip = tool(42, "cx", "delegate coaras: X", {
  tool: { name: "delegate", call_id: "cx", is_error: false, running: false, duration_ms: 12 },
});
const healed = mergeTrajectoryRows([privateLive], [tip]);
assert(healed.length === 1, "private+tip → one line");
const healedRow = healed[0].kind === "group" ? healed[0].group!.header : healed[0].row!;
assert(healedRow.seq === 42, "tip view_seq wins");
assert(healedRow.tool?.running === false, "terminal wins");

// 已有终态不被另一序号的 running 顶掉
const keep = mergeTrajectoryRows([tip], [privateLive]);
const keepRow = keep[0].kind === "group" ? keep[0].group!.header : keep[0].row!;
assert(keepRow.seq === 42, "terminal kept against running twin");
assert(keepRow.tool?.running === false, "still terminal");

// projectLiveFrame 认 view_seq
const projected = projectLiveFrame({
  type: "chunk",
  kind: "chunk",
  view_seq: 99,
  text: "hello",
  source: "web",
});
assert(projected?.seq === 99, "live frame keeps view_seq");
assert(projected?.text === "hello", "chunk text");

// janitor 合成头 seq 不与首子撞车
const j1: TrajectoryRow = {
  seq: 10,
  ts: 10,
  source: "web",
  turn_id: "tj",
  role: "assistant",
  text: "扫概况",
  actor: "janitor",
};
const j2: TrajectoryRow = { ...j1, seq: 11, text: "写记录" };
const jGrouped = groupTrajectoryRows([j1, j2]);
assert(jGrouped.length === 1 && jGrouped[0].kind === "group", "janitor run → one group");
const hdr = jGrouped[0].group!.header;
assert(hdr.seq === -1 - 10, "synthetic header seq");
assert(hdr.seq !== j1.seq, "header seq ≠ first child");
const flat = flattenTrajectoryLines(jGrouped);
const seqs = flat.map((r) => r.seq);
assert(new Set(seqs).size === seqs.length, "flat seqs unique (header + children)");

// daily 外挂组与 janitor 同尺
const d1: TrajectoryRow = {
  seq: 20,
  ts: 20,
  source: "web",
  turn_id: "td",
  role: "assistant",
  text: "写日报",
  actor: "daily",
};
const dGrouped = groupTrajectoryRows([d1]);
assert(dGrouped.length === 1 && dGrouped[0].kind === "group", "daily → one group");
assert(dGrouped[0].group?.key.startsWith("daily:"), "daily group key");
assert(dGrouped[0].group?.header.text?.includes("daily"), "daily header label");

// 跨段也聚一棵常驻树：janitor 跑多次、中间隔着普通行，仍归同一组（计数累加）
const gap: TrajectoryRow = {
  seq: 15,
  ts: 15,
  source: "web",
  turn_id: "t0",
  role: "assistant",
  text: "普通回合行",
};
const j3: TrajectoryRow = { ...j1, seq: 16, text: "再扫概况" };
const mixed = groupTrajectoryRows([j1, j2, gap, j3]);
const jGroups = mixed.filter((l) => l.kind === "group" && l.group?.header.actor === "janitor");
assert(jGroups.length === 1, "janitor 跨段仍一棵组");
assert(jGroups[0].group?.children.length === 3, "组内 3 行（含跨段新帧）");
assert(jGroups[0].group?.header.text?.includes("3 项"), "组头计数随更新");
assert(mixed.some((l) => l.kind === "row" && l.row?.seq === 15), "普通行不被吸进组");

console.log("trajectoryData.selftest: ok");
