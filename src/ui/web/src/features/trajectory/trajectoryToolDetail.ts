/** 录像带工具详情：何时扫 trace、何时拉 spill（纯函数，可自测）。 */

import type { ToolCallLookupActivity } from "../../lib/api";
import type { TrajectoryTool } from "./trajectoryData";

export const ENRICH_CACHE_MAX = 48;
export const SPILL_PAGE_LINES = 200;

/** 带子上已够用 → 不必扫 32MB trace。 */
export function tapeDetailSufficient(tool: TrajectoryTool): boolean {
  if (tool.running) return false;
  const hasArgs = Boolean(tool.arguments && Object.keys(tool.arguments).length > 0);
  const hasOutput = Boolean((tool.output || "").trim());
  const hasSpillRef = Boolean((tool.output_ref || "").trim());
  // 标明截断却无 spill 引用：仍需回查 trace 找 ref
  if (tool.output_truncated && !hasSpillRef) return false;
  return hasArgs || hasOutput || hasSpillRef;
}

/** 仅当截断或预览为空时自动要 spill；有预览时留给用户点「加载全文」。 */
export function shouldAutoWantSpill(opts: {
  spillRef: string;
  truncated: boolean;
  inlineOutput: string;
}): boolean {
  if (!opts.spillRef.trim()) return false;
  return opts.truncated || !opts.inlineOutput.trim();
}

/** 只缓存已完成的命中；miss / 进行中不写缓存，避免钉死。 */
export function shouldCacheEnrichment(row: ToolCallLookupActivity | null): boolean {
  if (!row) return false;
  if (row.done === false) return false;
  return true;
}

export class EnrichCache {
  private readonly map = new Map<string, ToolCallLookupActivity>();

  get(callId: string): ToolCallLookupActivity | undefined {
    if (!this.map.has(callId)) return undefined;
    const hit = this.map.get(callId)!;
    this.map.delete(callId);
    this.map.set(callId, hit);
    return hit;
  }

  set(callId: string, value: ToolCallLookupActivity): void {
    if (this.map.has(callId)) this.map.delete(callId);
    this.map.set(callId, value);
    while (this.map.size > ENRICH_CACHE_MAX) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }
}
