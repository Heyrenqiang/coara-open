// 壳 API 门面（《空间能力系统》槽位四）：插件可用的壳能力聚合点。
//
// 双构建共用一份契约（别名 "coara:shell"）：
// - 主构建（vite.config.ts）alias 到本文件——真模块，静态进 bundle
// - 插件构建（plugin-kit/vite.config.mjs）alias 到全局垫片——运行时从
//   window.__COARA_BASELINE__ 取同一份实例
// 内置页（usage/config/workflow 等迁插件的页）源码统一 import "coara:shell"，
// 不改源码即可两种构建各取所需。

export { useStore } from "./store";
export { getAuthToken, tokenQueryFragment } from "./auth";
export { tokens, cssVars, coaraAntdTheme } from "../theme/tokens";
export { PageShell } from "../components/layout/PageShell";
export { PageHeader } from "../components/layout/PageHeader";
export type { WorkspaceInfo, ChatMessage, RuntimeInfo } from "./chatTypes";

// 用量域 API（usage 插件页用）
export {
  fetchUsageDashboard,
  fetchUsagePricing,
  updateUsagePricing,
} from "./api";
export type {
  UsageAgentKindRow,
  UsageDashboardResponse,
  UsageDayRow,
  UsageModelRow,
  UsagePricingEntry,
  UsageTokenTotals,
  UsageWorkspaceRow,
} from "./api";

// antd/图标库命名空间（两种构建下形态一致：主构建=真命名空间，插件构建=
// 基线命名空间）。插件页解构取用，不写 default import（ESM/CJS 互操作陷阱）。
import * as _antd from "antd";
import * as _antdIcons from "@ant-design/icons";

export const antd = _antd;
export const antdIcons = _antdIcons;
export type { ColumnsType } from "antd/es/table";

// 模块对话浮窗（agentic 模块页共用）
export { ModuleChatFloat } from "../features/chat/ModuleChatFloat";

// 状态块组件（加载/空态/错误占位）
export { LoadingState, EmptyState, ErrorState } from "../components/states/States";

// 格式化助手（只导页面实际用的）
export { formatDateTime } from "./format";

// 消息/动态域 API（review 插件页用）
export {
  archiveUpdate,
  fetchUpdatesList,
  fetchUpdatesSummary,
  markUpdateRead,
  reviewUpdate,
  switchWorkspace,
} from "./api";
export type { UpdateItem, UpdateSalience, UpdateStatus } from "./api";

// 配置域 API（config 插件页用）
export {
  fetchAutostart,
  fetchConfig,
  fetchDefaultModel,
  fetchEventSources,
  fetchModelChoices,
  fetchProviders,
  fetchReminders,
  fetchSkillContent,
  fetchSkills,
  fetchTools,
  fetchUserRules,
  fetchWorkspaces,
  openUserRulesFile,
  saveConfig,
  saveDefaultModel,
  saveProviders,
  saveSkillContent,
  saveToolCredentials,
  setAutostart,
  setSkillDeferred,
} from "./api";
export type {
  AutostartState,
  ConfigEnvelope,
  DefaultModelState,
  EventSourceRow,
  ModelChoice,
  ProviderConfig,
  ReminderRow,
  SkillContent,
  ToolCredentialField,
  ToolItem,
  UserRulesState,
  WorkspaceRow,
} from "./api";

// 文件视图路由助手（配置页「编辑文件」入口用）
export { fileViewRoute } from "./fileLink";

// 记录域（records 插件页用）
export {
  archiveRecord,
  collectionFileUrl,
  deleteRecord,
  fetchCollectionEntry,
  fetchCollectionList,
  fetchRecordEntry,
  fetchRecordList,
  unarchiveRecord,
} from "./api";
export type { CollectionEntry, RecordEntry } from "./api";
export { fetchRecentFiles, fileRawUrl } from "./api";
export type { RecentFileEntry } from "./api";
export { formatDateGroupKey, formatDateGroupLabel, formatTimeHm } from "./format";
export { RosterGroup, RosterRow, RosterSection, rosterRowMeta, rosterRowTitle } from "../components/layout/Roster";
export { MarkdownMessage } from "../features/chat/MessageList";

// 工作流域（workflow 插件页用）
export { flowGraphKey } from "./chatTypes";
export { getWS } from "./ws";
export {
  createWorkflowDraft,
  deleteWorkflowDraft,
  fetchModuleSessionMessages,
  fetchWorkflowDraft,
  fetchWorkflowDrafts,
  parseWdl,
  emitWdl,
  reportActiveWorkflowDraft,
  saveWorkflowDraft,
} from "./api";
export { DraftConflictError } from "./api";
export type {
  KernelDocument,
  KernelEdgeSpec,
  KernelNodeSpec,
  WorkflowDraft,
} from "./api";
export type { FlowLiveNode, FlowLiveNodeStatus } from "./ws";
