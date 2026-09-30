// 冻结共享基线（《空间能力系统》槽位四地基）：壳入口把 React 生态与壳 API
// 挂到 window.__COARA_BASELINE__，插件 bundle 从这里取共享实例。
//
// 为什么不用构建期垫片/import map：壳产物是应用构建，垫片入口无人引用会被
// rollup treeshake 摇掉；运行时全局赋值对构建器零依赖，永远不会失效。
// 基线表只增不减——插件按名字引用，删名字=破坏存量插件。

import * as React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as ReactRouterDOM from "react-router-dom";
import * as zustand from "zustand";
import * as antd from "antd";
import * as antdIcons from "@ant-design/icons";
import { useStore } from "./store";
import { getAuthToken, tokenQueryFragment } from "./auth";
import { tokens, cssVars, coaraAntdTheme } from "../theme/tokens";
import { PageShell } from "../components/layout/PageShell";
import { PageHeader } from "../components/layout/PageHeader";
import { ModuleChatFloat } from "../features/chat/ModuleChatFloat";
import { EmptyState, ErrorState, LoadingState } from "../components/states/States";
import { formatDateTime } from "./format";
import {
  archiveUpdate,
  fetchUpdatesList,
  fetchUpdatesSummary,
  markUpdateRead,
  reviewUpdate,
  switchWorkspace,
} from "./api";
import { fetchUsageDashboard, fetchUsagePricing, updateUsagePricing } from "./api";
import {
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
import { fileViewRoute } from "./fileLink";
import { flowGraphKey } from "./chatTypes";
import { getWS } from "./ws";
import {
  createWorkflowDraft,
  deleteWorkflowDraft,
  fetchModuleSessionMessages,
  fetchWorkflowDraft,
  fetchWorkflowDrafts,
  parseWdl,
  emitWdl,
  reportActiveWorkflowDraft,
  saveWorkflowDraft,
  DraftConflictError,
} from "./api";
import {
  archiveRecord,
  collectionFileUrl,
  deleteRecord,
  fetchCollectionEntry,
  fetchCollectionList,
  fetchRecentFiles,
  fetchRecordEntry,
  fetchRecordList,
  fileRawUrl,
  unarchiveRecord,
} from "./api";
import { formatDateGroupKey, formatDateGroupLabel, formatTimeHm } from "./format";
import { RosterGroup, RosterRow, RosterSection, rosterRowMeta, rosterRowTitle } from "../components/layout/Roster";
import { MarkdownMessage } from "../features/chat/MessageList";

declare global {
  interface Window {
    __COARA_BASELINE__?: Record<string, unknown>;
  }
}

let installed = false;

/** 应用入口调用一次：冻结基线挂全局。幂等。 */
export function installBaseline(): void {
  if (installed) return;
  installed = true;
  window.__COARA_BASELINE__ = {
    React,
    ReactDOM,
    ReactDOMClient,
    ReactRouterDOM,
    zustand,
    antd,
    antdIcons,
    // 壳 API：插件与壳共享同一份 store 单例（不共享=两套状态，数据永远对不上）
    useStore,
    getAuthToken,
    tokenQueryFragment,
    tokens,
    cssVars,
    coaraAntdTheme,
    PageShell,
    PageHeader,
    ModuleChatFloat,
    EmptyState,
    ErrorState,
    LoadingState,
    formatDateTime,
    // 页面域 API（迁插件的内置页经基线取，不重复打包 fetch 逻辑）
    fetchUsageDashboard,
    fetchUsagePricing,
    updateUsagePricing,
    archiveUpdate,
    fetchUpdatesList,
    fetchUpdatesSummary,
    markUpdateRead,
    reviewUpdate,
    switchWorkspace,
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
    fileViewRoute,
    flowGraphKey,
    getWS,
    createWorkflowDraft,
    deleteWorkflowDraft,
    fetchModuleSessionMessages,
    fetchWorkflowDraft,
    fetchWorkflowDrafts,
    parseWdl,
    emitWdl,
    reportActiveWorkflowDraft,
    saveWorkflowDraft,
    DraftConflictError,
    archiveRecord,
    collectionFileUrl,
    deleteRecord,
    fetchCollectionEntry,
    fetchCollectionList,
    fetchRecentFiles,
    fetchRecordEntry,
    fetchRecordList,
    fileRawUrl,
    unarchiveRecord,
    formatDateGroupKey,
    formatDateGroupLabel,
    formatTimeHm,
    RosterGroup,
    RosterRow,
    RosterSection,
    rosterRowMeta,
    rosterRowTitle,
    MarkdownMessage,
  };
}
