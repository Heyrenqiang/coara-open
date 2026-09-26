/** 三端共享的显示口径常量与判据（聊天流噪音工具行 + 折叠组序）。
 *
 * 本文件由 scripts/dev/gen_envelopes.py 从 docs/protocol/coara-envelopes.json 生成，请勿手改。
 * 真源版本：v2（2026-09-11）
 */

export const FOLD_GROUP_ORDER = ["brief", "process", "result"] as const;
export type FoldGroupId = (typeof FOLD_GROUP_ORDER)[number];

export const FOLD_GROUP_TITLES: Record<FoldGroupId, string> = {
  brief: "任务指令",
  process: "过程",
  result: "最终结果",
};

export const FOLD_GROUP_DEFAULT_OPEN: Record<FoldGroupId, boolean> = {
  brief: false,
  process: true,
  result: true,
};

interface HiddenToolLineRule {
  id: string;
  hideWhenToolNameIn: string[];
  labelRegex: string;
  labelOkWhenToolNameIn: string[];
}

const RULES: HiddenToolLineRule[] = [
  {
    id: "delegate_wait",
    hideWhenToolNameIn: [],
    labelRegex: "^delegate\\s+wait\\b",
    labelOkWhenToolNameIn: ["", "delegate"],
  },
  {
    id: "send_file",
    hideWhenToolNameIn: ["send_file"],
    labelRegex: "^send_file\\b",
    labelOkWhenToolNameIn: [""],
  },
  {
    id: "todo_park",
    hideWhenToolNameIn: [],
    labelRegex: "^todo[\\s(-]+park\\b",
    labelOkWhenToolNameIn: ["", "todo"],
  },
  {
    id: "plan",
    hideWhenToolNameIn: [],
    labelRegex: "^plan[\\s(-]+plan\\b",
    labelOkWhenToolNameIn: ["", "plan"],
  },
];

const COMPILED = RULES.map((rule) => ({
  rx: new RegExp(rule.labelRegex),
  byName: new Set(rule.hideWhenToolNameIn),
  labelOk: new Set(rule.labelOkWhenToolNameIn),
}));

/** 这条工具行是否属于「不画」的过程噪音行（只看不画：数据与顺序都不动）。 */
export function isHiddenToolLineFields(toolName: string, label: string): boolean {
  const name = String(toolName ?? "").trim();
  const text = String(label ?? "").trim();
  for (const rule of COMPILED) {
    if (name && rule.byName.has(name)) return true;
    if (rule.labelOk.has(name) && rule.rx.test(text)) return true;
  }
  return false;
}
