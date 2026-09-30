import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { FolderOutlined } from "@ant-design/icons";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { useStore } from "../lib/store";
import { SpacePluginView, isPluginHomeView } from "./SpacePluginView";

/**
 * 空间主页兜底页（/home）：所有空间统一的「主页」入口。
 *
 * 有注册主页（home_view）的空间立即重定向到其主页路由；`plugin:` 前缀
 * 的声明走插件槽位（《空间能力系统》槽位四），本页就地整页渲染空间
 * 自声明的 UI bundle；没有主页的对话空间停在本页展示「暂无主页」。
 */
export function SpaceHomeView() {
  const navigate = useNavigate();
  const location = useLocation();
  const workspaces = useStore((s) => s.workspaces);
  const activeName = useStore((s) => s.activeName);
  const active = workspaces.find((ws) => ws.name === activeName);
  const home = active?.home_view?.trim();
  const isPlugin = isPluginHomeView(home);

  useEffect(() => {
    // 插件主页就地渲染，不重定向；系统路由照旧重定向
    if (home && !isPlugin && location.pathname === "/home") {
      navigate(home, { replace: true });
    }
  }, [home, isPlugin, location.pathname, navigate]);

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
      {isPlugin && active && home ? (
        <SpacePluginView workspace={active} homeView={home} />
      ) : (
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
      )}
    </PageShell>
  );
}
