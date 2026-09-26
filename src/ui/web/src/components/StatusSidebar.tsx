import { Typography, Tooltip } from "antd";
import {
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  MobileOutlined,
  PayCircleOutlined,
} from "@ant-design/icons";
import { useStore, isWebSource, type RuntimeInfo } from "../lib/store";
import { RosterSection } from "./layout/Roster";

const { Text } = Typography;

/** Match CLI context accounting; keep sidebar-friendly length. */
function formatContextParts(runtime: RuntimeInfo | null | undefined): {
  fill: string;
  cache: string | null;
} {
  if (!runtime) return { fill: "—", cache: null };
  const used = Number(runtime.context_used_tokens) || 0;
  const window = Number(runtime.context_window_tokens) || 0;
  const tilde = runtime.context_estimated ? "~" : "";
  const hit = runtime.context_cache_hit_ratio;
  const cache =
    typeof hit === "number" && Number.isFinite(hit)
      ? `${(hit * 100).toFixed(0)}%`
      : null;
  const fmtK = (n: number) => {
    const k = n / 1000;
    return k >= 100 ? `${k.toFixed(0)}k` : `${k.toFixed(1)}k`;
  };
  if (window > 0) {
    const pct = ((used / window) * 100).toFixed(1);
    return { fill: `${tilde}${pct}% · ${fmtK(used)}/${fmtK(window)}`, cache };
  }
  return { fill: used > 0 ? `${tilde}${used} tok` : "0", cache };
}

/** 会话花费展示：¥ 前缀 + 一位小数。 */
function formatSessionCost(runtime: RuntimeInfo | null | undefined): string {
  const cost = Number(runtime?.session_cost) || 0;
  return `¥${cost.toFixed(1)}`;
}

function StatusRow({
  icon,
  label,
  value,
  valueColor,
  last = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  valueColor?: string;
  last?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "10px 0",
        fontSize: 13,
        minWidth: 0,
        borderBottom: last ? "none" : "1px solid var(--coara-border-faint)",
      }}
    >
      <span style={{ color: "var(--coara-text-tertiary)", fontSize: 13, flexShrink: 0 }}>{icon}</span>
      <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
        {label}
      </Text>
      <Tooltip title={typeof value === "string" ? value : undefined} placement="left">
        <Text
          style={{
            fontSize: 13,
            marginLeft: "auto",
            textAlign: "right",
            color: valueColor || "var(--coara-text)",
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {value}
        </Text>
      </Tooltip>
    </div>
  );
}

/**
 * 对话页右侧状态栏：运行状态 / 上下文占用。
 * 空间身份与连接态在 ChatView 顶栏；宽度拖拽由 ChatView 负责。
 */
/** 他端在跑时的短标（与 CLI occupied_turn_label 对齐；空 = 本端/无）。 */
function remoteRunningLabel(runtime: RuntimeInfo | null | undefined): string {
  const src = String(runtime?.turn_source ?? "").trim();
  if (!src || isWebSource(src) || !runtime?.running) return "";
  if (src === "matrix") return "手机";
  if (src.startsWith("cli")) return "CLI";
  return "他端";
}

export function StatusSidebar() {
  const runtime = useStore((s) => s.runtime);
  const contextParts = formatContextParts(runtime);
  const sessionCost = formatSessionCost(runtime);
  const runningLabel = remoteRunningLabel(runtime);

  return (
    <div
      className="chat-scroll-nobar"
      style={{
        height: "100%",
        background: "var(--coara-surface)",
        borderLeft: "1px solid var(--coara-border-faint)",
        overflowY: "auto",
        padding: "12px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <RosterSection title="状态">
        {runningLabel ? (
          <StatusRow
            icon={<MobileOutlined />}
            label="运行中"
            value={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="coara-remote-running-dot" />
                {runningLabel}
              </span>
            }
          />
        ) : null}
        <StatusRow
          icon={<ApiOutlined />}
          label="Provider"
          value={runtime?.provider || "?"}
        />
        <StatusRow
          icon={<ApiOutlined />}
          label="Model"
          value={runtime?.model || "?"}
        />
        <StatusRow
          icon={<DashboardOutlined />}
          label="上下文"
          value={contextParts.fill}
        />
        {/* 缓存命中率恒渲染占位：数据晚到时只更新文本，不插入新行——
            避免启动时行突然出现把下方内容顶下去造成的布局抖动（闪）。 */}
        <StatusRow
          icon={<DatabaseOutlined />}
          label="缓存命中率"
          value={contextParts.cache ?? "—"}
        />
        <StatusRow
          icon={<PayCircleOutlined />}
          label="花费"
          value={sessionCost}
          last
        />
      </RosterSection>
    </div>
  );
}
