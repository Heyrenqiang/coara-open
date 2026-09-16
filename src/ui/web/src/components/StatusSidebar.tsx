import { Typography, Tooltip } from "antd";
import {
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
} from "@ant-design/icons";
import { useStore, type RuntimeInfo } from "../lib/store";

const { Text, Title } = Typography;

/** Match CLI context accounting; keep sidebar-friendly length. */
function formatContextParts(runtime: RuntimeInfo | null | undefined): {
  fill: string;
  cache: string | null;
} {
  if (!runtime) return { fill: "—", cache: null };
  const used = Number(runtime.context_used_tokens) || 0;
  const window = Number(runtime.context_window_tokens) || 0;
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
    return { fill: `${pct}% · ${fmtK(used)}/${fmtK(window)}`, cache };
  }
  return { fill: used > 0 ? `${used} tok` : "0", cache };
}

function StatusRow({
  icon,
  label,
  value,
  valueColor,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  valueColor?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 0",
        fontSize: 13,
        minWidth: 0,
      }}
    >
      <span style={{ color: "var(--coara-text-muted)", fontSize: 13, flexShrink: 0 }}>{icon}</span>
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
 * 对话页右侧状态栏（对话态专属）。
 *
 * 职责边界（见 docs/Web设计体系.md §6.2）：只放「运行状态 / 上下文占用」。
 * 全局事实（空间身份、连接态）已上移到 ChatView 顶栏——它们在别的页面同样成立，
 * 不该只在对话页可见。宽度可拖拽由 ChatView 负责。
 */
export function StatusSidebar() {
  const runtime = useStore((s) => s.runtime);
  const contextParts = formatContextParts(runtime);

  return (
    <div
      className="chat-scroll-nobar"
      style={{
        height: "100%",
        background: "var(--coara-surface)",
        borderLeft: "1px solid var(--coara-border-faint)",
        overflowY: "auto",
        padding: "16px 16px",
        display: "flex",
        flexDirection: "column",
        gap: 14,
      }}
    >
      {/* Status header */}
      <div>
        <Title level={5} style={{ margin: 0, fontSize: 14, fontWeight: 600, color: "var(--coara-text-strong)" }}>
          状态
        </Title>
      </div>

      {/* Runtime info */}
      <div>
        <Text type="secondary" style={{ fontSize: 11, fontWeight: 500 }}>
          运行时
        </Text>
        <div style={{ marginTop: 4 }}>
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
        </div>
      </div>
    </div>
  );
}
