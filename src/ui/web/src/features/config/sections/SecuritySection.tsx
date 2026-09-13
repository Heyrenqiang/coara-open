import { Card, Tag } from "antd";
import { LockOutlined } from "@ant-design/icons";

interface SecurityConfig {
  call_policy?: Record<string, string[]>;
  sandbox?: {
    enabled_for_untrusted?: boolean;
    blocked_commands?: string[];
    blocked_paths?: string[];
    blocked_hosts?: string[];
    blocked_env?: string[];
  };
}

interface Props {
  config: SecurityConfig;
}

export function SecuritySection({ config }: Props) {
  const sandbox = config.sandbox || {};
  return (
    <Card
      title={
        <span>
          <LockOutlined style={{ marginRight: 8 }} />
          安全
        </span>
      }
    >
      <div style={{ fontSize: 13 }}>
        <div style={{ display: "flex", padding: "4px 0" }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>沙箱</span>
          <span>
            {sandbox.enabled_for_untrusted ? (
              <Tag color="orange" style={{ fontSize: 11 }}>对不可信调用方启用</Tag>
            ) : (
              <Tag style={{ fontSize: 11 }}>关闭</Tag>
            )}
          </span>
        </div>
        {sandbox.blocked_commands && sandbox.blocked_commands.length > 0 && (
          <div style={{ display: "flex", padding: "4px 0" }}>
            <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>阻止命令</span>
            <span style={{ fontSize: 12 }}>{sandbox.blocked_commands.join(", ")}</span>
          </div>
        )}
        {sandbox.blocked_paths && sandbox.blocked_paths.length > 0 && (
          <div style={{ display: "flex", padding: "4px 0" }}>
            <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>阻止路径</span>
            <span style={{ fontSize: 12 }}>{sandbox.blocked_paths.join(", ")}</span>
          </div>
        )}
        {config.call_policy && Object.keys(config.call_policy).length > 0 && (
          <div style={{ display: "flex", padding: "4px 0" }}>
            <span style={{ color: "var(--coara-text-faint)", minWidth: 120 }}>调用策略</span>
            <span style={{ fontSize: 12, fontFamily: "monospace" }}>
              {JSON.stringify(config.call_policy)}
            </span>
          </div>
        )}
      </div>
    </Card>
  );
}
