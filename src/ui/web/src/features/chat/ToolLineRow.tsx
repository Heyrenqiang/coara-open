/** 工具行共用件：行渲染 + 展开面板（聊天流与 delegate 折叠共用）。 */
import { memo, useState, type ReactNode } from "react";
import type { TreeRow } from "../../lib/subagentTree";
import type { ToolLineGroup } from "../../lib/toolLineGroups";
import { TOOL_PAREN_LABEL_RE } from "../../lib/displayRules.generated";

/** 呼吸点判据（按 rows 引用 + callId 缓存，见 toolLineGroups.toolRunning）。 */
export { toolRunning } from "../../lib/toolLineGroups";

const MONO_FONT =
  "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace";

/** `tool(args)` → `tool - args`；剥掉历史 ` 报错: …`；非括号形态原样。 */
export function formatToolLabelForDisplay(label: string): string {
  let trimmed = label.trim();
  if (!trimmed) return trimmed;
  const errIdx = trimmed.indexOf(" 报错:");
  if (errIdx >= 0) trimmed = trimmed.slice(0, errIdx).trimEnd();
  const match = TOOL_PAREN_LABEL_RE.exec(trimmed);
  if (!match) return trimmed;
  const name = match[1];
  const args = match[2].trim();
  return args ? `${name} - ${args}` : name;
}

/** 工具行字号（展开区正文不越过它——子内容必须「退后半步」）。 */
const TOOL_LINE_FONT_SIZE = 12.5;
const TOOL_LINE_HEIGHT = 1.7;

/** 工具行本体（无面板）：· / 呼吸点 + label；可展开时行尾右置三角。失败整行红字。 */
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
        display: "flex",
        alignItems: "center",
        gap: 6,
        maxWidth: "100%",
        boxSizing: "border-box",
        paddingLeft,
        paddingRight: 16,
        fontFamily: MONO_FONT,
        fontSize: TOOL_LINE_FONT_SIZE,
        lineHeight: TOOL_LINE_HEIGHT,
        color: textColor,
        cursor: expandable ? "pointer" : "default",
        userSelect: expandable ? "none" : undefined,
      }}
    >
      {running ? (
        <ToolRunningDot color={textColor} />
      ) : (
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
      <span
        style={{
          flex: "1 1 auto",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          minWidth: 0,
        }}
      >
        {formatToolLabelForDisplay(label)}
      </span>
      {expandable ? (
        <span
          style={{
            flexShrink: 0,
            opacity: 0.45,
            fontSize: 10,
            transform: "translateY(-0.5px)",
          }}
        >
          {open ? "▾" : "▸"}
        </span>
      ) : null}
    </div>
  );
});

function ToolRunningDot({ color }: { color: string }) {
  return <span className="tool-running-dot" style={{ background: color }} aria-label="执行中" />;
}

/** 展开面板外壳：左缘参考线 + 缩进 + 右侧安全边距，整体是一个内聚的区块。 */
export function ToolLinePanel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        margin: "4px 16px 8px 21.5px",
        padding: "6px 12px 6px 12px",
        width: "auto",
        alignSelf: "stretch",
        boxSizing: "border-box",
        borderLeft: "1px solid var(--coara-border)",
        borderRadius: 2,
        background: "var(--coara-bg-subtle)",
        fontFamily: MONO_FONT,
        fontSize: 12,
        lineHeight: TOOL_LINE_HEIGHT,
        color: "var(--coara-text-secondary)",
        userSelect: "text",
      }}
    >
      {children}
    </div>
  );
}

/** 单组限高（面板另有 240 上限）。 */
const GROUP_MAX_HEIGHT = 160;

/** 手风琴：UI 折叠态不进 store；默认取 defaultOpen。组头＝标题（强一档）+ 规模提示，三角在行尾。 */
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
              <span
                style={{
                  color: "var(--coara-text)",
                  opacity: 0.8,
                  fontWeight: 500,
                  letterSpacing: 0.2,
                }}
              >
                {g.title}
              </span>
              <span
                style={{
                  flex: "1 1 auto",
                  minWidth: 0,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  color: "var(--coara-text-tertiary)",
                  fontSize: 11,
                }}
              >
                {g.hint}
              </span>
              <span style={{ flexShrink: 0, opacity: 0.45, fontSize: 10 }}>{open ? "▾" : "▸"}</span>
            </div>
            {open ? (
              <div
                style={{
                  maxHeight: GROUP_MAX_HEIGHT,
                  overflowY: "auto",
                  marginTop: 4,
                  paddingLeft: 8,
                  borderLeft: "1px solid var(--coara-border-muted)",
                }}
              >
                {renderBody(g)}
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

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

/** 过程组树行补给（帧未到时）。 */
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
