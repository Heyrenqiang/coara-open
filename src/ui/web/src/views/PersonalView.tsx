import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Avatar, Button, message } from "antd";
import { NodeIndexOutlined, UserOutlined } from "@ant-design/icons";
import { fetchAccountStatus, logoutAccount } from "../lib/account";
import { useStore } from "../lib/store";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { LoadingState } from "../components/states/States";
import { AccountLogin } from "../features/account/AccountLogin";

/**
 * 个人主页（/me）：账户信息 + 录像带 + 退出登录。
 * 用量 / 配置已是侧边栏独立路由（/usage、/config），不再收编进本页；
 * 录像带（完整对话与工具执行轨迹）在本页入口查看。
 */
export function PersonalView() {
  const account = useStore((s) => s.account);
  const setAccount = useStore((s) => s.setAccount);
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);

  if (account === null) {
    return (
      <PageShell header={<PageHeader title="个人" />}>
        <LoadingState />
      </PageShell>
    );
  }

  if (!account.logged_in) {
    return (
      <PageShell header={<PageHeader title="登录" />} surface="subtle">
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 16,
            minHeight: "100%",
          }}
        >
          {account.gate_state === "trial" && (
            <Alert
              type="info"
              showIcon
              message="账户状态"
              description={account.gate_reason || ""}
              style={{ width: "100%", maxWidth: 360 }}
            />
          )}
          {account.gate_state === "need_login" && (
            <Alert
              type="warning"
              showIcon
              message="需要登录"
              description={account.gate_reason || ""}
              style={{ width: "100%", maxWidth: 360 }}
            />
          )}
          <AccountLogin
            embedded
            onSuccess={() => {
              fetchAccountStatus()
                .then(setAccount)
                .catch(() => undefined);
            }}
          />
        </div>
      </PageShell>
    );
  }

  const logout = async () => {
    setLoggingOut(true);
    try {
      await logoutAccount();
      setAccount({ logged_in: false });
      message.success("已退出登录");
    } catch (err) {
      message.error(err instanceof Error ? err.message : "退出失败");
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <PageShell header={<PageHeader title="个人" />}>
      <div style={{ maxWidth: 720 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Avatar
            icon={<UserOutlined />}
            style={{ backgroundColor: "var(--coara-accent-subtle)", color: "var(--coara-accent)" }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {account.email}
            </div>
            {account.device_name && (
              <div style={{ fontSize: 12, color: "var(--coara-text-secondary)" }}>{account.device_name}</div>
            )}
          </div>
        </div>
        {account.gate_state === "locked_payment" && (
          <Alert
            type="warning"
            showIcon
            message="活跃时长已超免费额度"
            description={account.gate_reason || ""}
            action={<Button size="small">去付费</Button>}
            style={{ marginTop: 16 }}
          />
        )}
        <div
          role="button"
          tabIndex={0}
          onClick={() => navigate("/tape")}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              navigate("/tape");
            }
          }}
          style={{
            marginTop: 20,
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "12px 14px",
            border: "1px solid var(--coara-border-muted)",
            borderRadius: 8,
            cursor: "pointer",
            color: "var(--coara-text-strong)",
          }}
        >
          <NodeIndexOutlined style={{ color: "var(--coara-text-secondary)" }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 500 }}>录像带</div>
            <div style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
              每个空间一条录像带，页内可切换查看，可拖出独立窗口
            </div>
          </div>
        </div>
        <Button danger onClick={logout} loading={loggingOut} style={{ marginTop: 20 }}>
          退出登录
        </Button>
      </div>
    </PageShell>
  );
}
