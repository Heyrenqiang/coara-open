/**
 * Self-test for sidebar tool activity extraction (tool_start in-progress).
 * Run: npx tsx src/lib/toolActivity.selftest.ts
 */
import {
  extractToolActivity,
  findToolActivityByCallId,
  type ToolActivityEvent,
} from "./toolActivity.ts";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

function ev(
  partial: Partial<ToolActivityEvent> & Pick<ToolActivityEvent, "event_type">,
): ToolActivityEvent {
  return {
    timestamp: partial.timestamp || "2026-01-01T00:00:00.000Z",
    origin_scope: "main_loop",
    ...partial,
  };
}

// Live long tool: start only → spinning
{
  const rows = extractToolActivity([
    ev({ event_type: "llm_turn_start" }),
    ev({ event_type: "tool_start", tool: "media", call_id: "c1" }),
  ]);
  assert(rows.length === 1, "expected one row");
  assert(rows[0].tool === "media", "tool name");
  assert(rows[0].done === false, "should spin while running");
}

// Completion: start → call → result → done with summary
{
  const rows = extractToolActivity([
    ev({ event_type: "llm_turn_start" }),
    ev({ event_type: "tool_start", tool: "shell", call_id: "c2" }),
    ev({ event_type: "tool_call", tool: "shell", call_id: "c2" }),
    ev({ event_type: "tool_result", call_id: "c2", summary: "ok", ok: true }),
  ]);
  assert(rows.length === 1, "one completed row");
  assert(rows[0].done === true, "done after result");
  assert(rows[0].ok === true, "ok flag");
  assert(rows[0].summary === "ok", "summary");
}

// tool_call must not reopen spinner after start
{
  const rows = extractToolActivity([
    ev({ event_type: "tool_start", tool: "read", call_id: "c3" }),
    ev({ event_type: "tool_call", tool: "read", call_id: "c3" }),
  ]);
  assert(rows[0].done === true, "tool_call closes in-progress");
}

// Legacy hydrate: tool_call without start → done (not spinning)
{
  const rows = extractToolActivity([
    ev({ event_type: "tool_call", tool: "grep", call_id: "c4" }),
  ]);
  assert(rows[0].done === true, "orphan tool_call is done");
}

// Hydrate orphan start before later barrier → closed
{
  const rows = extractToolActivity([
    ev({ event_type: "tool_start", tool: "old", call_id: "c5" }),
    ev({ event_type: "tool_call", tool: "new", call_id: "c6" }),
    ev({ event_type: "tool_result", call_id: "c6", summary: "x", ok: true }),
  ]);
  const old = rows.find((r) => r.call_id === "c5");
  const neu = rows.find((r) => r.call_id === "c6");
  assert(old?.done === true, "orphan start closed by later barrier");
  assert(neu?.done === true, "completed new tool");
}

// Concurrent trailing starts stay open
{
  const rows = extractToolActivity([
    ev({ event_type: "tool_result", call_id: "prev", summary: "done", ok: true }),
    ev({ event_type: "tool_start", tool: "a", call_id: "ca" }),
    ev({ event_type: "tool_start", tool: "b", call_id: "cb" }),
  ]);
  // prev never had start/call in buffer — ignored
  const open = rows.filter((r) => !r.done);
  assert(open.length === 2, `two concurrent open, got ${open.length}`);
}

// Subagent tools shown (发起端回显：服务端已按视图过滤，subagent_loop 即本空间子智能体)
{
  const rows = extractToolActivity([
    ev({ event_type: "llm_turn_start" }),
    ev({
      event_type: "tool_start",
      tool: "shell",
      call_id: "sa",
      origin_scope: "subagent_loop",
    }),
    ev({
      event_type: "tool_call",
      tool: "shell",
      call_id: "sa",
      origin_scope: "subagent_loop",
    }),
  ]);
  assert(rows.length === 1, "subagent tool shown");
  assert(rows[0].from_subagent === true, "subagent flag");
  assert(rows[0].done === true, "subagent tool completed");
}

// flow_loop scope still excluded
{
  const rows = extractToolActivity([
    ev({
      event_type: "tool_start",
      tool: "shell",
      call_id: "fl",
      origin_scope: "flow_loop",
    }),
  ]);
  assert(rows.length === 0, "flow_loop omitted");
}

// Detail lookup: tool_complete arrives before tool_call — must keep output/args
{
  const found = findToolActivityByCallId(
    [
      ev({
        event_type: "tool_start",
        tool: "read",
        call_id: "c7",
        args: { path: "/tmp/a" },
      }),
      ev({
        event_type: "tool_complete",
        call_id: "c7",
        tool: "read",
        tool_output: "file body",
        duration_ms: 12,
      }),
      ev({
        event_type: "tool_call",
        tool: "read",
        call_id: "c7",
        args: { path: "/tmp/a" },
      }),
      ev({
        event_type: "tool_result",
        call_id: "c7",
        summary: "read ok",
        ok: true,
      }),
    ],
    "c7",
  );
  assert(found, "found call");
  assert(found!.tool_output === "file body", "keep tool_complete output after tool_call");
  assert(found!.args && (found!.args as { path: string }).path === "/tmp/a", "keep args");
  assert(found!.summary === "read ok", "summary from result");
  assert(found!.duration_ms === 12, "duration from complete");
}

console.log("toolActivity.selftest: ok");
