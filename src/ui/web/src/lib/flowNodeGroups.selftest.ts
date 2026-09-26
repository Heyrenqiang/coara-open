/**
 * flowNodeGroups 自测。运行：npx tsx src/lib/flowNodeGroups.selftest.ts
 *
 * 覆盖三级折叠第二级的归属规则：subagent_id 命中 → coara_id 兜底 → 都不命中不丢。
 */
import { groupFramesByNode, nodeHasContent, type FlowNodeRef } from "./flowNodeGroups";
import type { ChatMessage } from "./store";

let failures = 0;
function check(name: string, cond: boolean): void {
  if (!cond) {
    failures += 1;
    console.error(`FAIL ${name}`);
  }
}

function frame(extra: Record<string, unknown>): ChatMessage {
  return { id: `m${Math.random()}`, role: "assistant", text: "", ...extra } as unknown as ChatMessage;
}

const nodes: FlowNodeRef[] = [
  { nodeId: "collect", subagentId: "flow-demo-collect", coaraId: "coara-a" },
  { nodeId: "review", subagentId: "flow-demo-review", coaraId: "coara-b" },
];

// ① subagent_id 命中（过程帧）
const chunkA = frame({ type: "subagent_chunk", subagent_id: "flow-demo-collect", text: "收集完成" });
// ② coara_id 兜底（结果帧不带 subagent_id）
const resultB = frame({ type: "subagent_result", coara_id: "coara-b", text: "评审通过" });
// ③ 都不命中
const stray = frame({ type: "subagent_chunk", text: "无主" });

const groups = groupFramesByNode([chunkA, resultB, stray], nodes);

check("subagent_id 命中归到 collect", groups.byNode.get("collect")?.length === 1);
check("coara_id 兜底归到 review", groups.byNode.get("review")?.[0] === resultB);
check("无主帧进未分组不丢", groups.ungrouped.length === 1 && groups.ungrouped[0] === stray);
check("空节点仍在清单里", groups.byNode.has("collect") && nodeHasContent(groups, "collect"));
check("未收内容的节点判空", !nodeHasContent(groups, "missing"));

// ④ 空节点表：全部落到未分组，绝不静默丢
const noNodes = groupFramesByNode([chunkA, resultB], []);
check("无节点表时全进未分组", noNodes.ungrouped.length === 2 && noNodes.byNode.size === 0);

if (failures > 0) {
  console.error(`flowNodeGroups selftest: ${failures} failed`);
  process.exit(1);
}
console.log("flowNodeGroups selftest: ok");
