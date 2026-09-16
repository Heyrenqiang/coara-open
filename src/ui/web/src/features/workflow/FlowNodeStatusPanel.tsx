/**
 * FlowNodeStatusPanel — 节点会话状态面板：FlowRoot 会话内实时状态。
 * 作为编辑器右侧面板的一种视图（与配置/WDL/帮助互斥）。
 * 执行层（实例 run/cancel/resume）已剥离为独立 wdl 软件，本面板只呈现
 * 会话推进产生的节点状态，不接任何执行接口。
 */
import { Empty, Tag, Typography } from "antd";
import type { FlowLiveNode, FlowLiveNodeStatus } from "../../lib/ws";
import { STATUS_LABELS_CN } from "./editor/NodeStatusBadge";
import "./flow-node-status.css";

const { Text, Paragraph } = Typography;

const STATUS_TAG: Record<FlowLiveNodeStatus, string> = {
  pending: "default",
  running: "processing",
  done: "success",
  failed: "error",
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
                <Tag color={STATUS_TAG[status] || "default"}>{statusLabel}</Tag>
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
                <Text type="danger" className="node-run-inspector-label">
                  错误
                </Text>
                <Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap" }}>
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
