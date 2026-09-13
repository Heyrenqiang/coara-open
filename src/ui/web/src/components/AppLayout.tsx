import { useEffect, useRef, useState, startTransition, type ReactNode } from "react";
import { Layout, Menu, Splitter, Badge, Tooltip, Input, Modal, message } from "antd";
import {
  DeleteOutlined,
  MessageOutlined,
  PlusOutlined,
  UserOutlined,
  FolderOutlined,
} from "@ant-design/icons";
import { useLocation, useNavigate } from "react-router-dom";
import { useStore, type WorkspaceInfo } from "../lib/store";
import {
  HOME_PREFIXES,
  ROUTE_PREFETCH,
  fallbackSpaceIcon,
  systemViewByHome,
} from "../lib/navRegistry";
import {
  createWorkspaceEntry,
  fetchUpdatesSummary,
  fetchWorkspaceList,
  removeWorkspaceEntry,
  switchWorkspace,
} from "../lib/api";

const { Content } = Layout;

/**
 * Layout sizing (IDE / chat best practice):
 * - Persist side *ratios* of the viewport so enlarge widens rails within caps;
 *   shrink compresses only when the middle would fall below MAIN_MIN.
 * - Middle panel has no fixed size — it absorbs leftover space.
 */
const LAYOUT_KEY = "coara.web.layout.ratios";

const LEFT_MIN = 200;
const LEFT_MAX = 280;
const RIGHT_MIN = 280;
const RIGHT_MAX = 400;
const MAIN_MIN = 520;
const MOBILE_BREAKPOINT = 768;
const MOBILE_NAV = 200;

/** Default share of viewport width (clamped by min/max above). */
const DEFAULT_LEFT_RATIO = 0.12;
const DEFAULT_RIGHT_RATIO = 0.2;

const prefetched = new Set<string>();

function prefetchRoute(key: string) {
  if (prefetched.has(key)) return;
  const load = ROUTE_PREFETCH[key];
  if (!load) return;
  prefetched.add(key);
  void load().catch(() => {
    prefetched.delete(key);
  });
}

/** 系统空间图标：优先按主页路由反查注册表（稳定），未注册主页再按门面兜底。 */
function systemSpaceIcon(ws: WorkspaceInfo): ReactNode {
  const Icon = systemViewByHome(ws.home_view)?.Icon ?? fallbackSpaceIcon(ws.storefront);
  return <Icon />;
}

/** 侧边栏「对话空间」判定：内核没给回已注册的系统主页，即按对话空间处理
 *  （仓库门面 + daily 会话型）。有 home_view 且能指回注册表的空间
 *  （用量/配置/消息/记录）一律走系统空间组落各自主页。 */
function isConversationSpace(ws: WorkspaceInfo): boolean {
  return !systemViewByHome(ws.home_view);
}

/** 侧边栏空间菜单（docs/空间模型与内容注册表.md §3）：全部空间统一从注册表渲染，
 *  上组=对话空间（点击真切换现场落对话页），下组=系统空间（点击落各自主页），
 *  中间分隔线。对话空间行 hover 出「移出」按钮——系统空间属于软件自身，不可移出。 */
