import { useEffect, useState } from "react";
import { Card, Tag, Typography } from "antd";
import { KeyOutlined } from "@ant-design/icons";
import { fetchVaultStatus, type VaultStatus } from "../../../lib/vault";

const { Text } = Typography;

export function VaultSection() {
  const [status, setStatus] = useState<VaultStatus | null>(null);

  useEffect(() => {
    fetchVaultStatus()
      .then(setStatus)
      .catch(() => undefined);
  }, []);

  const enabled = status?.enabled ?? false;
  const initialized = status?.initialized ?? false;

  return (
    <Card
      title={
        <span>
          <KeyOutlined style={{ marginRight: 8 }} />
          保险柜
        </span>
      }
    >
      <div style={{ fontSize: 13 }}>
        <div style={{ display: "flex", padding: "4px 0", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--coara-text-faint)", minWidth: 60 }}>状态</span>
          {!enabled ? (
            <Tag style={{ fontSize: 11 }}>未启用</Tag>
          ) : (
            <>
              <Tag color={initialized ? "success" : "default"} style={{ fontSize: 11 }}>
                {initialized ? "已设置密码" : "未设置密码"}
              </Tag>
              {initialized && (
                <>
                  <Tag color={status?.unlocked ? "processing" : "default"} style={{ fontSize: 11 }}>
                    {status?.unlocked ? "已打开" : "已锁定"}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    已封存 {status?.entries ?? 0} 项
                  </Text>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </Card>
  );
}
