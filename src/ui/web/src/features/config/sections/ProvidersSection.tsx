import { useMemo, useRef, useState, type CSSProperties } from "react";
import { Button, Input, InputNumber, Select, Tabs, Typography, message } from "antd";
import { DownOutlined, PlusOutlined, RightOutlined } from "@ant-design/icons";
import type { ProviderConfig } from "../../../lib/api";
import { fetchProviders, saveProviders } from "../../../lib/api";

const { Text } = Typography;

interface Props {
  providers: Record<string, ProviderConfig>;
  /** 保存成功后回调（父层刷新列表/探测可用模型）。 */
  onSaved?: () => void;
}

/** 单个模型的出厂/当前设置（一个标签页一份）。 */
interface ModelDraft {
  id: string;
  name: string;
  description: string;
  function_calling: boolean;
  vision: boolean;
  max_tokens: number | null;
  temperature: number | null;
  /** on/off/low/medium/high；空 = 跟随厂商默认 */
  thinking: string;
}

/** 展开区草稿：共享字段 + 按模型分页的标签页。 */
interface MoreDraft {
  base_url: string;
  driver: string;
  models: ModelDraft[];
  activeModel: string;
}

function modelDraftOf(entry: any): ModelDraft {
  if (typeof entry === "string") {
    return {
      id: entry,
      name: "",
      description: "",
      function_calling: true,
      vision: false,
      max_tokens: null,
      temperature: null,
      thinking: "",
    };
  }
  const thinkingRaw = typeof entry?.thinking === "string" ? entry.thinking.trim().toLowerCase() : "";
  const thinkingOk = ["on", "off", "low", "medium", "high"].includes(thinkingRaw);
  return {
    id: String(entry?.id ?? ""),
    name: String(entry?.name ?? ""),
    description: String(entry?.description ?? ""),
    function_calling: entry?.function_calling !== false,
    vision: Boolean(entry?.vision),
    max_tokens: typeof entry?.max_tokens === "number" ? entry.max_tokens : null,
    temperature: typeof entry?.temperature === "number" ? entry.temperature : null,
    thinking: thinkingOk ? thinkingRaw : "",
  };
}

function draftOf(p: ProviderConfig): MoreDraft {
  const raw: any[] = Array.isArray((p.models as any)?.available) ? (p.models as any).available : [];
  const models = raw.map(modelDraftOf).filter((m) => m.id);
  return {
    base_url: p.base_url || "",
    driver: p.driver || "openai",
    models,
    activeModel: models[0]?.id ?? "",
  };
}

const FIELD_LABEL: CSSProperties = {
  fontSize: 12,
  color: "var(--coara-text-tertiary)",
  flexShrink: 0,
};

/** 「是否已配 key」以后端 envelope 的 has_key 为准（key 存放在 home .env，
 *  envelope 里不再带内联值）；存量内联掩码 "***" 兜底。 */
function hasKey(p: ProviderConfig): boolean {
  return Boolean(p.has_key);
}

/**
 * 模型配置：一页安静的名录。
 * 首行只有 provider 名；key 输入保持原样（回车即存）；
 * 「更多」折叠区：共享字段（地址/驱动）+ 按模型分页的标签页——出厂预置
 * 最新模型各一页（说明/能力/最大输出等出厂已设好），可加页，一标签页一个模型 id。
 */
