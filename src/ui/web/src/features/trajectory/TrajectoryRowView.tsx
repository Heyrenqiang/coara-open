import { memo } from "react";
import {
  ApiOutlined,
  BulbOutlined,
  CodeOutlined,
  ForkOutlined,
  InfoCircleOutlined,
  MessageOutlined,
  RobotOutlined,
  ToolOutlined,
  UserOutlined,
} from "@ant-design/icons";
import type { TrajectoryRow } from "./trajectoryData";
import "./trajectory.css";

/** 行类型 → chip 语义（图标 + 标签 + 色类）。录像带行角色封闭集 v2。 */
const ROLE_CHIP: Record<string, { icon: React.ReactNode; label: string; cls: string }> = {
  user: { icon: <UserOutlined />, label: "用户", cls: "tr-chip-user" },
  continuation: { icon: <UserOutlined />, label: "接续", cls: "tr-chip-user" },
  assistant: { icon: <MessageOutlined />, label: "助手", cls: "tr-chip-assistant" },
  thinking: { icon: <BulbOutlined />, label: "思考", cls: "tr-chip-thinking" },
  tool: { icon: <ToolOutlined />, label: "工具", cls: "tr-chip-tool" },
  inject: { icon: <InfoCircleOutlined />, label: "注入", cls: "tr-chip-inject" },
  context_module: { icon: <ApiOutlined />, label: "模块", cls: "tr-chip-inject" },
  prompt: { icon: <CodeOutlined />, label: "提示词", cls: "tr-chip-system" },
  subagent: { icon: <ForkOutlined />, label: "子智能体", cls: "tr-chip-subagent" },
  janitor: { icon: <RobotOutlined />, label: "janitor", cls: "tr-chip-janitor" },
  background: { icon: <RobotOutlined />, label: "后台", cls: "tr-chip-janitor" },
};

/** chip 角色：actor（外挂录像带）优先于 role。 */
function chipOf(row: TrajectoryRow) {
  if (row.actor === "janitor") return ROLE_CHIP.janitor;
  if (row.actor === "daily") return ROLE_CHIP.background;
  if (row.role === "inject" && row.tag === "接续输入") return ROLE_CHIP.continuation;
  return ROLE_CHIP[row.role] ?? { icon: <CodeOutlined />, label: row.role || "?", cls: "tr-chip-system" };
}

/** 状态点：running 呼吸 / 完成灰 / 失败红 / 用户蓝 */
function StateDot({ row }: { row: TrajectoryRow }) {
  let cls = "tr-dot";
  if (row.role === "tool") {
    if (row.tool?.running) cls += " tr-dot-running";
    else if (row.tool?.is_error) cls += " tr-dot-error";
  }
  return <span className={cls} aria-hidden />;
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`;
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  const two = (v: number) => String(v).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

const SOURCE_LABEL: Record<string, string> = {
  web: "web",
  matrix: "手机",
  "cli-attached": "CLI",
};

export interface TrajectoryRowViewProps {
  row: TrajectoryRow;
  selected: boolean;
  onSelect: (row: TrajectoryRow) => void;
  /** delegate 组头才有：展开态与切换 */
  expandable?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  /** 组头下的子行计数徽标（组头专属） */
  childCount?: number;
  /** 子树连接线深度：0=顶行，1=组内子行（连接线 + 缩进） */
  depth?: number;
}

/** 轨迹行：chip + 状态点 + 一句摘要 + 来源 + 耗时 + 时间。瘦信息，详情全在侧边。 */
export const TrajectoryRowView = memo(function TrajectoryRowView({
  row,
  selected,
  onSelect,
  expandable = false,
  expanded = false,
  onToggle,
  childCount,
  depth = 0,
}: TrajectoryRowViewProps) {
  if (row.role === "divider") {
    return (
      <div className="tr-divider" role="separator">
        <span className="tr-divider-line" />
        <span className="tr-divider-label">{row.text}</span>
        <span className="tr-divider-line" />
      </div>
    );
  }

  const chip = chipOf(row);
  const text = (row.text ?? "").trim();
  const duration = row.role === "tool" ? fmtDuration(row.tool?.duration_ms) : "";
  const source = SOURCE_LABEL[row.source] ?? "";

  return (
    <div
      className={`tr-row${selected ? " tr-row-selected" : ""}${row.subagent ? " tr-row-subagent" : ""}${depth > 0 ? ` tr-row-depth-${Math.min(depth, 2)}` : ""}`}
      data-role={row.role}
      onClick={() => onSelect(row)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(row);
        }
      }}
    >
      {depth > 0 && <span className="tr-tree-line" aria-hidden />}
      <span className={`tr-chip ${chip.cls}`} title={chip.label}>
        {chip.icon}
        <span className="tr-chip-label">{chip.label}</span>
      </span>
      <StateDot row={row} />
      <span className="tr-row-text" title={text}>
        {text || "—"}
      </span>
      {typeof childCount === "number" && childCount > 0 && (
        <span className="tr-row-child-count">{childCount} 项</span>
      )}
      {expandable && (
        <button
          type="button"
          className="tr-row-fold"
          aria-label={expanded ? "收起" : "展开"}
          onClick={(e) => {
            e.stopPropagation();
            onToggle?.();
          }}
        >
          {expanded ? "▾" : "▸"}
        </button>
      )}
      {source && <span className="tr-row-source">{source}</span>}
      {duration && <span className="tr-row-duration">{duration}</span>}
      <span className="tr-row-time">{fmtTime(row.ts)}</span>
    </div>
  );
});
