// 配置页插件页面（壳组件经门面取，配置域组件相对引入——随插件打包）
import { useNavigate } from "react-router-dom";
import { ModuleChatFloat, PageHeader, PageShell } from "coara:shell";
import { ConfigPanel } from "./ConfigPanel";

export function ConfigView() {
  const navigate = useNavigate();
  return (
    <PageShell
      header={
        <PageHeader
          title={
            <span
              role="button"
              onClick={() => navigate("/chat")}
              title="进入对话页"
              style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer" }}
            >
              配置管理
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
