/** 聊天流里应当隐藏的工具行（**只在渲染层过滤，数据不动**）。
 *
 * 两条规则（真源就是本文件，文档里只登记清单）：
 *  - `delegate wait`：它是同步点、不是工作——一回合可能出现几十次（生产带里 41 条），
 *    混在正文段落之间把阅读节奏切碎。活动树早就按同一条规则不给它建行
 *    （lib/subagentTree.ts 里也有这道门），聊天流与它保持一致。保留可见的是 delegate
 *    的四类实事：spawn（`delegate <类型>: 任务`）/ `resume` / `message` / `stop`。
 *  - `send_file`：它的效果本身就是聊天流里那张文件/图片卡，工具行是重复信息。
 *    直接判 `tool_name`；老帧没有 tool_name 时退回 label（`send_file(...)`）。
 *
 * 取向：**认不出来就不隐**（字段缺失且 label 也对不上 → 照常显示），宁可多一行，
 * 也不把用户可能需要的过程信息吞掉。
 *
 * 只看不画：顺序不变量、去重与快照对账仍按这些行参与；将来要「显示全部工具行」
 * 只需让调用点不再使用这个判据（或给它加一个开关）。
 */
export function isHiddenToolLine(
  tool: { label?: string; tool_name?: string } | undefined,
): boolean {
  if (!tool) return false;
  const name = String(tool.tool_name ?? "").trim();
  const label = String(tool.label ?? "").trim();
  // ① delegate 的同步点：label 是最稳的锚（服务端与 CLI 同源生成），tool_name 作复核；
  //    老帧缺 tool_name 时按 label 放行。
  if (/^delegate\s+wait\b/.test(label) && (name === "" || name === "delegate")) return true;
  // ② send_file：tool_name 直接判；缺字段的老帧看 label。
  if (name === "send_file") return true;
  if (name === "" && /^send_file\b/.test(label)) return true;
  return false;
}
