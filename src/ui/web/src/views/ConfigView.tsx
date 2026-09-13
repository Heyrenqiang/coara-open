import { ConfigPanel } from "../features/config/ConfigPanel";
import { ModuleChatFloat } from "../features/chat/ModuleChatFloat";
import { ExperimentalBadge } from "../components/ExperimentalBadge";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";

export function ConfigView() {
  return (
    <PageShell
      header={
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              配置管理
              <ExperimentalBadge />
            </span>
          }
        />
      }
    >
      <ConfigPanel />
      {/* 配置助手浮动会话：用自然语言管理配置（providers 等） */}
      <ModuleChatFloat
        subject="config"
        title="配置助手"
        emptyHint={"直接说出要做的配置\n例如：帮我加一个 DeepSeek"}
      />
    </PageShell>
  );
}
