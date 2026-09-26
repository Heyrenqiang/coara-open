import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spin, Tabs, theme } from "antd";
import {
  fetchConfig,
  fetchProviders,
  fetchSkills,
  fetchTools,
  fetchWorkspaces,
} from "../../lib/api";
import { useStore } from "../../lib/store";
import type { ToolItem } from "../../lib/api";
import { ProvidersSection } from "./sections/ProvidersSection";
import { GeneralSection } from "./sections/GeneralSection";
import { SkillsSection } from "./sections/SkillsSection";
import { ToolsSection } from "./sections/ToolsSection";
import { SecuritySection } from "./sections/SecuritySection";
import { EventSourcesSection } from "./sections/EventSourcesSection";
import { RemindersSection } from "./sections/RemindersSection";
import { WorkspacesSection } from "./sections/WorkspacesSection";

interface Config {
  log_level?: string;
  default_provider?: string;
  default_model?: string;
  coara_home?: string;
  effective_coara_home?: string;
  skills_enabled?: boolean;
  records?: {
    enabled?: boolean;
    default_ttl_days?: number | null;
  };
  context_compression?: {
    threshold?: number;
    preserve_ratio?: number;
    min_compressible_fraction?: number;
  };
  matrix?: {
    homeserver?: string;
    user?: string;
    password?: string;
    notify_room_id?: string;
    guest_rooms?: string[];
    tunnel_enabled?: boolean;
    tunnel_mode?: string;
    tunnel_token?: string;
    tunnel_public_url?: string;
  };
  skills?: {
    default_include?: string[];
  };
  security?: {
    call_policy?: Record<string, string[]>;
    sandbox?: {
      enabled_for_untrusted?: boolean;
      blocked_commands?: string[];
      blocked_paths?: string[];
      blocked_hosts?: string[];
      blocked_env?: string[];
    };
  };
}

interface Providers {
  providers?: Record<string, any>;
}

interface Skill {
  name: string;
  description?: string;
  source?: string;
}

/** 配置页：纯状态展示 + 定时轮询刷新。编辑全部走配置助手对话。 */
export function ConfigPanel() {
  const [config, setConfig] = useState<Config | null>(null);
  const [providers, setProviders] = useState<Providers | null>(null);
  const [skillsPool, setSkillsPool] = useState<Skill[]>([]);
  const [tools, setTools] = useState<ToolItem[]>([]);
  const [workspaces, setWorkspaces] = useState<Array<{ name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { token } = theme.useToken();
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 默认落「模型」tab；URL ?focus=models 时强制切到「模型」并定位 provider 模块。
  const initialFocus = useMemo(
    () => new URLSearchParams(window.location.search).get("focus") === "models",
    [],
  );
  const [activeTab, setActiveTab] = useState("models");
  const didFocusScroll = useRef(false);
  useEffect(() => {
    if (initialFocus) setActiveTab("models");
  }, [initialFocus]);
  useEffect(() => {
    if (!loading && initialFocus && !didFocusScroll.current) {
      didFocusScroll.current = true;
      requestAnimationFrame(() => {
        const el = document.getElementById("providers-section");
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
          el.style.transition = "box-shadow 0.6s ease";
          el.style.boxShadow = "var(--coara-shadow-modal)";
          setTimeout(() => {
            el.style.boxShadow = "none";
          }, 1800);
        }
      });
    }
  }, [loading, initialFocus]);

  const load = useCallback(async () => {
    try {
      const [configData, providersData, skillsData, toolsData, workspacesData] =
        await Promise.all([
          fetchConfig(),
          fetchProviders(),
          fetchSkills(),
          fetchTools(),
          fetchWorkspaces(),
        ]);
      setConfig({ ...(configData.config || {}), effective_coara_home: configData.effective_coara_home });
      setProviders(providersData);
      setSkillsPool(skillsData.skills || []);
      setTools(toolsData.tools || []);
      setWorkspaces(workspacesData.workspaces || []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    // 配置只读化后变更只来自配置助手（会触发刷新）：30s 兜底轮询 + 页面隐藏时暂停
    const TICK_MS = 30_000;
    const tick = () => {
      if (document.visibilityState === "visible") void load();
    };
    timerRef.current = setInterval(tick, TICK_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [load]);

  // providers 写穿：providers_changed（web 保存 / 配置助手热重载 / 手改文件后
  // 触发热重载）即时重拉，显示与实际配置同步——30s 轮询只是兜底。
  const providersRevision = useStore((s) => s.providersRevision);
  const workspacesRevision = useStore((s) => s.workspacesRevision);
  useEffect(() => {
    if (providersRevision > 0 || workspacesRevision > 0) void load();
  }, [providersRevision, workspacesRevision, load]);

  if (loading) {
    return (
      <div style={{ textAlign: "center", padding: 40 }}>
        <Spin />
      </div>
    );
  }

  if (error) {
    return <div style={{ padding: 20, color: token.colorError }}>错误: {error}</div>;
  }

  const providersData = providers?.providers || {};
  const defaultInclude = config?.skills?.default_include || [];

  const tabItems = [
    {
      key: "general",
      label: "常规",
      children: (
        <>
          <GeneralSection
            config={{
              log_level: config?.log_level,
              default_provider: config?.default_provider,
              default_model: config?.default_model,
              coara_home: config?.coara_home,
              effective_coara_home: config?.effective_coara_home,
              skills_enabled: config?.skills_enabled,
              records: config?.records,
              context_compression: config?.context_compression,
            }}
          />
        </>
      ),
    },
    {
      key: "models",
      label: "模型",
      children: <ProvidersSection providers={providersData} />,
    },
    {
      key: "tools",
      label: "工具",
      children: <ToolsSection tools={tools} onSaved={load} />,
    },
    {
      key: "skills",
      label: "技能",
      children: (
        <SkillsSection skillsPool={skillsPool} defaultInclude={defaultInclude} />
      ),
    },
    {
      key: "services",
      label: "服务",
      children: (
        <>
          <SecuritySection config={config?.security || {}} />
        </>
      ),
    },
    {
      key: "automation",
      label: "自动化",
      children: (
        <>
          <EventSourcesSection workspaces={workspaces} />
          <RemindersSection />
        </>
      ),
    },
    {
      key: "workspaces",
      label: "工作空间",
      children: <WorkspacesSection />,
    },
  ];

  return (
    <div style={{ padding: 20, maxWidth: 1200, margin: "0 auto" }}>
      <Tabs items={tabItems} activeKey={activeTab} onChange={setActiveTab} />
    </div>
  );
}