function buildSpaceMenuItems({
  workspaces,
  updatesPending,
  hoveredSpace,
  onHoverSpace,
  onRemoveSpace,
}: {
  workspaces: WorkspaceInfo[];
  updatesPending: number;
  hoveredSpace: string | null;
  onHoverSpace: (name: string | null) => void;
  onRemoveSpace: (ws: WorkspaceInfo) => void;
}): NonNullable<React.ComponentProps<typeof Menu>["items"]> {
  const convItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [];
  const sysItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [];
  for (const ws of workspaces) {
    if (isConversationSpace(ws)) {
      const deletable = !ws.active;
      const hovered = hoveredSpace === ws.name;
      convItems.push({
        key: `space:${ws.name}`,
        icon: <MessageOutlined />,
        title: ws.summary || ws.name,
        label: (
          <span
            style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", minWidth: 0 }}
            onMouseEnter={() => onHoverSpace(ws.name)}
            onMouseLeave={() => onHoverSpace(null)}
          >
            <span
              style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            >
              {ws.name}
            </span>
            {deletable ? (
              <DeleteOutlined
                aria-label={`移出工作空间 ${ws.name}`}
                title="移出工作空间"
                onClick={(event) => {
                  // 行本身是「切换空间」——移出按钮不能顺带触发切换
                  event.stopPropagation();
                  event.preventDefault();
                  onRemoveSpace(ws);
                }}
                style={{
                  flexShrink: 0,
                  fontSize: 12,
                  color: "var(--coara-text-tertiary)",
                  opacity: hovered ? 0.85 : 0,
                  transition: "opacity 0.15s",
                  cursor: "pointer",
                }}
              />
            ) : null}
          </span>
        ),
      });
    } else {
      let label: ReactNode = ws.name;
      if (ws.home_view === "/review") {
        label =
          updatesPending > 0 ? (
            <Badge count={updatesPending} size="small" offset={[6, 0]}>
              {ws.name}
            </Badge>
          ) : (
            ws.name
          );
      }
      if (ws.home_view && ROUTE_PREFETCH[ws.home_view]) {
        const home = ws.home_view;
        label = (
          <span onMouseEnter={() => prefetchRoute(home)} onFocus={() => prefetchRoute(home)}>
            {label}
          </span>
        );
      }
      sysItems.push({
        key: `home:${ws.name}`,
        icon: systemSpaceIcon(ws),
        label,
        title: ws.summary || ws.name,
      });
    }
  }
  if (convItems.length === 0) {
    convItems.push({ key: "/chat", icon: <FolderOutlined />, label: "对话" });
  }
  const items: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [...convItems];
  if (sysItems.length > 0) {
    items.push({ type: "divider" }, ...sysItems);
  }
  return items;
}

/** 目录路径末段（作为登记名兜底）。 */
function pathBasename(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

type SideRatios = { leftRatio: number; rightRatio: number };

function clamp(n: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, n));
}

function viewportWidth(): number {
  return typeof window !== "undefined" ? window.innerWidth : 1440;
}

function defaultRatios(): SideRatios {
  return { leftRatio: DEFAULT_LEFT_RATIO, rightRatio: DEFAULT_RIGHT_RATIO };
}

/** Ratio → preferred px at this viewport (before MAIN_MIN fit). */
function preferredPx(vw: number, ratios: SideRatios): { left: number; right: number } {
  return {
    left: clamp(Math.round(ratios.leftRatio * vw), LEFT_MIN, LEFT_MAX),
    right: clamp(Math.round(ratios.rightRatio * vw), RIGHT_MIN, RIGHT_MAX),
  };
}

/**
 * Preferred px → display px.
 * Enlarge / roomy: keep preferred (middle grows).
 * Shrink past MAIN_MIN: scale sides down toward mins.
 */
function fitSides(vw: number, preferred: { left: number; right: number }): {
  left: number;
  right: number;
} {
  let left = preferred.left;
  let right = preferred.right;
  const sideBudget = Math.max(LEFT_MIN + RIGHT_MIN, vw - MAIN_MIN);
  if (left + right <= sideBudget) {
    return { left, right };
  }
  const scale = sideBudget / (left + right);
  left = Math.max(LEFT_MIN, Math.floor(left * scale));
  right = Math.max(RIGHT_MIN, Math.floor(right * scale));
  if (left + right > sideBudget) {
    left = LEFT_MIN;
    right = Math.max(0, sideBudget - left);
  }
  return { left, right };
}

function layoutForViewport(vw: number, ratios: SideRatios): { left: number; right: number } {
  return fitSides(vw, preferredPx(vw, ratios));
}

function readRatios(): SideRatios {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "");
    const leftRatio = Number(raw?.leftRatio);
    const rightRatio = Number(raw?.rightRatio);
    if (!Number.isFinite(leftRatio) || !Number.isFinite(rightRatio)) {
      return defaultRatios();
    }
    return {
      leftRatio: clamp(leftRatio, 0.05, 0.35),
      rightRatio: clamp(rightRatio, 0.08, 0.4),
    };
  } catch {
    return defaultRatios();
  }
}

function persistRatios(ratios: SideRatios) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(ratios));
  } catch {
    /* ignore quota / private mode */
  }
}

interface AppLayoutProps {
  children: ReactNode;
  connected: boolean;
}

