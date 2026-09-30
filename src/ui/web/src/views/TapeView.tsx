import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Select, Tooltip, message } from "antd";
import { ExportOutlined, NodeIndexOutlined, ReloadOutlined } from "@ant-design/icons";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { useStore } from "../lib/store";
import { getAuthToken } from "../lib/auth";
import { TrajectoryPage } from "../features/trajectory/TrajectoryPage";
import {
  fetchTrajectoryWorkspaces,
  type TrajectoryWorkspace,
} from "../features/trajectory/trajectoryData";

/** 本标签是否录像带拖出窗（popout 模式）：无侧边栏、不回主对话页。 */
export function isTapePopout(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("popout") === "1";
}

/**
 * 录像带全屏页（/tape）。
 *
 * - 就地切换：页头空间下拉只换本页在看的那条录像带，不动全局 active 空间，
 *   主对话页现场不受干扰（方案 A）。
 * - 拖出独立窗：window.open 带 token + popout=1 开第二个标签；该标签走
 *   observer WS（role=tape），不占单标签坑、不顶替主连接，与主对话并行。
 * - 默认取全局 active 空间；拖出后主标签自动回 /chat。
 */
export function TapeView() {
  const navigate = useNavigate();
  const globalActiveDir = useStore((s) => s.workspaceDir) ?? "";
  const activeName = useStore((s) => s.activeName);
  const [targetDir, setTargetDir] = useState(() => {
    // 拖出窗落地：URL dir 参数优先（popoutWindow 写入），其次全局 active
    if (typeof window !== "undefined") {
      const fromUrl = new URLSearchParams(window.location.search).get("dir");
      if (fromUrl) return fromUrl;
    }
    return globalActiveDir;
  });
  const [tapeWorkspaces, setTapeWorkspaces] = useState<TrajectoryWorkspace[]>([]);
  const [reloadToken, setReloadToken] = useState(0);
  const popout = useMemo(() => isTapePopout(), []);

  useEffect(() => {
    void fetchTrajectoryWorkspaces().then(setTapeWorkspaces);
  }, []);

  const refreshTape = useCallback(() => {
    void fetchTrajectoryWorkspaces().then(setTapeWorkspaces);
    setReloadToken((n) => n + 1);
  }, []);

  // 全局 active 空间变化时，只在用户没手动换过目标的情况下跟随
  const userPicked = useRef(false);
  useEffect(() => {
    if (!userPicked.current && globalActiveDir) setTargetDir(globalActiveDir);
  }, [globalActiveDir]);

  const options = useMemo(() => {
    // 录像带清单（含 has_tape 标记）为主；若全局 active 空间不在清单里（无录像带）也补一项
    const opts = tapeWorkspaces.map((ws) => ({
      value: ws.workspace_dir,
      label: `${ws.name}${ws.has_tape ? "" : "（无录像带）"}`,
      disabled: !ws.has_tape,
    }));
    if (globalActiveDir && !tapeWorkspaces.some((w) => w.workspace_dir === globalActiveDir)) {
      opts.unshift({ value: globalActiveDir, label: `${activeName ?? "当前空间"}（当前）`, disabled: false });
    }
    return opts;
  }, [tapeWorkspaces, globalActiveDir, activeName]);

  const popoutWindow = useCallback(() => {
    const token = getAuthToken();
    const params = new URLSearchParams({ popout: "1" });
    if (token) params.set("token", token);
    // 拖出窗接上当前在看的目标空间（observer 无 state hydrate，不带则落地为空要重选）
    if (targetDir) params.set("dir", targetDir);
    const url = `${window.location.origin}/tape?${params.toString()}`;
    const win = window.open(url, "coara-tape", "width=1100,height=760,menubar=no,toolbar=no,location=no,status=no");
    if (!win) {
      message.warning("浏览器拦截了弹出窗口，请允许本站的弹出式窗口");
      return;
    }
    // 主标签回对话页，录像带留在新窗口里并行
    if (!popout) navigate("/chat");
  }, [navigate, popout, targetDir]);

  return (
    <PageShell
      scroll="hidden"
      padded={false}
      header={
        <PageHeader
          title={
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <NodeIndexOutlined style={{ color: "var(--coara-text-secondary)" }} />
              录像带
            </span>
          }
          onBack={popout ? undefined : () => navigate("/chat")}
          actions={
            <>
              <Select
                value={targetDir || undefined}
                options={options}
                onChange={(dir) => {
                  userPicked.current = true;
                  setTargetDir(dir);
                }}
                placeholder="选择工作空间"
                size="small"
                style={{ minWidth: 180 }}
                popupMatchSelectWidth={false}
              />
              <Tooltip title="重新从落带加载">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={refreshTape}
                  disabled={!targetDir}
                >
                  刷新
                </Button>
              </Tooltip>
              {!popout && (
                <Tooltip title="拖出为独立窗口，与主对话并行">
                  <Button size="small" icon={<ExportOutlined />} onClick={popoutWindow}>
                    拖出
                  </Button>
                </Tooltip>
              )}
            </>
          }
        />
      }
    >
      {targetDir ? (
        <TrajectoryPage workspaceDir={targetDir} reloadToken={reloadToken} />
      ) : (
        <div style={{ padding: 40, textAlign: "center", color: "var(--coara-text-muted)" }}>
          选择一个工作空间查看它的录像带
        </div>
      )}
    </PageShell>
  );
}
