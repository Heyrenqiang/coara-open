import { useMemo, useState } from "react";
import { Button, Input, Typography, message } from "antd";
import type { ProviderConfig } from "../../../lib/api";
import { saveProviders } from "../../../lib/api";

const { Text } = Typography;

interface Props {
  providers: Record<string, ProviderConfig>;
  /** 保存成功后回调（父层刷新列表/探测可用模型）。 */
  onSaved?: () => void;
}

/**
 * 模型配置：一页安静的名录。
 * 无卡片、无标签、无彩色状态——名称 + 默认模型一行概览，
 * 待配置项前置并内嵌 key 输入（回车即存），已配置项只剩一个弱化入口。
 */
export function ProvidersSection({ providers, onSaved }: Props) {
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);

  // 待配置（无 key 且未停用）排最前；已停用垫底
  const entries = useMemo(() => {
    const rank = ([, p]: [string, ProviderConfig]) => {
      if (p.enabled === false) return 2;
      return p.api_key ? 1 : 0;
    };
    return [...Object.entries(providers)].sort((a, b) => rank(a) - rank(b));
  }, [providers]);

  const saveKey = async (name: string) => {
    const p = providers[name];
    if (!p) return;
    const draft = (keyDrafts[name] ?? "").trim();
    const hadKey = Boolean(p.api_key);
    setSaving(name);
    try {
      const next = {
        ...providers,
        [name]: {
          ...p,
          // 填了新 key 就用新值；没填：原已有 key 提交掩码占位符让后端还原，
          // 原本就没有 key 则留空（不误造 "***" 占位符）。
          api_key: draft || (hadKey ? "***" : ""),
        },
      };
      await saveProviders({ providers: next });
      setKeyDrafts((d) => ({ ...d, [name]: "" }));
      message.success(`${name} 的 API Key 已保存`);
      onSaved?.();
    } catch (err) {
      message.error(`保存失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(null);
    }
  };

  if (entries.length === 0) {
    return (
      <Text type="secondary" style={{ fontSize: 13 }}>
        尚未配置模型提供者，可在右下角配置助手中说「帮我加一个 DeepSeek」
      </Text>
    );
  }

  return (
    <div>
      {entries.map(([name, p], idx) => {
        const modelIds: string[] = Array.isArray((p.models as any)?.available)
          ? (p.models as any).available
              .map((m: any) => (typeof m === "string" ? m : m?.id))
              .filter(Boolean)
          : [];
        const defaultModel = p.default_model || (p.models as any)?.default || "";
        const hadKey = Boolean(p.api_key);
        const disabled = p.enabled === false;
        const needKey = !hadKey && !disabled;

        return (
          <div
            key={name}
            id={idx === 0 ? "providers-section" : undefined}
            style={{
              padding: needKey ? "14px 0" : "11px 0",
              borderBottom: "1px solid var(--coara-border-faint)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: 10,
                flexWrap: "wrap",
              }}
            >
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: disabled
                    ? "var(--coara-text-tertiary)"
                    : "var(--coara-text)",
                }}
              >
                {name}
              </span>
              <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>
                {defaultModel || modelIds[0] || ""}
                {modelIds.length > 1 && ` · 共 ${modelIds.length} 个模型`}
                {disabled && " · 已停用"}
              </span>
            </div>
            {needKey ? (
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  marginTop: 10,
                }}
              >
                <Input.Password
                  placeholder="粘贴 API Key，回车保存"
                  value={keyDrafts[name] ?? ""}
                  onChange={(e) =>
                    setKeyDrafts((d) => ({ ...d, [name]: e.target.value }))
                  }
                  onPressEnter={() => void saveKey(name)}
                  style={{ flex: 1, maxWidth: 420, fontSize: 13 }}
                />
                <Button
                  loading={saving === name}
                  onClick={() => void saveKey(name)}
                >
                  保存
                </Button>
              </div>
            ) : (
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  marginTop: 6,
                }}
              >
                <Input.Password
                  size="small"
                  placeholder="输入新 Key 覆盖"
                  value={keyDrafts[name] ?? ""}
                  onChange={(e) =>
                    setKeyDrafts((d) => ({ ...d, [name]: e.target.value }))
                  }
                  onPressEnter={() => void saveKey(name)}
                  style={{ maxWidth: 240 }}
                />
                <Button
                  size="small"
                  type="text"
                  loading={saving === name}
                  onClick={() => void saveKey(name)}
                >
                  保存
                </Button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
