/** 工具行渲染的共用件。
 *
 * 为什么抽出来：工具行的行渲染（✓/✗/◌ + label + ▸/耗时）与展开面板排版必须只有
 * 一份实现——复制两份的写法第一次换色就会分叉。聊天流内联工具行（MessageList 的
 * ToolLine，含 delegate 行）与 delegate 展开区里的过程帧都引这里的件拼装
 * （lib/toolLineGroups 只管分组数据，画由这里统一）。
 */
import { memo, useState, type ReactNode } from "react";
import type { TreeRow } from "../../lib/subagentTree";
import { descendantsOf } from "../../lib/toolLineGroups";
import type { ToolLineGroup } from "../../lib/toolLineGroups";

const MONO_FONT =
  "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace";

/** 把内核的 `tool(args)` 改成 `tool - args`，少一层括号噪音（对齐手机端
 *  `formatToolLabelForDisplay`）。已是 `tool - …` / `delegate …:` 等非括号
 *  形态时原样返回。 */
const TOOL_PAREN_LABEL_RE = /^([A-Za-z_][\w.]*)\((.*)\)$/s;

export function formatToolLabelForDisplay(label: string): string {
  const trimmed = label.trim();
  if (!trimmed) return trimmed;
  const match = TOOL_PAREN_LABEL_RE.exec(trimmed);
  if (!match) return trimmed;
  const name = match[1];
  const args = match[2].trim();
  return args ? `${name} - ${args}` : name;
}

/** 该工具行是否还在跑：按完整后代判——子智能体刚起步、工具行还没出时也要亮 spinner。
 *  后代取数与过程组「帧还没到的树行」共用 lib/toolLineGroups 里那一份（descendantsOf）。 */
export function toolRunning(rows: TreeRow[], callId: string): boolean {
  if (callId === "") return false;
  return (
    rows.some((r) => r.nodeId === callId && r.active) ||
    descendantsOf(rows, callId).some((r) => r.active)
  );
}

/** 工具行的「行」本身（不含展开面板）：调用方负责外层的间距与排布。
 *  形态对齐手机端 ToolLineRow：行首 `·`（比正文略大），label 括号改 `-` 分隔，
 *  不显示耗时；运行中行首换成单点呼吸脉动（ToolRunningDots 同款）。
 *  失败整行（点与标签）用红字，前缀仍是 ·。 */
export const ToolLineRow = memo(function ToolLineRow({
  label,
  ok,
  running,
  expandable,
  open,
  onToggle,
  paddingLeft = 16,
}: {
  label: string;
  ok: boolean;
  running: boolean;
  expandable: boolean;
  open: boolean;
  onToggle: () => void;
  paddingLeft?: number;
}) {
  const textColor = ok ? "var(--coara-text-secondary)" : "var(--coara-danger)";
  return (
    <div
      role={expandable ? "button" : undefined}
      onClick={expandable ? onToggle : undefined}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        maxWidth: "100%",
        // 时间要不要溢出，取决于这行的宽度算不算内边距：content-box 下
        // maxWidth 100% 不含左右内边距，整行会比正文宽出一个左内边距（22px），
        // 时间就顶到正文右缘之外。border-box + 右侧内边距后，行宽与正文一致，
        // 长标签由它自己省略，时间始终停在右缘内侧。
        boxSizing: "border-box",
        paddingLeft,
        paddingRight: 16,
        fontFamily: MONO_FONT,
        fontSize: 12.5,
        lineHeight: 1.7,
        color: textColor,
        cursor: expandable ? "pointer" : "default",
        userSelect: expandable ? "none" : undefined,
      }}
    >
      {running ? (
        <ToolRunningDot color={textColor} />
      ) : (
        // 静态点与运行中点同一形态（CSS 圆点）：不用 · 字符——字符墨迹位置由字体
        // 设计决定，放大后字框几何中心与墨迹中心错开，怎么对都不居中；圆点则
        // 天然随 flex 居中，与运行中态只差一个呼吸动画。
        <span
          style={{
            flexShrink: 0,
            width: 3,
            height: 3,
            borderRadius: "50%",
            background: textColor,
          }}
        />
      )}
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>
        {formatToolLabelForDisplay(label)}
      </span>
      {expandable ? (
        <span style={{ flexShrink: 0, opacity: 0.45 }}>{open ? "▾" : "▸"}</span>
      ) : null}
    </div>
  );
});

