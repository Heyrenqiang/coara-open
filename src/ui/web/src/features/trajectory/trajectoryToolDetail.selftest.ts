/** Run: npx tsx src/features/trajectory/trajectoryToolDetail.selftest.ts */

import {
  EnrichCache,
  shouldAutoWantSpill,
  shouldCacheEnrichment,
  tapeDetailSufficient,
} from "./trajectoryToolDetail";
import type { TrajectoryTool } from "./trajectoryData";

function tool(partial: Partial<TrajectoryTool>): TrajectoryTool {
  return {
    name: "read",
    call_id: "c1",
    is_error: false,
    running: false,
    duration_ms: null,
    ...partial,
  };
}

let failed = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) {
    console.error("FAIL:", msg);
    failed += 1;
  }
}

assert(tapeDetailSufficient(tool({ arguments: { path: "/a" }, output: "ok" })), "rich tape sufficient");
assert(!tapeDetailSufficient(tool({ running: true, arguments: { path: "/a" } })), "running never sufficient");
assert(
  !tapeDetailSufficient(tool({ arguments: { path: "/a" }, output: "x", output_truncated: true })),
  "truncated without ref needs enrich",
);
assert(
  tapeDetailSufficient(
    tool({ arguments: { path: "/a" }, output: "x", output_truncated: true, output_ref: "spill/1" }),
  ),
  "truncated with ref sufficient",
);
assert(tapeDetailSufficient(tool({ output: "only-out" })), "output-only tools skip enrich");
assert(!tapeDetailSufficient(tool({})), "empty thin needs enrich");

assert(shouldAutoWantSpill({ spillRef: "r", truncated: true, inlineOutput: "prev" }), "auto on truncated");
assert(!shouldAutoWantSpill({ spillRef: "r", truncated: false, inlineOutput: "prev" }), "no auto when preview ok");
assert(shouldAutoWantSpill({ spillRef: "r", truncated: false, inlineOutput: "" }), "auto when no preview");
assert(!shouldAutoWantSpill({ spillRef: "", truncated: true, inlineOutput: "" }), "no ref no spill");

assert(!shouldCacheEnrichment(null), "never cache miss");
assert(!shouldCacheEnrichment({ call_id: "c", tool: "t", done: false, timestamp: "" }), "never cache running");
assert(shouldCacheEnrichment({ call_id: "c", tool: "t", done: true, timestamp: "" }), "cache done");

const cache = new EnrichCache();
cache.set("a", { call_id: "a", tool: "t", done: true, timestamp: "" });
assert(cache.get("a")?.call_id === "a", "cache hit");

if (failed > 0) {
  console.error(`${failed} assertion(s) failed`);
  process.exit(1);
}
console.log("trajectoryToolDetail.selftest: ok");
