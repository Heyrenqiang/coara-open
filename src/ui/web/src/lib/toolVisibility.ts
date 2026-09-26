/** 聊天流里应当隐藏的工具行（**只在渲染层过滤，数据不动**）。
 *
 * 判据已下沉到协议真源：`docs/protocol/coara-envelopes.json` 的 `display_rules` 段
 * → `lib/displayRules.generated.ts`（三端同源；生成物勿手改）。
 * 四条规则：
 *  - `delegate wait`：同步点不是工作——一回合可能出现几十次，混在正文里把阅读节奏切碎
 *    （活动树按同一条规则不给它建行）。delegate 的四类实事仍显示：spawn / resume / message / stop
 *  - `send_file`：效果本身就是聊天流里那张文件/图片卡，工具行是重复信息
 *  - `todo park`：效果就是 park 的 message 气泡，工具行是重复信息
 *  - `plan`：只有 `action=plan` 不上屏（它的效果就是计划审阅气泡本身）；`enter`/`exit`
 *    是模式开关，要上屏
 *
 * 取向：**认不出来就不隐**（字段缺失且 label 也对不上 → 照常显示）。
 *
 * 只看不画：顺序不变量、去重与快照对账仍按这些行参与；将来要「显示全部工具行」，
 * 只需让调用点不再使用这个判据。
 */
import { isHiddenToolLineFields } from "./displayRules.generated";

export function isHiddenToolLine(
  tool: { label?: string; tool_name?: string } | undefined,
): boolean {
  if (!tool) return false;
  return isHiddenToolLineFields(String(tool.tool_name ?? ""), String(tool.label ?? ""));
}
