import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { FolderOutlined } from "@ant-design/icons";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { useStore } from "../lib/store";

/**
 * 空间主页兜底页（/home）：所有空间统一的「主页」入口。
 *
 * 有注册主页（home_view）的空间立即重定向到其主页路由；没有主页的
 * 对话空间停在本页，明确展示「暂无主页」。左上角空间名按钮与各主页
 * 标题按钮互为一对：对话页点它进主页，主页点标题回对话页。
 */
export function SpaceHomeView() {
  const navigate = useNavigate();
  const location = useLocation();
  const workspaces = useStore((s) => s.workspaces);
  const activeName = useStore((s) => s.activeName);
  const active = workspaces.find((ws) => ws.name === activeName);
  const home = active?.home_view?.trim();

  useEffect(() => {
    if (home && location.pathname === "/home") {
      navigate(home, { replace: true });
    }
  }, [home, location.pathname, navigate]);

  return (
    <PageShell
      header={
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <FolderOutlined style={{ color: "var(--coara-text-muted)" }} />
              {activeName || "空间"} 主页
            </span>
          }
        />
      }
    >
      <div
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--coara-text-muted)",
          fontSize: 14,
        }}
      >
        暂无主页
      </div>
    </PageShell>
  );
}
