import { Card } from "antd";
import { CompressOutlined } from "@ant-design/icons";

interface CompressionConfig {
  threshold?: number;
  preserve_ratio?: number;
  min_compressible_fraction?: number;
}

interface Props {
  config: CompressionConfig;
}

export function CompressionSection({ config }: Props) {
  return (
    <Card
      title={
        <span>
          <CompressOutlined style={{ marginRight: 8 }} />
          上下文压缩
        </span>
      }
    >
      <div style={{ fontSize: 13 }}>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>触发阈值</span>
          <span>{config.threshold ?? "默认"}</span>
        </div>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>保留比例</span>
          <span>{config.preserve_ratio ?? "默认"}</span>
        </div>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>最小可压缩比例</span>
          <span>{config.min_compressible_fraction ?? "默认"}</span>
        </div>
      </div>
    </Card>
  );
}