/** 执行中的工具指示：单点呼吸脉动（对齐手机端 ToolRunningDots）。
 *  透明度与缩放逐帧起伏，一眼看出这条还在跑，动感清楚又不抢注意力。 */
function ToolRunningDot({ color }: { color: string }) {
  return <span className="tool-running-dot" style={{ background: color }} aria-label="执行中" />;
}

/** 展开面板外壳：缩进、字体、排版——工具行的展开内容统一走这里。 */
export function ToolLinePanel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        marginTop: 2,
        paddingLeft: 32,
        paddingRight: 16,
        width: "100%",
        boxSizing: "border-box",
        fontFamily: MONO_FONT,
        fontSize: 12.5,
        lineHeight: 1.7,
        color: "var(--coara-text-secondary)",
        userSelect: "text",
      }}
    >
      {children}
    </div>
  );
}

/** 折叠面板里单组内容的限高：超出组内滚动。面板本身还有 240px 上限，两级各管一层
 *  ——一组（比如很长的任务指令）不该把整块面板撑满，把别的组挤出视野。 */
const GROUP_MAX_HEIGHT = 160;

/** 手风琴分组面板：每组一行标题栏（标题 + 计数 + ▸/▾），点击折叠/展开该组。
 *  组折叠态是纯 UI 态：不进 store、不持久化；默认值取 groups 的 defaultOpen，
 *  面板收起再打开即回到默认（用户可再自行折叠）。 */
export function ToolLineAccordion({
  groups,
  renderBody,
}: {
  groups: ToolLineGroup[];
  renderBody: (group: ToolLineGroup) => ReactNode;
}) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  return (
    <>
      {groups.map((g) => {
        const open = overrides[g.id] ?? g.defaultOpen;
        return (
          <div key={g.id} style={{ marginTop: 6 }}>
            <div
              role="button"
              onClick={() => setOverrides((m) => ({ ...m, [g.id]: !open }))}
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 6,
                cursor: "pointer",
                userSelect: "none",
              }}
            >
              <span style={{ color: "var(--coara-text-secondary)" }}>{g.title}</span>
              <span style={{ opacity: 0.5, fontSize: 11.5 }}>{g.hint}</span>
              <span style={{ opacity: 0.45 }}>{open ? "▾" : "▸"}</span>
            </div>
            {open ? (
              <div style={{ maxHeight: GROUP_MAX_HEIGHT, overflowY: "auto", paddingLeft: 12 }}>
                {renderBody(g)}
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

/** 行首标记：运行中 ◌、停车 …、成功 ✓、失败 ✗（与聊天流工具行同款）。 */
function mark(row: TreeRow): string {
  if (row.pending) return "…";
  if (row.active) return "◌";
  return row.isError ? "✗" : "✓";
}

function markColor(row: TreeRow): string {
  if (row.pending) return "var(--coara-text-tertiary)";
  if (row.active) return "var(--coara-progress)";
  return row.isError ? "var(--coara-danger)" : "var(--coara-success)";
}

/** 后代工作行列表（谁在跑、跑到哪一步）：过程组里补进来的树行（帧还没到的那些）用它渲染。 */
export function ToolSubtreeRows({ rows }: { rows: TreeRow[] }) {
  return (
    <>
      {rows.map((r) => (
        <div
          key={r.nodeId}
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 6,
            paddingLeft: r.depth * 12,
            overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          <span style={{ flexShrink: 0, width: 14, textAlign: "center", color: markColor(r) }}>
            {r.pending ? mark(r) : r.active ? <span className="coara-spin">{mark(r)}</span> : mark(r)}
          </span>
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              color: r.active ? "var(--coara-text)" : "var(--coara-text-secondary)",
            }}
          >
            {r.label}
          </span>
        </div>
      ))}
    </>
  );
}
