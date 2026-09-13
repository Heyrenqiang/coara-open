import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spin, Tabs, theme } from "antd";
import {
  fetchConfig,
  fetchProviders,
  fetchSkills,
  fetchWorkspaces,
} from "../../lib/api";
import { ProvidersSection } from "./sections/ProvidersSection";
import { GeneralSection } from "./sections/GeneralSection";
import { SkillsSection } from "./sections/SkillsSection";
import { CompressionSection } from "./sections/CompressionSection";
import { EventsSection } from "./sections/EventsSection";
import { MatrixSection } from "./sections/MatrixSection";
import { SecuritySection } from "./sections/SecuritySection";
import { VaultSection } from "./sections/VaultSection";
import { EventSourcesSection } from "./sections/EventSourcesSection";
import { RemindersSection } from "./sections/RemindersSection";
import { WorkspacesSection } from "./sections/WorkspacesSection";

interface Config {
  log_level?: string;
  default_provider?: string;
  default_model?: string;
  coara_home?: string;
  vault_enabled?: boolean;
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
  events?: {
    webhook_host?: string;
    webhook_port?: number;
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
  const [workspaces, setWorkspaces] = useState<Array<{ name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { token } = theme.useToken();
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 首启引导：URL 带 ?focus=models 时直接落在「模型」tab 并定位到 provider 模块
  // （无可用模型时从聊天页跳转过来，用户第一眼就能填 API Key）。
  const initialFocus = useMemo(
    () => new URLSearchParams(window.location.search).get("focus") === "models",
    [],
  );
  const [activeTab, setActiveTab] = useState(initialFocus ? "models" : "general");
  const didFocusScroll = useRef(false);
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
      const [configData, providersData, skillsData, workspacesData] =
        await Promise.all([
          fetchConfig(),
          fetchProviders(),
          fetchSkills(),
          fetchWorkspaces(),
        ]);
      setConfig(configData.config || configData);
      setProviders(providersData);
      setSkillsPool(skillsData.skills || []);
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
              skills_enabled: config?.skills_enabled,
              records: config?.records,
            }}
          />
          <CompressionSection config={config?.context_compression || {}} />
          <EventsSection config={config?.events || {}} />
          <MatrixSection config={config?.matrix || {}} />
        </>
      ),
    },
    {
      key: "models",
      label: "模型",
      children: <ProvidersSection providers={providersData} />,
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
          <VaultSection />
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
