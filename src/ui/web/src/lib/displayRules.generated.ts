/** 三端共享的显示口径常量与判据（聊天流噪音工具行 + 折叠组序 + 耗时口径 + 静默子智能体）。
 *
 * 本文件由 scripts/dev/gen_envelopes.py 从 docs/protocol/coara-envelopes.json 生成，请勿手改。
 * 真源版本：v2（2026-09-27）
 */

/** 工具行标签括号形态正则（捕获组1=工具名、组2=参数正文）。 */
export const TOOL_PAREN_LABEL_RE = /^([A-Za-z_][\w.]*)\((.*)\)$/s;

/** 工具调用耗时口径（毫秒入参）：<1000 显 `{ms}ms`；<10000 显一位小数 `{x.x}s`；否则整数 `{n}s`。 */
export const DURATION_MS_SECONDS_AT = 1000;
export const DURATION_MS_WHOLE_SECONDS_AT = 10000;

/** 回合/相位耗时口径（秒入参，取整后）：<60 显 `{n}s`；<3600 显 `{m}m {ss}s`；否则 `{h}h {mm}m {ss}s`。 */
export const DURATION_MINUTE_SECONDS = 60;
export const DURATION_HOUR_SECONDS = 3600;
export const DURATION_PART_SEPARATOR = " ";

/** 静默子智能体名单（janitor/daily 系统管家，三端同尺）。 */
export const CLI_SILENT_SUBAGENT_TYPES: ReadonlySet<string> = new Set(["daily", "janitor"]);

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
