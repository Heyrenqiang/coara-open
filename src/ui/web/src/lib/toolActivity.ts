/** Derive sidebar "最近工具调用" rows from the in-memory trace buffer.

 * Semantics:
 * - ``tool_start`` → in-progress (spinner) while the tool is actually running
 * - ``tool_call`` / ``tool_result`` → completion (executor emits both after execute)
 * - Unpaired historical starts (hydrate / buffer trim) are closed so they never
 *   spin forever; only starts after the last barrier event stay open
 */

import type { DisplayBlock } from "./ws";
import type { CanonicalDiffLines } from "./ws";

export interface ToolActivityEvent {
  event_type: string;
  timestamp: string;
  turn_id?: string;
  origin_scope?: string;
  /** 模块主体标记（如 "config"）；主聊天侧栏跳过带 subject 的模块事件。 */
  subject?: string;
  tool?: string;
  call_id?: string;
  summary?: string;
  ok?: boolean;
  args?: Record<string, unknown>;
  display_blocks?: DisplayBlock[];
  diff_lines?: CanonicalDiffLines;
  tool_output?: string;
  tool_output_truncated?: boolean;
  tool_output_ref?: string;
  duration_ms?: number;
  is_error?: boolean;
}

export interface ToolActivity {
  call_id?: string;
  tool: string;
  summary?: string;
  ok?: boolean;
  done: boolean;
  timestamp: string;
  turn_id?: string;
  /** 子智能体（delegate spawn 的 sa-*）的工具调用——侧栏可据此加来源标记。 */
  from_subagent?: boolean;
  /** 1-based LLM round within the current user turn. */
  turn_number: number;
  args?: Record<string, unknown>;
  display_blocks?: DisplayBlock[];
  diff_lines?: CanonicalDiffLines;
  tool_output?: string;
  tool_output_truncated?: boolean;
  tool_output_ref?: string;
  duration_ms?: number;
  is_error?: boolean;
}

function isMainSessionScope(scope: string | undefined): boolean {
  // Missing scope (older persisted rows) counts as main; only explicit
  // subagent_loop is excluded.
  return scope !== "subagent_loop";
}

/**
 * 是否计入侧栏展示：主会话（main_loop / 无 scope 旧行）+ 本空间子智能体。
 *
 * 到达本缓冲的事件已过服务端视图过滤（_serialize_trace_event 按空间/会话
 * 判定 same_view），subagent_loop 即本视图空间的 delegate 子智能体——用户
 * 要求「子智能体的工具调用也要回显到发起端」，不再剔除；flow_loop 保持排除。
 */
function isSidebarScope(scope: string | undefined): boolean {
  return scope !== "flow_loop";
}

function isModuleSubject(subject: string | undefined): boolean {
  return subject != null && subject !== "" && subject !== "root";
}

function activityId(e: ToolActivityEvent): string {
  return e.call_id || `${e.tool || "unknown"}-${e.timestamp}`;
}

/**
 * Build recent tool activity rows (newest first, capped by caller via slice).
 */
export function extractToolActivity(events: ToolActivityEvent[]): ToolActivity[] {
  const calls = new Map<string, ToolActivity>();
  const order: string[] = [];
  let currentRound = 0;
  let lastBarrierIndex = -1;

  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (e.event_type === "user_message") {
      currentRound = 0;
      lastBarrierIndex = i;
      continue;
    }
    if (e.event_type === "llm_turn_start") {
      if (!isMainSessionScope(e.origin_scope) || isModuleSubject(e.subject)) continue;
      currentRound += 1;
      lastBarrierIndex = i;
      continue;
    }
    if (e.event_type === "tool_start") {
      if (!isSidebarScope(e.origin_scope) || isModuleSubject(e.subject)) continue;
      const id = activityId(e);
      if (!calls.has(id)) {
        calls.set(id, {
          call_id: e.call_id,
          tool: e.tool || "(unknown)",
          done: false,
          timestamp: e.timestamp,
          turn_id: e.turn_id,
          turn_number: currentRound || 1,
          ...(e.origin_scope === "subagent_loop" ? { from_subagent: true } : {}),
          ...(e.args ? { args: e.args } : {}),
        });
        order.push(id);
      } else if (e.args && !calls.get(id)!.args) {
        calls.get(id)!.args = e.args;
      }
      continue;
    }
    if (e.event_type === "tool_call") {
      if (!isSidebarScope(e.origin_scope) || isModuleSubject(e.subject)) continue;
      lastBarrierIndex = i;
      const id = activityId(e);
      const existing = calls.get(id);
      if (existing) {
        // Completion event after tool_start — never reopen a spinner.
        existing.done = true;
        if (e.args) existing.args = e.args;
        if (e.tool) existing.tool = e.tool;
      } else {
        // No prior start in the buffer (legacy hydrate / trimmed start):
        // tool_call is emitted only after execute, so treat as finished.
        calls.set(id, {
          call_id: e.call_id,
          tool: e.tool || "(unknown)",
          done: true,
          timestamp: e.timestamp,
          turn_id: e.turn_id,
          turn_number: currentRound || 1,
          ...(e.origin_scope === "subagent_loop" ? { from_subagent: true } : {}),
          ...(e.args ? { args: e.args } : {}),
        });
        order.push(id);
      }
      continue;
    }
    if (e.event_type === "tool_result") {
      if (!isSidebarScope(e.origin_scope) || isModuleSubject(e.subject)) continue;
      lastBarrierIndex = i;
      if (e.call_id && calls.has(e.call_id)) {
        const t = calls.get(e.call_id)!;
        t.summary = e.summary;
        t.ok = e.ok;
        t.done = true;
      }
      continue;
    }
    if (e.event_type === "tool_complete") {
      if (e.call_id && calls.has(e.call_id)) {
        const t = calls.get(e.call_id)!;
        if (e.display_blocks?.length) t.display_blocks = e.display_blocks;
        if (e.diff_lines) t.diff_lines = e.diff_lines;
        if (e.tool_output) {
          t.tool_output = e.tool_output;
          t.tool_output_truncated = e.tool_output_truncated;
          t.tool_output_ref = e.tool_output_ref;
        } else if (e.tool_output_ref) {
          t.tool_output_ref = e.tool_output_ref;
          t.tool_output_truncated = e.tool_output_truncated;
        }
        if (typeof e.duration_ms === "number") t.duration_ms = e.duration_ms;
        if (e.is_error != null) t.is_error = e.is_error;
        if (e.tool) t.tool = e.tool;
      }
      if (isMainSessionScope(e.origin_scope)) {
        lastBarrierIndex = i;
      }
      continue;
    }
    if (e.event_type === "turn_end") {
      lastBarrierIndex = i;
    }
  }

  // Close orphaned in-progress rows from hydrate / trim: only tool_start events
  // after the last barrier may remain spinning (truly still running / concurrent).
  for (const id of order) {
    const t = calls.get(id)!;
    if (t.done) continue;
    let startAfterBarrier = false;
    for (let i = lastBarrierIndex + 1; i < events.length; i++) {
      const e = events[i];
      if (e.event_type !== "tool_start") continue;
      if (!isSidebarScope(e.origin_scope)) continue;
      if (activityId(e) === id) {
        startAfterBarrier = true;
        break;
      }
    }
    if (!startAfterBarrier) {
      t.done = true;
    }
  }

  return order
    .map((id) => calls.get(id)!)
    .slice(-10)
    .reverse();
}

