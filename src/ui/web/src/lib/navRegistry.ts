import type { ComponentType } from "react";
import {
  MessageOutlined,
  BookOutlined,
  ApartmentOutlined,
  AuditOutlined,
  BarChartOutlined,
  SettingOutlined,
} from "@ant-design/icons";

/**
 * 系统视图注册表（docs/Web设计体系.md §1、docs/空间性质模型.md §6）。
 *
 * 系统侧视图是**有限且关于软件自身**的（对话 / 消息 / 记录 / 用量 / 配置），
 * 集中登记于此，供三处消费，避免各写一份前缀表、图标 switch、预取表：
 *   1. 侧边栏选中项判定（`HOME_PREFIXES` 前缀匹配）
 *   2. 系统空间图标（按 `WorkspaceInfo.home_view` 反查 `Icon`）
 *   3. hover 预取（`ROUTE_PREFETCH`）
 *
 * 用户空间是**无限**的：内核在 `WorkspaceInfo.home_view` 里指回这里某个 `path`；
 * 指不回来的（无 home_view 或未注册）一律按对话空间处理。
 */
interface SystemView {
  /** 路由前缀，同时是 `WorkspaceInfo.home_view` 的取值 */
  path: string;
  /** 未识别空间名的兜底展示名 */
  label: string;
  Icon: ComponentType;
  /** 懒加载器（hover 预取用）；null 表示已随主包加载、无需预取 */
  load: (() => Promise<unknown>) | null;
}

const SYSTEM_VIEWS: readonly SystemView[] = [
  { path: "/chat", label: "对话", Icon: MessageOutlined, load: null },
  { path: "/review", label: "消息", Icon: AuditOutlined, load: () => import("../views/ReviewView") },
  { path: "/records", label: "记录", Icon: BookOutlined, load: () => import("../views/RecordsView") },
  { path: "/workflow", label: "工作流", Icon: ApartmentOutlined, load: () => import("../views/WorkflowView") },
  { path: "/usage", label: "用量", Icon: BarChartOutlined, load: () => import("../views/UsageView") },
  { path: "/config", label: "配置", Icon: SettingOutlined, load: () => import("../views/ConfigView") },
];

/** 「主页」前缀：系统视图中除对话外都可作空间主页（对话是兜底入口）。 */
export const HOME_PREFIXES: readonly string[] = SYSTEM_VIEWS.filter(
  (v) => v.path !== "/chat",
).map((v) => v.path);

/** 按 `home_view` 反查系统视图；未注册返回 undefined。 */
export function systemViewByHome(homeView: string | undefined | null): SystemView | undefined {
  if (!homeView) return undefined;
  return SYSTEM_VIEWS.find((v) => v.path === homeView);
}

/** 内核给了未注册的 home_view 时的图标兜底（按门面形态）。 */
export function fallbackSpaceIcon(storefront?: string): ComponentType {
  return storefront === "display" ? BarChartOutlined : SettingOutlined;
}

/** 非系统视图的按需预取路由（个人页 / 文件查看器）。 */
const EXTRA_PREFETCH: Record<string, () => Promise<unknown>> = {
  "/me": () => import("../views/PersonalView"),
  "/file": () => import("../views/FileView"),
};

/** 路由 → 懒加载器的统一预取表：系统视图（有 load 的）+ 附加路由。 */
export const ROUTE_PREFETCH: Record<string, () => Promise<unknown>> = {
  ...Object.fromEntries(
    SYSTEM_VIEWS.filter((v) => v.load).map((v) => [v.path, v.load as () => Promise<unknown>]),
  ),
  ...EXTRA_PREFETCH,
};
