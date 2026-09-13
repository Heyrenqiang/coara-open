/** Self-test for the web subagent activity tree tracker.
 *
 * Covers the CLI-parity semantics that matter for correctness:
 *  1. subagent_start hangs under its delegate tool row (parent_tool_call_id)
 *  2. child tools map to the subagent node via coara_id
 *  3. tool_complete(foreground_async delegate) is only a spawn ack — row stays open
 *  4. subagent_complete closes the node and cascades to the parent delegate row
 *  5. janitor/daily are fully silent (no rows, their tools never leak)
 *  6. delegate(wait) creates no row; delegate tool children are hidden
 *  7. complete-without-start synthesizes a placeholder (dropped frame tolerance)
 *
 * Run: npx tsx src/lib/subagentTree.selftest.ts
 */
import { SubagentTreeTracker, formatToolLabel, type TreeRow } from "./subagentTree.ts";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

function labels(rows: TreeRow[]): string[] {
  return rows.map((r) => r.label);
}

// formatToolLabel: primary arg + clipping
{
  assert(formatToolLabel("read", { path: "D:\\a\\b.ts" }) === "read(D:\\a\\b.ts)", "label: path arg");
  assert(formatToolLabel("shell", { command: "git status" }) === "shell(git status)", "label: command arg");
  assert(formatToolLabel("grep", {}) === "grep", "label: bare tool name");
  const long = "x".repeat(120);
  assert(formatToolLabel("read", { path: long }).length === "read()".length + 81, "label: clipped at 80+ellipsis");
}

// 1-4: delegate → subagent → tools → ack → complete cascade
{
  const t = new SubagentTreeTracker();
  t.ingest({ type: "tool_start", call_id: "tc-1", tool: "delegate", args: { action: "spawn", description: "摸底" } });
  t.ingest({
    type: "subagent_start",
    subagent_id: "sa-1",
    subagent_type: "coaras",
    description: "摸底 web 端",
    child_coara_id: "co-1",
    parent_tool_call_id: "tc-1",
  });
  t.ingest({ type: "tool_start", call_id: "tc-2", tool: "read", args: { path: "a.ts" }, coara_id: "co-1" });
  let rows = t.rows();
  assert(rows.length === 3, "tree: delegate + subagent + tool rows");
  assert(rows[0].nodeId === "tc-1" && rows[0].depth === 0, "tree: delegate root");
  assert(rows[1].nodeId === "sa-1" && rows[1].depth === 1, "tree: subagent under delegate");
  assert(rows[2].nodeId === "tc-2" && rows[2].depth === 2, "tree: child tool under subagent");
  assert(rows.every((r) => r.active), "tree: all active while running");
  // spawn ack: delegate tool_complete must NOT close the row
  t.ingest({ type: "tool_complete", call_id: "tc-1", tool: "delegate", delegate_mode: "foreground_async", ok: true });
  rows = t.rows();
  assert(rows[0].active, "tree: spawn ack keeps delegate row open");
  // child tool completes
  t.ingest({ type: "tool_complete", call_id: "tc-2", tool: "read", ok: true });
  rows = t.rows();
  assert(!rows[2].active && !rows[2].isError, "tree: tool row closed ok");
  // subagent completes: cascade closes delegate
  t.ingest({ type: "subagent_complete", subagent_id: "sa-1", child_coara_id: "co-1" });
  rows = t.rows();
  assert(!rows[1].active, "tree: subagent closed");
  assert(!rows[0].active, "tree: parent delegate closed by cascade");
}

// 5: janitor/daily silent — no rows, their tools never leak
{
  const t = new SubagentTreeTracker();
  t.ingest({
    type: "subagent_start",
    subagent_id: "sa-j",
    subagent_type: "janitor",
    description: "维护",
    child_coara_id: "co-j",
  });
  t.ingest({ type: "tool_start", call_id: "tc-j", tool: "record", args: { name: "x" }, coara_id: "co-j" });
  assert(t.rows().length === 0, "silent: janitor creates no rows");
  assert(!t.hasActive(), "silent: no active nodes");
}

// 6: delegate(wait) no row; nested delegate (delegate with parent) hidden
{
  const t = new SubagentTreeTracker();
  t.ingest({ type: "tool_start", call_id: "tc-w", tool: "delegate", args: { action: "wait" } });
  assert(t.rows().length === 0, "wait: no row");
  // 嵌套 delegate（带 parent_tool_call_id）不建行——子智能体行才是 delegate 的孩子
  t.ingest({ type: "tool_start", call_id: "tc-1", tool: "delegate", args: { action: "spawn", description: "x" } });
  t.ingest({ type: "tool_start", call_id: "tc-2", tool: "delegate", args: { action: "spawn", description: "y" }, parent_tool_call_id: "tc-1" });
  const rows = t.rows();
  assert(rows.length === 1 && rows[0].nodeId === "tc-1", "nested delegate hidden");
  // 主会话在 delegate 活跃期间的普通工具：不挂到 delegate 行下（独立根行）
  t.ingest({ type: "tool_start", call_id: "tc-3", tool: "read", args: { path: "a" } });
  const rows2 = t.rows();
  assert(rows2.length === 2 && rows2[1].nodeId === "tc-3" && rows2[1].depth === 0, "main tool stays at root during delegate");
}

// 7: complete without start synthesizes placeholder and cascades
{
  const t = new SubagentTreeTracker();
  t.ingest({ type: "tool_start", call_id: "tc-1", tool: "delegate", args: { action: "spawn", description: "x" } });
  t.ingest({ type: "subagent_complete", subagent_id: "sa-late", parent_tool_call_id: "tc-1" });
  const rows = t.rows();
  assert(rows.some((r) => r.nodeId === "sa-late" && !r.active), "late complete: placeholder closed");
  assert(!rows[0].active, "late complete: delegate cascaded");
}

// failure marks error; single-delegate parent inference
{
  const t = new SubagentTreeTracker();
  t.ingest({ type: "tool_start", call_id: "tc-1", tool: "delegate", args: { action: "spawn", description: "x" } });
  t.ingest({
    type: "subagent_start",
    subagent_id: "sa-1",
    subagent_type: "aide",
    description: "调研",
    child_coara_id: "co-1",
  });
  const rows = t.rows();
  assert(rows.length === 2 && rows[1].nodeId === "sa-1" && rows[1].depth === 1, "single active delegate: parent inferred without explicit id");
  t.ingest({ type: "subagent_failed", subagent_id: "sa-1", child_coara_id: "co-1" });
  const after = t.rows();
  assert(after[1].isError && after[0].isError, "failure: error propagates to delegate row");
}

// clear resets everything
{
  const t = new SubagentTreeTracker();
  t.ingest({ type: "tool_start", call_id: "tc-1", tool: "delegate", args: { action: "spawn", description: "x" } });
  t.clear();
  assert(t.rows().length === 0 && !t.hasActive(), "clear: empty");
}

console.log("subagentTree selftest: all assertions passed");