export function AppLayout({ children }: AppLayoutProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const workspaces = useStore((s) => s.workspaces);
  const activeName = useStore((s) => s.activeName);
  const setWorkspaces = useStore((s) => s.setWorkspaces);
  const resetForWorkspaceSwitch = useStore((s) => s.resetForWorkspaceSwitch);
  const hydrateToolActivity = useStore((s) => s.hydrateToolActivity);
  const updatesPending = useStore((s) => s.updatesPending);
  const setUpdatesPending = useStore((s) => s.setUpdatesPending);
  const account = useStore((s) => s.account);
  const switchingRef = useRef(false);
  // 侧栏空间增删：hover 行才显示移出按钮；添加工作空间走同一份登记表接口
  const [hoveredSpace, setHoveredSpace] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addBusy, setAddBusy] = useState(false);

  const bootVw = viewportWidth();
  const ratiosRef = useRef(readRatios());
  const [isMobile, setIsMobile] = useState(bootVw <= MOBILE_BREAKPOINT);
  const bootLayout = layoutForViewport(bootVw, ratiosRef.current);
  const [left, setLeft] = useState(bootLayout.left);
  const [right, setRight] = useState(bootLayout.right);

  useEffect(() => {
    const onResize = () => {
      const vw = window.innerWidth;
      setIsMobile(vw <= MOBILE_BREAKPOINT);
      if (vw <= MOBILE_BREAKPOINT) return;
      const fitted = layoutForViewport(vw, ratiosRef.current);
      setLeft(fitted.left);
      setRight(fitted.right);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useEffect(() => {
    fetchWorkspaceList()
      .then((data) => {
        setWorkspaces(data.workspaces, data.active_name);
      })
      .catch((err) => {
        console.error("Failed to load workspace list:", err);
      });
  }, [setWorkspaces]);

  // 消息角标：进入即拉一次，之后 30s 轮询（操作后页面会主动刷新）
  useEffect(() => {
    const tick = () => {
      fetchUpdatesSummary()
        .then((s) => setUpdatesPending(s.pending))
        .catch(() => undefined);
    };
    tick();
    const timer = window.setInterval(tick, 30_000);
    return () => window.clearInterval(timer);
  }, [setUpdatesPending]);

  const applyDragSizes = (sizes: number[]) => {
    const nextLeft = clamp(Number(sizes[0]) || left, LEFT_MIN, LEFT_MAX);
    const nextRight = clamp(Number(sizes[2]) || right, RIGHT_MIN, RIGHT_MAX);
    setLeft(nextLeft);
    setRight(nextRight);
    return { left: nextLeft, right: nextRight };
  };

  // 对话空间真切换（方案 A）：换 web 视图 WorkspaceSession 现场，落对话页。
  // activeName/active 立即用目标名更新（不等二次往返），后台 fetch 只做元数据校正
  // ——不用全局 active_name 覆盖（web 独立视图不动全局前台），视图名以心跳 runtime 为准。
  const handleSpaceSwitch = async (name: string) => {
    if (switchingRef.current) return;
    switchingRef.current = true;
    try {
      if (name !== activeName) {
        const result = await switchWorkspace(name);
        const dir = String(result?.workspace_dir || "").trim();
        if (!dir) {
          message.error("切换成功但未收到空间标识，请刷新页面");
          return;
        }
        // 响应自带目标空间快照：就地上屏（切完只出现「骨架 → 目标内容」两种画面，
        // 不必再等一次 hydrate）；旧服务端没有 snapshot 时退回原流程。
        resetForWorkspaceSwitch(dir, result.snapshot ?? null);
        setWorkspaces(
          useStore.getState().workspaces.map((ws) => ({ ...ws, active: ws.name === name })),
          name,
        );
        void hydrateToolActivity();
        fetchWorkspaceList()
          .then((data) =>
            setWorkspaces(
              data.workspaces.map((ws) => ({ ...ws, active: ws.name === name })),
              name,
            ),
          )
          .catch(() => {});
      }
      navigate("/chat");
    } catch (err) {
      console.error("Failed to switch workspace:", err);
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      switchingRef.current = false;
    }
  };

  // 系统空间主页的视图切换（不跳对话页）：与 handleSpaceSwitch 同机制切 web 视图
  // WorkspaceSession，但停留各自主页（记录页浮窗的会话主体即该空间会话）
  const handleSpaceViewSwitch = async (name: string) => {
    if (!name || name === activeName || switchingRef.current) return;
    switchingRef.current = true;
    try {
      const result = await switchWorkspace(name);
      const dir = String(result?.workspace_dir || "").trim();
      if (!dir) {
        message.error("切换成功但未收到空间标识，请刷新页面");
        return;
      }
      // 同上：快照随响应回来就一次提交上屏，没有则退回「清空 + 等 hydrate」。
      resetForWorkspaceSwitch(dir, result.snapshot ?? null);
      setWorkspaces(
        useStore.getState().workspaces.map((ws) => ({ ...ws, active: ws.name === name })),
        name,
      );
      void hydrateToolActivity();
      fetchWorkspaceList()
        .then((data) =>
          setWorkspaces(
            data.workspaces.map((ws) => ({ ...ws, active: ws.name === name })),
            name,
          ),
        )
        .catch(() => {});
    } catch (err) {
      console.error("Failed to switch workspace view:", err);
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      switchingRef.current = false;
    }
  };

  // 登记表变更后立刻拉一次列表（服务端也会推 workspaces_changed，这里给即时反馈）
  const refreshWorkspaceList = () => {
    fetchWorkspaceList()
      .then((data) => setWorkspaces(data.workspaces, data.active_name))
      .catch(() => undefined);
  };

  const handleAddWorkspace = async () => {
    const path = addPath.trim();
    const name = addName.trim() || pathBasename(path);
    if (!path) {
      message.warning("请填写工作空间目录（目录需已存在）");
      return;
    }
    setAddBusy(true);
    try {
      await createWorkspaceEntry(name, path);
      message.success(`已添加工作空间 ${name}`);
      setAddOpen(false);
      setAddName("");
      setAddPath("");
      refreshWorkspaceList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setAddBusy(false);
    }
  };

  // 移出 = 只取消登记（磁盘不动）；当前正在查看的空间不出按钮，故无需额外守卫
  const handleRemoveWorkspace = (ws: WorkspaceInfo) => {
    Modal.confirm({
      title: `移出工作空间「${ws.name}」`,
      content: "只取消登记，磁盘目录与文件保持不动。",
      okText: "移出",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await removeWorkspaceEntry(ws.name);
          message.success(`已移出工作空间 ${ws.name}`);
          refreshWorkspaceList();
        } catch (err) {
          message.error(err instanceof Error ? err.message : String(err));
        }
      },
    });
  };

  // 选中项：系统空间主页按 home:<name>，对话页按当前视图空间 space:<name>
  const homePrefix = HOME_PREFIXES.find((prefix) => location.pathname.startsWith(prefix));
  const homeEntry = homePrefix
    ? workspaces.find((ws) => ws.home_view === homePrefix)
    : undefined;
  const selectedKey = homePrefix
    ? homeEntry
      ? `home:${homeEntry.name}`
      : ""
    : `space:${activeName ?? ""}`;

  // 左下角个人入口：图标 + 一行状态（未登录 / 邮箱），与上方菜单项同款扁平样式
  const meActive =
    location.pathname.startsWith("/me") || location.pathname.startsWith("/login");
  const meEntry = (
    <Tooltip
      title={account?.logged_in ? account.device_name || "已登录" : "点击登录"}
      placement="right"
    >
      <div
        onClick={() => {
          // 已在个人页（或登录页）时点击不响应——避免重置页签并触发重新加载
          if (meActive) return;
          prefetchRoute("/me");
          startTransition(() => {
            navigate("/me");
          });
        }}
        onMouseEnter={() => prefetchRoute("/me")}
        style={{
          height: 40,
          margin: "4px 8px",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          borderRadius: 8,
          background: meActive ? "var(--coara-accent-subtle)" : "transparent",
          color: meActive ? "var(--coara-accent)" : "var(--coara-text)",
          flexShrink: 0,
        }}
      >
        <UserOutlined style={{ fontSize: 16 }} />
        <span
          style={{
            marginLeft: 10,
            fontSize: 13,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            color: account?.logged_in ? "inherit" : "var(--coara-text-tertiary)",
          }}
        >
          {account?.logged_in ? account.email || "已登录" : "未登录"}
        </span>
      </div>
    </Tooltip>
  );

  const navPanel = (
    <div
      style={{
        height: "100%",
        overflow: "auto",
        display: "flex",
        flexDirection: "column",
        background: "var(--coara-surface)",
      }}
    >
      <div style={{ padding: "8px 8px 0", flexShrink: 0 }}>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          style={{
            width: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            padding: "7px 10px",
            borderRadius: 8,
            border: "1px dashed var(--coara-border)",
            background: "transparent",
            color: "var(--coara-text-secondary)",
            fontSize: 13,
            cursor: "pointer",
          }}
        >
          <PlusOutlined style={{ fontSize: 12 }} />
          添加工作空间
        </button>
      </div>
      <Menu
        mode="inline"
        selectedKeys={selectedKey ? [selectedKey] : []}
        items={buildSpaceMenuItems({
          workspaces,
          updatesPending,
          hoveredSpace,
          onHoverSpace: setHoveredSpace,
          onRemoveSpace: handleRemoveWorkspace,
        })}
        onClick={({ key }) => {
          const keyStr = String(key);
          // space:<name> = 对话空间（真切换现场落对话页）；home:<name> = 系统空间
          // （navigate 到其主页，页面内对话维持模块助手机制不变）；/chat 为空表兜底。
          if (keyStr.startsWith("space:")) {
            void handleSpaceSwitch(keyStr.slice("space:".length));
          } else if (keyStr.startsWith("home:")) {
            const target = workspaces.find((ws) => ws.name === keyStr.slice("home:".length));
            const home = target?.home_view?.trim();
            if (!home) return;
            prefetchRoute(home);
            startTransition(() => {
              navigate(home);
            });
            // 系统空间：点击主页同时切换 web 视图到该空间——页面内对话浮窗的
            // 会话主体是该空间的 WorkspaceSession，不是前台空间
            void handleSpaceViewSwitch(target?.name ?? "");
          } else if (keyStr === "/chat") {
            navigate("/chat");
          }
        }}
        style={{ borderRight: 0, paddingTop: 4, flex: 1, background: "transparent" }}
      />
      {meEntry}
      <Modal
        open={addOpen}
        title="添加工作空间"
        okText="添加"
        cancelText="取消"
        confirmLoading={addBusy}
        onOk={() => void handleAddWorkspace()}
        onCancel={() => setAddOpen(false)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 6 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>目录</span>
            <Input
              value={addPath}
              onChange={(e) => setAddPath(e.target.value)}
              placeholder="例如 D:\code_ws\my-project（目录需已存在）"
              onPressEnter={() => void handleAddWorkspace()}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
              名称（留空取目录名）
            </span>
            <Input value={addName} onChange={(e) => setAddName(e.target.value)} placeholder="可选" />
          </label>
        </div>
      </Modal>
    </div>
  );

  const mainPanel = (
    <Content
      style={{
        height: "100%",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        background: "var(--coara-surface)",
      }}
    >
      {children}
    </Content>
  );

  if (isMobile) {
    return (
      <Layout style={{ height: "100vh" }}>
        <div style={{ width: MOBILE_NAV, flexShrink: 0, height: "100%" }}>{navPanel}</div>
        {mainPanel}
      </Layout>
    );
  }

  // 统一两栏布局：左导航 + 主区。状态栏是对话页的一部分（见 ChatView），
  // 不在此公共布局渲染；其它子模块整页独立。
  return (
    <Layout style={{ height: "100vh" }}>
      <Splitter
        className="coara-app-splitter"
        style={{ height: "100%", width: "100%" }}
        onResize={(sizes) => {
          applyDragSizes(sizes);
        }}
        onResizeEnd={(sizes) => {
          const next = applyDragSizes(sizes);
          const vw = Math.max(1, window.innerWidth);
          const ratios = {
            leftRatio: next.left / vw,
            rightRatio: next.right / vw,
          };
          ratiosRef.current = ratios;
          persistRatios(ratios);
        }}
      >
        <Splitter.Panel size={left} min={LEFT_MIN} max={LEFT_MAX}>
          {navPanel}
        </Splitter.Panel>
        <Splitter.Panel min={MAIN_MIN}>{mainPanel}</Splitter.Panel>
      </Splitter>
    </Layout>
  );
}
