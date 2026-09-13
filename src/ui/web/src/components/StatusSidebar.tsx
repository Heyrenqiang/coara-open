import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Typography, Divider, Tooltip } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  ToolOutlined,
  ApiOutlined,
  DashboardOutlined,
  DatabaseOutlined,
} from "@ant-design/icons";
import { useStore, type RuntimeInfo } from "../lib/store";
import { extractToolActivity, type ToolActivity } from "../lib/toolActivity";
import { formatClock } from "../lib/format";

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
 * 职责边界（见 docs/Web设计体系.md §6.2）：只放「运行状态 / 上下文占用 / 最近工具调用」。
 * 全局事实（空间身份、连接态）已上移到 ChatView 顶栏——它们在别的页面同样成立，
 * 不该只在对话页可见。宽度可拖拽由 ChatView 负责。
 */
export function StatusSidebar() {
  const navigate = useNavigate();
  const runtime = useStore((s) => s.runtime);
  // Don't subscribe to the full traceEvents array — streaming chat_chunk would
  // re-render this sidebar on every token. activityEpoch only bumps on tool-ish events.
  const activityEpoch = useStore((s) => s.activityEpoch);
  const toolActivityReady = useStore((s) => s.toolActivityReady);

  const toolActivity = useMemo(
    () => extractToolActivity(useStore.getState().traceEvents),
    [activityEpoch],
  );
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

      <Divider style={{ margin: "4px 0" }} />

      {/* Recent tool activity */}
      <div style={{ flex: 1, minHeight: 0 }}>
        <Text type="secondary" style={{ fontSize: 11, fontWeight: 500 }}>
          最近工具调用
        </Text>
        <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 8 }}>
          {!toolActivityReady ? (
            // hydrate 完成前显示加载占位：避免「暂无工具调用」先闪一下再被真实
            // 列表替换（与缓存命中率恒渲染占位同一思路——数据晚到不插行不切换）。
            <Text type="secondary" style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
              加载中…
            </Text>
          ) : toolActivity.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
              暂无工具调用
            </Text>
          ) : (
            toolActivity.map((tc) => (
              <ToolActivityRow
                key={tc.call_id || `${tc.tool}-${tc.timestamp}`}
                tc={tc}
                onOpen={() =>
                  navigate(
                    "/file?view=tool&call_id=" + encodeURIComponent(tc.call_id || ""),
                  )
                }
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function ToolActivityRow({ tc, onOpen }: { tc: ToolActivity; onOpen: () => void }) {
  return (
    <div
      style={{
        padding: "8px 10px",
        background: "var(--coara-bg-subtle)",
        borderRadius: 6,
        fontSize: 12,
        border: "1px solid var(--coara-border-faint)",
        cursor: "pointer",
        transition: "all 0.15s ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "var(--coara-bg-subtle)";
        e.currentTarget.style.borderColor = "var(--coara-border)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "var(--coara-bg-subtle)";
        e.currentTarget.style.borderColor = "var(--coara-border-faint)";
      }}
      onDoubleClick={onOpen}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            minWidth: 18,
            height: 18,
            borderRadius: 4,
            background: "var(--coara-border-muted)",
            color: "var(--coara-text-strong)",
            fontSize: 10,
            fontWeight: 600,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {tc.turn_number}
        </span>
        <ToolOutlined style={{ fontSize: 11, color: "var(--coara-text-muted)" }} />
        <Text
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: "var(--coara-text)",
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {tc.tool}
        </Text>
        <Text type="secondary" style={{ fontSize: 10, color: "var(--coara-text-tertiary)", flexShrink: 0 }}>
          {formatClock(tc.timestamp)}
        </Text>
        {tc.done ? (
          tc.ok === false ? (
            <CloseCircleOutlined style={{ color: "var(--coara-error)", fontSize: 12 }} />
          ) : (
            <CheckCircleOutlined style={{ color: "var(--coara-success)", fontSize: 12 }} />
          )
        ) : (
          <LoadingOutlined style={{ color: "var(--coara-accent)", fontSize: 12 }} />
        )}
      </div>
      {tc.summary ? (
        <Text
          type="secondary"
          style={{
            display: "block",
            fontSize: 11,
            color: "var(--coara-text-secondary)",
            marginTop: 3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {tc.summary.length > 90 ? tc.summary.slice(0, 90) + "…" : tc.summary}
        </Text>
      ) : null}
    </div>
  );
}
