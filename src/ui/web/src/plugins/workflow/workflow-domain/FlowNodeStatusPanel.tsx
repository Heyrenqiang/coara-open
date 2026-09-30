/**
 * FlowNodeStatusPanel — 节点会话状态面板：FlowRoot 会话内实时状态。
 * 作为编辑器右侧面板的一种视图（与配置/WDL/帮助互斥）。
 * 执行层（实例 run/cancel/resume）已剥离为独立 wdl 软件，本面板只呈现
 * 会话推进产生的节点状态，不接任何执行接口。
 */
import { Empty, Tag, Typography } from "antd";
import type { FlowLiveNode, FlowLiveNodeStatus } from "coara:shell";
import { STATUS_LABELS_CN } from "./editor/NodeStatusBadge";
import "./flow-node-status.css";

const { Text, Paragraph } = Typography;

const STATUS_TAG_STYLE: Record<FlowLiveNodeStatus, React.CSSProperties> = {
  pending: { color: "var(--coara-text-tertiary)" },
  running: { color: "var(--coara-progress)", borderColor: "var(--coara-progress)" },
  done: { color: "var(--coara-success)", borderColor: "var(--coara-success)" },
  failed: { color: "var(--coara-error)", borderColor: "var(--coara-error)" },
};

export function FlowNodeStatusPanel({
  nodeId,
  liveNode,
}: {
  nodeId: string | null;
  liveNode: FlowLiveNode | null;
}) {
  const status = liveNode?.status;
  const statusLabel = status
    ? STATUS_LABELS_CN[status === "done" ? "success" : status] || status
    : "未知";

  return (
    <>
      {!nodeId ? (
        <Empty description="选中画布上的节点查看状态" />
      ) : (
        <div className="node-run-inspector-body">
          <section className="node-run-inspector-summary">
            <div className="node-run-inspector-tags">
              {status && (
                <Tag style={STATUS_TAG_STYLE[status]}>{statusLabel}</Tag>
              )}
              {typeof liveNode?.activations === "number" && liveNode.activations > 0 && (
                <Tag>第 {liveNode.activations} 次激活</Tag>
              )}
            </div>
            {liveNode?.task ? (
              <div className="node-run-inspector-block">
                <Text type="secondary" className="node-run-inspector-label">
                  任务
                </Text>
                <Paragraph
                  ellipsis={{ rows: 4, expandable: true, symbol: "展开" }}
                  style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}
                >
                  {liveNode.task}
                </Paragraph>
              </div>
            ) : null}
            {(liveNode?.error || (liveNode?.result && liveNode.status === "failed")) && (
              <div className="node-run-inspector-block">
                <Text type="danger" className="node-run-inspector-label node-run-inspector-error-text">
                  错误
                </Text>
                <Paragraph className="node-run-inspector-error-text" style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>
                  {liveNode.error || liveNode.result}
                </Paragraph>
              </div>
            )}
            {liveNode?.result && liveNode.status === "done" ? (
              <div className="node-run-inspector-block">
                <Text type="secondary" className="node-run-inspector-label">
                  结果摘要
                </Text>
                <Paragraph
                  ellipsis={{ rows: 6, expandable: true, symbol: "展开" }}
                  style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}
                >
                  {liveNode.result}
                </Paragraph>
              </div>
            ) : null}
            {!liveNode && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                当前无会话内实时状态（节点可能仅存在于已保存草案）。
              </Text>
            )}
          </section>
        </div>
      )}
    </>
  );
}
