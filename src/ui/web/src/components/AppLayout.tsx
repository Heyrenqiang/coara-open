import { useEffect, useRef, useState, startTransition, type ReactNode } from "react";
import { Layout, Menu, Splitter, Badge, Tooltip, Input, Modal, message, Button, List } from "antd";
import {
  ArrowUpOutlined,
  DeleteOutlined,
  FolderOutlined,
  MessageOutlined,
  PlusOutlined,
  UserOutlined,
  WarningOutlined,
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
  browseFs,
  fetchUpdatesSummary,
  fetchWorkspaceList,
  rebindWorkspaceEntry,
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

/** 侧边栏「创作者空间」判定：开发者给用户的示例工作空间（工作流），比普通
 *  空间高级但不是配置/消息那种纯系统空间——独立成组，居普通空间与系统空间之间。 */
function isWorkflowSpace(ws: WorkspaceInfo): boolean {
  return ws.home_view === "/workflow";
}

/** 侧边栏「对话空间」判定：内核没给回已注册的系统主页，即按对话空间处理
 *  （仓库门面 + daily 会话型）。工作流分流到创作者组，不在此列。 */
function isConversationSpace(ws: WorkspaceInfo): boolean {
  return !systemViewByHome(ws.home_view) && !isWorkflowSpace(ws);
}

/** 侧边栏空间菜单（docs/空间模型与内容注册表.md §3）：全部空间统一从注册表渲染，
 *  上组=对话空间（点击真切换现场落对话页），中组=创作者空间（工作流，点击落画布主页），
 *  下组=系统空间（点击落各自主页），组间分隔线。对话空间行 hover 出「移出」按钮
 *  ——系统空间属于软件自身，不可移出。 */
function buildSpaceMenuItems({
  workspaces,
  updatesPending,
  onRemoveSpace,
  onMissingSpace,
}: {
  workspaces: WorkspaceInfo[];
  updatesPending: number;
  onRemoveSpace: (ws: WorkspaceInfo) => void;
  onMissingSpace: (ws: WorkspaceInfo) => void;
}): NonNullable<React.ComponentProps<typeof Menu>["items"]> {
  const convItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [];
  const workflowItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [];
  const sysItems: NonNullable<React.ComponentProps<typeof Menu>["items"]> = [];
  for (const ws of workspaces) {
    if (isWorkflowSpace(ws)) {
      // 创作者空间（工作流）：同系统空间一样点击落画布主页，但独立成组居中
      const home = ws.home_view?.trim();
      workflowItems.push({
        key: `home:${ws.name}`,
        icon: systemSpaceIcon(ws),
        label: home && ROUTE_PREFETCH[home] ? (
          <span onMouseEnter={() => prefetchRoute(home)} onFocus={() => prefetchRoute(home)}>
            {ws.name}
          </span>
        ) : (
          ws.name
        ),
        title: ws.summary || ws.name,
      });
      continue;
    }
    if (isConversationSpace(ws)) {
      const deletable = !ws.active && !ws.missing;
      // 目录已删的空间：置灰 + 叹号标注，点击不切换，走恢复流程（改绑/移除）
      const nameLabel = ws.missing ? (
        <span style={{ display: "flex", alignItems: "center", gap: 6, flex: 1, minWidth: 0 }}>
          <span
            style={{
              flex: 1,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              color: "var(--coara-text-tertiary)",
            }}
          >
            {ws.name}
          </span>
          <Tooltip title={`目录不存在：${ws.path}`}>
            <WarningOutlined style={{ flexShrink: 0, fontSize: 12, color: "var(--coara-danger)" }} />
          </Tooltip>
        </span>
      ) : (
        <span
          style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {ws.name}
        </span>
      );
      convItems.push({
        key: `space:${ws.name}`,
        icon: <MessageOutlined style={ws.missing ? { color: "var(--coara-text-tertiary)" } : undefined} />,
        title: ws.missing ? `目录不存在：${ws.path}` : ws.summary || ws.name,
        label: (
          <span
            className="workspace-space-row"
            style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", minWidth: 0 }}
            onClickCapture={ws.missing ? (event) => {
              event.stopPropagation();
              event.preventDefault();
              onMissingSpace(ws);
            } : undefined}
          >
            {nameLabel}
            {deletable ? (
              <DeleteOutlined
                aria-label={`移出工作空间 ${ws.name}`}
                title="移出工作空间"
                className="workspace-space-delete"
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
  if (workflowItems.length > 0) {
    items.push({ type: "divider" }, ...workflowItems);
  }
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

/** 名称拼进路径前的净化：去掉路径非法字符与首尾空白/点。 */
function sanitizeDirName(name: string): string {
  return name
    .replace(/[\\/:*?"<>|]/g, "")
    .replace(/[\u0000-\u001f]/g, "")
    .trim()
    .replace(/^\.+|\.+$/g, "");
}

/** 拼接目录：根路径已用 \\ 还是 / 保持其风格。 */
function joinDir(root: string, leaf: string): string {
  const sep = root.includes("/") && !root.includes("\\") ? "/" : "\\";
  return root.replace(/[\\/]+$/, "") + sep + leaf;
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
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  // 名称/目录双向自动联动：各记「是否被用户手动改过」，手动改过的项脱离联动
  const nameManualRef = useRef(false);
  const pathManualRef = useRef(false);
  const recommendRootRef = useRef("");
  // 目录选择器（次级弹窗，服务端驱动逐级下钻）
  const [browseOpen, setBrowseOpen] = useState(false);
  const [browsePath, setBrowsePath] = useState("");
  const [browseParent, setBrowseParent] = useState<string | null>(null);
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([]);
  const [browseBusy, setBrowseBusy] = useState(false);
  // 目录已删空间的恢复对话框：改绑新目录（rebindWorkspaceEntry）
  const [rebindTarget, setRebindTarget] = useState<WorkspaceInfo | null>(null);
  const [rebindPath, setRebindPath] = useState("");
  const [rebindBusy, setRebindBusy] = useState(false);

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
    const name = addName.trim();
    if (!name) {
      message.warning("请填写工作空间名称");
      return;
    }
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
      nameManualRef.current = false;
      pathManualRef.current = false;
      refreshWorkspaceList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setAddBusy(false);
    }
  };

  // 打开弹窗：重置双自动态，并预取推荐根目录（最长公共父目录或 cwd）
  const openAddModal = () => {
    setAddName("");
    setAddPath("");
    nameManualRef.current = false;
    pathManualRef.current = false;
    setAddOpen(true);
    browseFs("")
      .then((data) => {
        recommendRootRef.current = data.path;
      })
      .catch(() => {
        recommendRootRef.current = "";
      });
  };

  // 名称 onChange：用户输入即置手动态；目录仍自动态时联动默认目录
  const onAddNameChange = (value: string) => {
    nameManualRef.current = true;
    setAddName(value);
    if (!pathManualRef.current) {
      const root = recommendRootRef.current;
      const leaf = sanitizeDirName(value);
      setAddPath(root && leaf ? joinDir(root, leaf) : "");
    }
  };

  // 目录 onChange：用户输入即置手动态；名称仍自动态时联动尾段目录名
  const onAddPathChange = (value: string) => {
    pathManualRef.current = true;
    setAddPath(value);
    if (!nameManualRef.current && value.trim()) {
      setAddName(pathBasename(value.trim()));
    }
  };

  // 目录选择器：浏览某级目录（path 空 = 推荐根目录）
  const browseSeqRef = useRef(0);
  const loadBrowse = (path: string) => {
    // 代际守卫：连点目录行/上级时只应用最新一次响应，慢响应不覆盖快响应
    const mySeq = ++browseSeqRef.current;
    setBrowseBusy(true);
    browseFs(path)
      .then((data) => {
        if (mySeq !== browseSeqRef.current) return;
        setBrowsePath(data.path);
        setBrowseParent(data.parent);
        setBrowseDirs(data.dirs);
      })
      .catch((err) => {
        if (mySeq !== browseSeqRef.current) return;
        message.error(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (mySeq === browseSeqRef.current) setBrowseBusy(false);
      });
  };

  const openBrowseModal = () => {
    setBrowseOpen(true);
    // 优先从当前输入目录的父级开浏览，其次推荐根目录
    const current = addPath.trim();
    loadBrowse(current || recommendRootRef.current || "");
  };

  // 选定：回填路径（视为用户选定，脱离目录自动态）；名称仍自动态则联动目录名
  const confirmBrowse = () => {
    if (!browsePath) return;
    pathManualRef.current = true;
    setAddPath(browsePath);
    if (!nameManualRef.current) {
      setAddName(pathBasename(browsePath));
    }
    setBrowseOpen(false);
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

  // 目录已删空间的恢复入口：改绑到新目录（id 不变、历史档案不断链）或移出登记。
  const handleMissingSpace = (ws: WorkspaceInfo) => {
    setRebindTarget(ws);
    setRebindPath("");
  };

  const handleRebindConfirm = async () => {
    if (!rebindTarget) return;
    const path = rebindPath.trim();
    if (!path) {
      message.warning("请填写新目录（目录需已存在）");
      return;
    }
    setRebindBusy(true);
    try {
      await rebindWorkspaceEntry(rebindTarget.name, path);
      message.success(`已改绑到 ${path}`);
      setRebindTarget(null);
      refreshWorkspaceList();
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setRebindBusy(false);
    }
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
          onClick={openAddModal}
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
          onRemoveSpace: handleRemoveWorkspace,
          onMissingSpace: handleMissingSpace,
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
            <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>名称（必填）</span>
            <Input
              value={addName}
              onChange={(e) => onAddNameChange(e.target.value)}
              placeholder="给这个空间起个名字"
              onPressEnter={() => void handleAddWorkspace()}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>目录</span>
            <div style={{ display: "flex", gap: 8 }}>
              <Input
                value={addPath}
                onChange={(e) => onAddPathChange(e.target.value)}
                placeholder="例如 D:\code_ws\my-project（目录需已存在）"
                onPressEnter={() => void handleAddWorkspace()}
                style={{ flex: 1 }}
              />
              <Button onClick={openBrowseModal}>选择…</Button>
            </div>
          </label>
        </div>
      </Modal>
      <Modal
        open={browseOpen}
        title="选择目录"
        okText="选定此目录"
        cancelText="取消"
        onOk={confirmBrowse}
        onCancel={() => setBrowseOpen(false)}
        width={520}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 8, paddingTop: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Button
              size="small"
              icon={<ArrowUpOutlined />}
              disabled={!browseParent || browseBusy}
              onClick={() => browseParent && loadBrowse(browseParent)}
            >
              上级
            </Button>
            <span
              style={{
                flex: 1,
                fontSize: 12,
                color: "var(--coara-text-secondary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={browsePath}
            >
              {browsePath}
            </span>
          </div>
          <List
            size="small"
            loading={browseBusy}
            dataSource={browseDirs}
            locale={{ emptyText: browseBusy ? "加载中…" : "此目录下没有子目录" }}
            style={{ maxHeight: 320, overflow: "auto", border: "1px solid var(--coara-border)", borderRadius: 8 }}
            renderItem={(d) => (
              <List.Item
                style={{ cursor: "pointer", padding: "6px 12px" }}
                onClick={() => loadBrowse(d.path)}
              >
                <FolderOutlined style={{ marginRight: 8, color: "var(--coara-accent)" }} />
                <span style={{ fontSize: 13 }}>{d.name}</span>
              </List.Item>
            )}
          />
        </div>
      </Modal>
      <Modal
        open={rebindTarget !== null}
        title={`恢复空间「${rebindTarget?.name ?? ""}」`}
        okText="改绑到此目录"
        cancelText="取消"
        confirmLoading={rebindBusy}
        onOk={() => void handleRebindConfirm()}
        onCancel={() => setRebindTarget(null)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 6 }}>
          <div style={{ fontSize: 12, color: "var(--coara-text-secondary)" }}>
            原目录已不存在：{rebindTarget?.path}
          </div>
          <div style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
            项目若是挪了位置，改绑到新目录——空间身份与历史对话都保留；若项目已废弃，取消后从侧边栏移出登记即可。
          </div>
          <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>新目录</span>
            <Input
              value={rebindPath}
              onChange={(e) => setRebindPath(e.target.value)}
              placeholder="项目现在的目录（需已存在）"
              onPressEnter={() => void handleRebindConfirm()}
            />
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
