import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Input, Modal, Select, Switch, message } from "antd";
import {
  fetchAutostart,
  fetchDefaultModel,
  fetchModelChoices,
  fetchUserRules,
  openUserRulesFile,
  saveConfig,
  saveDefaultModel,
  setAutostart,
  useStore,
  type ModelChoice,
} from "coara:shell";
import { fileViewRoute } from "coara:shell";
import { configMeta, configRow, configTitle } from "./configChrome";

/** 与 ContextCompressionSettings 默认值一致（未写配置时展示具体数值，不写「默认」）。 */
const COMPRESSION_DEFAULTS = {
  threshold: 0.6,
  preserve_ratio: 0.3,
  min_compressible_fraction: 0.05,
} as const;

interface GeneralConfig {
  log_level?: string;
  default_provider?: string;
  default_model?: string;
  coara_home?: string;
  effective_coara_home?: string;
  skills_enabled?: boolean;
  records?: {
    enabled?: boolean;
  };
  context_compression?: {
    threshold?: number;
    preserve_ratio?: number;
    min_compressible_fraction?: number;
  };
}

interface Props {
  config: GeneralConfig;
}

function SettingRow({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={configRow}>
      <div style={configTitle}>{title}</div>
      <div style={{ ...configMeta, display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>{children}</div>
    </div>
  );
}

/** 开机自启：OS 层注册项是唯一事实源，开关直写注册项（不经 config.yaml）。 */
function AutostartRow() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetchAutostart()
      .then((s) => setEnabled(s.enabled))
      .catch(() => setEnabled(null));
  }, []);

  const onToggle = async (next: boolean) => {
    setBusy(true);
    try {
      const s = await setAutostart(next);
      setEnabled(s.enabled);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "设置开机自启失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow title="开机自启">
      {enabled === null ? "未知" : <Switch size="small" checked={enabled} loading={busy} onChange={onToggle} />}
    </SettingRow>
  );
}

/** 用户规则：文件入口形态。查看走应用内文件页，编辑用系统默认编辑器打开。 */
function UserRulesRow() {
  const navigate = useNavigate();
  const [path, setPath] = useState<string | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ok" | "error">("loading");
  const [opening, setOpening] = useState(false);

  const reload = () => {
    setLoadState("loading");
    fetchUserRules()
      .then((s) => {
        setPath(s.path || null);
        setLoadState("ok");
      })
      .catch(() => setLoadState("error"));
  };

  useEffect(() => {
    reload();
  }, []);

  const onOpen = async () => {
    setOpening(true);
    try {
      await openUserRulesFile();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "打开用户规则文件失败");
    } finally {
      setOpening(false);
    }
  };

  return (
    <SettingRow title="用户规则">
      {loadState === "error" ? (
        <>
          <span>加载失败</span>
          <Button size="small" type="text" onClick={reload}>
            重试
          </Button>
        </>
      ) : (
        <>
          {path ? (
            <a
              href={fileViewRoute(path)}
              onClick={(e) => {
                if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;
                e.preventDefault();
                navigate(fileViewRoute(path));
              }}
              style={{
                color: "var(--coara-link)",
                textDecoration: "none",
                fontSize: 12,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                minWidth: 0,
              }}
              title={path}
            >
              {path}
            </a>
          ) : (
            <span>{loadState === "loading" ? "加载中" : "未知路径"}</span>
          )}
          <Button size="small" type="text" loading={opening} disabled={loadState !== "ok"} onClick={onOpen}>
            打开编辑
          </Button>
        </>
      )}
    </SettingRow>
  );
}

/** 全局默认模型（llm_preferences 承载；等价 /model --global）。 */
function DefaultModelRow() {
  const [choices, setChoices] = useState<ModelChoice[]>([]);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const providersRevision = useStore((s) => s.providersRevision);

  useEffect(() => {
    fetchModelChoices()
      .then((d) => setChoices(d.catalog ?? []))
      .catch(() => {});
    fetchDefaultModel()
      .then((d) => {
        if (d.default_provider && d.default_model) {
          setValue(`${d.default_provider}/${d.default_model}`);
        } else {
          setValue("");
        }
      })
      .catch(() => {});
  }, [providersRevision]);

  const onChange = async (key: string) => {
    if (!key || key === value || busy) return;
    setBusy(true);
    try {
      await saveDefaultModel(key);
      setValue(key);
      message.success("默认模型已更新");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow title="默认模型">
      <Select
        size="small"
        loading={busy}
        value={value || undefined}
        placeholder="选择默认模型"
        style={{ minWidth: 220 }}
        options={choices.map((c) => ({ value: c.key, label: c.label || c.key.replace("/", "·") }))}
        onChange={(v) => void onChange(String(v))}
      />
    </SettingRow>
  );
}

/** coara Home：显示实际生效路径，可弹出修改（写 config.yaml，重启内核生效）。 */
function CoaraHomeRow({ config }: Props) {
  const effective = config.effective_coara_home || config.coara_home || "";
  const [current, setCurrent] = useState(effective);
  const [modalOpen, setModalOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCurrent(effective);
  }, [effective]);

  const openModal = () => {
    setDraft(config.coara_home || current);
    setModalOpen(true);
  };

  const onSave = async () => {
    const next = draft.trim();
    if (!next) {
      message.error("请输入绝对路径");
      return;
    }
    setBusy(true);
    try {
      await saveConfig({ coara_home: next });
      setCurrent(next);
      setModalOpen(false);
      message.success("已保存，重启内核后生效");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow title="coara Home">
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }} title={current}>
        {current || "未知"}
      </span>
      <Button size="small" type="text" onClick={openModal}>
        修改
      </Button>
      <Modal
        title="修改 coara Home"
        open={modalOpen}
        onOk={onSave}
        onCancel={() => setModalOpen(false)}
        confirmLoading={busy}
        okText="保存"
        cancelText="取消"
        width={480}
      >
        <div style={{ marginBottom: 8, color: "var(--coara-text-faint)", fontSize: 12 }}>
          改后重启内核生效，原有数据不会自动迁移。
        </div>
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="绝对路径，例如 D:\coara"
          onPressEnter={() => void onSave()}
        />
      </Modal>
    </SettingRow>
  );
}

export function GeneralSection({ config }: Props) {
  const compression = config.context_compression || {};
  const threshold = compression.threshold ?? COMPRESSION_DEFAULTS.threshold;
  const preserve = compression.preserve_ratio ?? COMPRESSION_DEFAULTS.preserve_ratio;
  const minFraction = compression.min_compressible_fraction ?? COMPRESSION_DEFAULTS.min_compressible_fraction;

  return (
    <div>
      <DefaultModelRow />
      <CoaraHomeRow config={config} />
      <SettingRow title="技能">{config.skills_enabled !== false ? "开启" : "关闭"}</SettingRow>
      <SettingRow title="记录">{config.records?.enabled !== false ? "开启" : "关闭"}</SettingRow>
      <AutostartRow />
      <SettingRow title="上下文压缩">
        {`触发阈值 ${threshold} · 保留比例 ${preserve} · 最小可压缩 ${minFraction}`}
      </SettingRow>
      <UserRulesRow />
    </div>
  );
}