/** 按 call_id 在当前 events 缓冲区里定位一个工具调用（含 args / diff / 全文输出）。 */
export function findToolActivityByCallId(
  events: ToolActivityEvent[],
  callId: string,
): ToolActivity | null {
  let acc: ToolActivity | undefined;
  for (const e of events) {
    if (e.call_id !== callId) continue;
    if (e.event_type === "tool_start") {
      acc = {
        call_id: e.call_id,
        tool: e.tool || "(tool)",
        done: false,
        timestamp: e.timestamp,
        turn_id: e.turn_id,
        turn_number: 1,
        ...(e.args ? { args: e.args } : {}),
      };
      continue;
    }
    if (e.event_type === "tool_complete") {
      if (!acc) {
        acc = {
          call_id: e.call_id,
          tool: e.tool || "(tool)",
          done: true,
          timestamp: e.timestamp,
          turn_id: e.turn_id,
          turn_number: 1,
        };
      }
      if (e.display_blocks?.length) acc.display_blocks = e.display_blocks;
      if (e.diff_lines) acc.diff_lines = e.diff_lines;
      if (e.tool_output) {
        acc.tool_output = e.tool_output;
        acc.tool_output_truncated = e.tool_output_truncated;
        acc.tool_output_ref = e.tool_output_ref;
      } else if (e.tool_output_ref) {
        acc.tool_output_ref = e.tool_output_ref;
        acc.tool_output_truncated = e.tool_output_truncated;
      }
      if (typeof e.duration_ms === "number") acc.duration_ms = e.duration_ms;
      if (e.is_error != null) acc.is_error = e.is_error;
      if (e.tool) acc.tool = e.tool;
      acc.done = true;
      continue;
    }
    if (e.event_type === "tool_call") {
      acc = {
        call_id: e.call_id,
        tool: e.tool || acc?.tool || "(tool)",
        done: true,
        timestamp: e.timestamp,
        turn_id: e.turn_id,
        turn_number: acc?.turn_number ?? 1,
        args: e.args ?? acc?.args,
        summary: acc?.summary,
        ok: acc?.ok,
        display_blocks: acc?.display_blocks,
        diff_lines: acc?.diff_lines,
        // Prefer full tool_complete output; fall back to tool_call preview.
        tool_output: acc?.tool_output ?? e.tool_output,
        tool_output_truncated: acc?.tool_output_truncated ?? e.tool_output_truncated,
        tool_output_ref: acc?.tool_output_ref ?? e.tool_output_ref,
        duration_ms: acc?.duration_ms ?? e.duration_ms,
        is_error: acc?.is_error ?? e.is_error,
      };
      continue;
    }
    if (e.event_type === "tool_result") {
      if (!acc) {
        acc = {
          call_id: e.call_id,
          tool: e.tool || "(tool)",
          done: true,
          timestamp: e.timestamp,
          turn_id: e.turn_id,
          turn_number: 1,
        };
      }
      acc.summary = e.summary;
      acc.ok = e.ok;
      acc.done = true;
      if (e.tool) acc.tool = e.tool;
    }
  }
  return acc ?? null;
}
