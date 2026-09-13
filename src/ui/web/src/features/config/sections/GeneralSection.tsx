import { Card } from "antd";
import { SettingOutlined } from "@ant-design/icons";

interface GeneralConfig {
  log_level?: string;
  default_provider?: string;
  default_model?: string;
  coara_home?: string;
  skills_enabled?: boolean;
  records?: {
    enabled?: boolean;
  };
}

interface Props {
  config: GeneralConfig;
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", padding: "6px 0", fontSize: 13 }}>
      <span style={{ color: "var(--coara-text-faint)", minWidth: 100, flexShrink: 0 }}>{label}</span>
      <span>{value}</span>
    </div>
  );
}

export function GeneralSection({ config }: Props) {
  return (
    <Card
      title={
        <span>
          <SettingOutlined style={{ marginRight: 8 }} />
          通用设置
        </span>
      }
    >
      <InfoRow label="日志级别" value={config.log_level || "INFO"} />
      <InfoRow
        label="默认模型"
        value={
          config.default_provider && config.default_model
            ? `${config.default_provider}·${config.default_model}`
            : "未设置"
        }
      />
      <InfoRow label="coara Home" value={config.coara_home || "默认"} />
      <InfoRow label="技能" value={config.skills_enabled !== false ? "开启" : "关闭"} />
      <InfoRow label="记录" value={config.records?.enabled !== false ? "开启" : "关闭"} />
    </Card>
  );
}
