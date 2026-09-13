/** 把 diff 行/帧挂到「产生它的那条工具行」紧后面（按 `tool_call_id` 精确配对）。
 *
 * 为什么不能靠相邻：服务端投递顺序是「先工具行、再 diff」，但两者之间可能插进正文、
 * 工具行、其他 diff——生产带里 1558 条 diff 只有 540 条紧跟自己的 tool 帧，靠“前一条
 * 是不是工具行”来挂位必然出错。老帧没有 `tool_call_id`（空串）时退回原顺序（保持
 * 现有相邻语义），绝不丢弃。
 *
 * 纯函数 + 可泛型：折叠区的过程条目与主消息流的消息行都用它，规则只有一份。
 */

export interface DiffPairingAccessors<T> {
  /** 该条目所属的工具调用 id：工具行取自自身（tool.tool_call_id / nodeId），diff 取自身 */
  callId: (item: T) => string;
  isTool: (item: T) => boolean;
  isDiff: (item: T) => boolean;
  /** 同一工具多个 diff 的排序键（view_seq，升序）；缺省按原相对顺序（稳定排序） */
  seq: (item: T) => number | undefined;
}

/** 返回重排后的数组；**无需重排时返回原数组引用**（不触发无谓重渲染）。 */
export function pairDiffsWithTools<T>(items: T[], acc: DiffPairingAccessors<T>): T[] {
  // 先把所有「带 call_id 的 diff」收起来（顺序即原相对顺序），再看有没有能配的工具行。
  const pending = new Map<string, T[]>();
  for (const item of items) {
    if (!acc.isDiff(item)) continue;
    const callId = acc.callId(item);
    if (!callId) continue;
    const bucket = pending.get(callId);
    if (bucket) bucket.push(item);
    else pending.set(callId, [item]);
  }
  if (pending.size === 0) return items;
  const toolCalls = new Set<string>();
  for (const item of items) {
    if (!acc.isTool(item)) continue;
    const callId = acc.callId(item);
    if (callId) toolCalls.add(callId);
  }
  // 只有「列表里确实存在对应工具行」的 diff 才搬位置；配不到的（工具行缺失/老帧）留原位。
  for (const callId of [...pending.keys()]) {
    if (!toolCalls.has(callId)) pending.delete(callId);
  }
  if (pending.size === 0) return items;

  const out: T[] = [];
  const emitted = new Set<string>();
  for (const item of items) {
    if (acc.isDiff(item)) {
      const callId = acc.callId(item);
      // 已配对的 diff 一律不留在原位：改由它的工具行带出（可能在前面，也可能在后面）。
      if (callId && pending.has(callId)) continue;
    }
    out.push(item);
    if (!acc.isTool(item)) continue;
    const callId = acc.callId(item);
    const group = callId ? pending.get(callId) : undefined;
    if (!group || emitted.has(callId)) continue;
    emitted.add(callId);
    const sorted = [...group].sort(
      (a, b) => (acc.seq(a) ?? Number.MAX_SAFE_INTEGER) - (acc.seq(b) ?? Number.MAX_SAFE_INTEGER),
    );
    for (const diff of sorted) out.push(diff);
  }
  // 引用稳定：逐项相同就返回原数组（顺序归一/重放会反复调这里）。
  if (out.length === items.length && out.every((item, i) => item === items[i])) return items;
  return out;
}