export function ProvidersSection({ providers, onSaved }: Props) {
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [moreDrafts, setMoreDrafts] = useState<Record<string, MoreDraft>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState<string | null>(null);
  // 新 provider 暂存：只收名称 + API Key，保存进表后按现有卡渲染（更多全空可填）
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newKey, setNewKey] = useState("");
  // 即改即存：防抖 600ms 合并连续键入；保存期间的新改动挂起，完成后补一次
  const persistTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const persistDirty = useRef<Record<string, boolean>>({});
  const persistBusy = useRef<Record<string, boolean>>({});
  const draftsRef = useRef(moreDrafts);
  draftsRef.current = moreDrafts;

  // 按 providers.yaml 声明顺序稳定排列：不随 key 有无/编辑重排，
  // 否则填完 key 或改完配置后该 provider 会滑到列表底部。
  const entries = useMemo(() => [...Object.entries(providers)], [providers]);

  const moreOf = (name: string, p: ProviderConfig): MoreDraft => moreDrafts[name] ?? draftOf(p);

  const patchMore = (
    name: string,
    p: ProviderConfig,
    partial: Partial<MoreDraft>,
    opts?: { persist?: boolean },
  ) => {
    setMoreDrafts((d) => ({ ...d, [name]: { ...moreOf(name, p), ...partial } }));
    // 仅切模型标签页不写盘（activeModel 是 UI 态）
    if (opts?.persist !== false) schedulePersist(name);
  };

  const patchModel = (name: string, p: ProviderConfig, modelId: string, partial: Partial<ModelDraft>) => {
    const m = moreOf(name, p);
    patchMore(name, p, {
      models: m.models.map((md) => (md.id === modelId ? { ...md, ...partial } : md)),
    });
  };

  /** 即改即存：更多区任何变动 600ms 防抖后写回；保存中的新改动挂起补存。 */
  function schedulePersist(name: string) {
    const timers = persistTimers.current;
    if (timers[name]) clearTimeout(timers[name]);
    timers[name] = setTimeout(() => void persistMore(name), 600);
  }

  /** 保存已落盘但热重载失败（后端 hot_reload=false：provider 实例还是旧的）——
   *  必须明告重启内核生效，否则配置页看着已生效、实际模型链没换。 */
  function notifyHotReload(result: { hot_reload?: boolean }): void {
    if (result.hot_reload === false) {
      message.warning("已保存，但热重载失败，请重启内核生效");
    }
  }

  /** 保存前向服务端拉最新全表再合并——saveProviders 是全量替换，用 props 快照会覆盖并发改动。 */
  async function fetchProvidersLatest(): Promise<Record<string, ProviderConfig>> {
    const data = await fetchProviders();
    const map = (data as { providers?: Record<string, ProviderConfig> })?.providers;
    return map && typeof map === "object" ? map : {};
  }

  async function persistMore(name: string) {
    if (persistBusy.current[name]) {
      persistDirty.current[name] = true;
      return;
    }
    persistBusy.current[name] = true;
    try {
      const res = await fetchProvidersLatest();
      const p = res[name];
      if (!p) return;
      const m = draftsRef.current[name] ?? draftOf(p);
      // 模型清单按标签页写回：已有条目的定价等字段保留（按 id 从原配置带出），
      // 页面编辑的说明/能力/最大输出覆盖之；新增页纯 id 起步。
      const prevAvailable: any[] = Array.isArray((p.models as any)?.available)
        ? (p.models as any).available
        : [];
      const prevById = new Map<string, any>();
      for (const x of prevAvailable) {
        const id = typeof x === "string" ? x : x?.id;
        if (id) prevById.set(String(id), x);
      }
      const seen = new Set<string>();
      const available = m.models
        .filter((md) => md.id.trim())
        .filter((md) => {
          const id = md.id.trim();
          if (seen.has(id)) return false;
          seen.add(id);
          return true;
        })
        .map((md) => {
          const id = md.id.trim();
          const prev = prevById.get(id);
          const base: any = typeof prev === "object" && prev !== null ? { ...prev } : { id };
          base.id = id;
          if (md.name.trim()) base.name = md.name.trim();
          if (md.description.trim()) base.description = md.description.trim();
          base.function_calling = md.function_calling;
          if (md.vision) base.vision = true;
          else delete base.vision;
          if (md.max_tokens != null) base.max_tokens = md.max_tokens;
          else if (prev == null) delete base.max_tokens;
          if (md.temperature != null) base.temperature = md.temperature;
          else if (prev == null) delete base.temperature;
          if (md.thinking) base.thinking = md.thinking;
          else delete base.thinking;
          return base;
        });
      const entry: ProviderConfig = {
        ...p,
        // key 不碰：已有 key 提交掩码占位符让后端跳过，原本没有则留空
        api_key: hasKey(p) ? "***" : "",
        base_url: m.base_url.trim() || p.base_url,
        driver: m.driver,
        // 默认模型 = 清单第一项（选择器仍全量列出 available；default 只是
        // 「该 provider 未显式选模型时用哪个」，随分页顺序自然成立）。
        default_model: available[0]?.id ?? p.default_model,
        models: {
          ...(p.models as any),
          default: available[0]?.id ?? (p.models as any)?.default,
          available,
        },
      };
      const saved = await saveProviders({ providers: { ...res, [name]: entry } });
      onSaved?.();
      notifyHotReload(saved);
    } catch (err) {
      message.error(`保存失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      persistBusy.current[name] = false;
      if (persistDirty.current[name]) {
        persistDirty.current[name] = false;
        void persistMore(name);
      }
    }
  }

  const saveKey = async (name: string) => {
    const draft = (keyDrafts[name] ?? "").trim();
    setSaving(name);
    try {
      const res = await fetchProvidersLatest();
      const p = res[name];
      if (!p) {
        message.error(`${name} 已不存在，请刷新后再试`);
        return;
      }
      const hadKey = hasKey(p);
      const entry: ProviderConfig = {
        ...p,
        // 填了新 key 就用新值；没填：已有 key 提交掩码占位符让后端跳过，
        // 原本就没有 key 则留空（不误造 "***" 占位符）。
        api_key: draft || (hadKey ? "***" : ""),
      };
      const saved = await saveProviders({ providers: { ...res, [name]: entry } });
      setKeyDrafts((d) => ({ ...d, [name]: "" }));
      message.success(`${name} 的 API Key 已保存`);
      onSaved?.();
      notifyHotReload(saved);
    } catch (err) {
      message.error(`保存失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(null);
    }
  };

  /** 添加新 provider：名称 + API Key 起步，保存进表后「更多」全空可填。 */
  const addProvider = async () => {
    const name = newName.trim().toLowerCase().replace(/\s+/g, "-");
    if (!name) {
      message.warning("请填写 provider 名称");
      return;
    }
    setSaving("__new__");
    try {
      const res = await fetchProvidersLatest();
      if (res[name]) {
        message.warning(`${name} 已存在`);
        return;
      }
      const entry: ProviderConfig = {
        base_url: "",
        driver: "openai",
        api_key: newKey.trim(),
        models: { available: [] },
      };
      const saved = await saveProviders({ providers: { ...res, [name]: entry } });
      setAdding(false);
      setNewName("");
      setNewKey("");
      message.success(`已添加 ${name}，在「更多」里补地址与模型`);
      onSaved?.();
      notifyHotReload(saved);
    } catch (err) {
      message.error(`保存失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(null);
    }
  };

  const addProviderRow = adding ? (
    <div
      style={{
        padding: "14px 0",
        borderBottom: "1px solid var(--coara-border-faint)",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Input
          size="small"
          placeholder="provider 名称（如 openai）"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          style={{ maxWidth: 220 }}
        />
        <Input.Password
          size="small"
          placeholder="粘贴 API Key（可先留空）"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          onPressEnter={() => void addProvider()}
          style={{ flex: 1, maxWidth: 320 }}
        />
        <Button size="small" loading={saving === "__new__"} onClick={() => void addProvider()}>
          保存
        </Button>
        <Button
          size="small"
          type="text"
          onClick={() => {
            setAdding(false);
            setNewName("");
            setNewKey("");
          }}
        >
          取消
        </Button>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        保存后在「更多」里补 API 地址、驱动与模型标签页。
      </Text>
    </div>
  ) : (
    <div
      role="button"
      onClick={() => setAdding(true)}
      style={{
        padding: "11px 0",
        fontSize: 13,
        color: "var(--coara-text-tertiary)",
        cursor: "pointer",
        userSelect: "none",
      }}
    >
      + 添加 provider
    </div>
  );

  if (entries.length === 0) {
    return (
      <div id="providers-section">
        <Text type="secondary" style={{ fontSize: 13, display: "block", marginBottom: 8 }}>
          尚未配置模型提供者，可在下方添加，或在右下角配置助手中说「帮我加一个 DeepSeek」
        </Text>
        {addProviderRow}
      </div>
    );
  }

  return (
    <div>
      {entries.map(([name, p], idx) => {
        const hadKey = hasKey(p);
        const needKey = !hadKey;
        const open = Boolean(expanded[name]);
        const more = moreOf(name, p);

        return (
          <div
            key={name}
            id={idx === 0 ? "providers-section" : undefined}
            style={{
              padding: needKey ? "14px 0" : "11px 0",
              borderBottom: "1px solid var(--coara-border-faint)",
            }}
          >
            <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
              <span style={{ fontSize: 14, fontWeight: 600, color: "var(--coara-text)" }}>
                {name}
              </span>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: needKey ? 10 : 6 }}>
              <Input.Password
                size="small"
                placeholder={needKey ? "粘贴 API Key，回车保存" : "输入新 Key 覆盖"}
                value={keyDrafts[name] ?? ""}
                onChange={(e) => setKeyDrafts((d) => ({ ...d, [name]: e.target.value }))}
                onPressEnter={() => void saveKey(name)}
                style={{ flex: 1, maxWidth: 420 }}
              />
              <Button size="small" loading={saving === name} onClick={() => void saveKey(name)}>
                保存
              </Button>
            </div>
            <div
              role="button"
              onClick={() => setExpanded((e) => ({ ...e, [name]: !open }))}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                marginTop: 8,
                fontSize: 12,
                color: "var(--coara-text-tertiary)",
                cursor: "pointer",
                userSelect: "none",
              }}
            >
              {open ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}
              更多
            </div>
            {open && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 10,
                  marginTop: 10,
                  padding: "12px 14px",
                  border: "1px solid var(--coara-border-faint)",
                  borderRadius: 8,
                  maxWidth: 640,
                }}
              >
                <Tabs
                  size="small"
                  activeKey={more.activeModel}
                  onChange={(k) => patchMore(name, p, { activeModel: k }, { persist: false })}
                  items={more.models.map((md) => ({
                    key: md.id,
                    label: md.id,
                  }))}
                  tabBarExtraContent={
                    <Button
                      size="small"
                      type="text"
                      icon={<PlusOutlined />}
                      onClick={() => {
                        const nid = `model-${more.models.length + 1}`;
                        patchMore(name, p, {
                          models: [
                            ...more.models,
                            {
                              id: nid,
                              name: "",
                              description: "",
                              function_calling: true,
                              vision: false,
                              max_tokens: null,
                              temperature: null,
                              thinking: "",
                            },
                          ],
                          activeModel: nid,
                        });
                      }}
                    >
                      添加
                    </Button>
                  }
                />
                {more.models
                  .filter((md) => md.id === more.activeModel)
                  .map((md) => (
                    <div key={md.id} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={FIELD_LABEL}>模型 ID</span>
                        <Input
                          size="small"
                          value={md.id}
                          onChange={(e) => {
                            const nid = e.target.value;
                            patchMore(name, p, {
                              models: more.models.map((x) => (x === md ? { ...x, id: nid } : x)),
                              activeModel: nid,
                            });
                          }}
                          style={{ flex: 1, color: "var(--coara-text-secondary)" }}
                        />
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={FIELD_LABEL}>API 地址</span>
                        <Input
                          size="small"
                          value={more.base_url}
                          onChange={(e) => patchMore(name, p, { base_url: e.target.value })}
                          style={{ flex: 1, color: "var(--coara-text-secondary)" }}
                        />
                        <span style={FIELD_LABEL}>驱动</span>
                        <Select
                          size="small"
                          value={more.driver}
                          onChange={(v) => patchMore(name, p, { driver: v })}
                          options={[
                            { value: "openai", label: "openai" },
                            { value: "responses", label: "responses" },
                          ]}
                          style={{ width: 130 }}
                        />
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        <span style={FIELD_LABEL}>最大输出</span>
                        <InputNumber
                          size="small"
                          value={md.max_tokens}
                          onChange={(v) => patchModel(name, p, md.id, { max_tokens: typeof v === "number" ? v : null })}
                          min={1}
                          style={{ width: 120 }}
                        />
                        <span style={FIELD_LABEL}>温度</span>
                        <InputNumber
                          size="small"
                          value={md.temperature}
                          onChange={(v) => patchModel(name, p, md.id, { temperature: typeof v === "number" ? v : null })}
                          min={0}
                          max={2}
                          step={0.1}
                          style={{ width: 90 }}
                        />
                        <span style={FIELD_LABEL}>视觉</span>
                        <Select
                          size="small"
                          value={md.vision ? "yes" : "no"}
                          onChange={(v) => patchModel(name, p, md.id, { vision: v === "yes" })}
                          options={[
                            { value: "yes", label: "支持" },
                            { value: "no", label: "不支持" },
                          ]}
                          style={{ width: 90 }}
                        />
                        {more.models.length > 1 && (
                          <Button
                            size="small"
                            type="text"
                            danger
                            style={{ marginLeft: "auto" }}
                            onClick={() => {
                              const rest = more.models.filter((x) => x.id !== md.id);
                              patchMore(name, p, {
                                models: rest,
                                activeModel: rest[0]?.id ?? "",
                              });
                            }}
                          >
                            删除此模型
                          </Button>
                        )}
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        <span style={FIELD_LABEL}>思考模式</span>
                        <Select
                          size="small"
                          value={md.thinking || "default"}
                          onChange={(v) =>
                            patchModel(name, p, md.id, { thinking: v === "default" ? "" : String(v) })
                          }
                          options={[
                            { value: "default", label: "默认（厂商/内置）" },
                            { value: "on", label: "开（不指定档位·跟厂商）" },
                            { value: "low", label: "低" },
                            { value: "medium", label: "中" },
                            { value: "high", label: "高" },
                            { value: "off", label: "关" },
                          ]}
                          style={{ width: 190 }}
                        />
                        <span style={{ color: "var(--coara-text-faint)", fontSize: 12 }}>
                          高档推理更深但首字更慢；会话内可用 /thinking 临时覆盖
                        </span>
                      </div>
                    </div>
                  ))}
              </div>
            )}
          </div>
        );
      })}
      {addProviderRow}
    </div>
  );
}
