import { useMemo, useState } from "react";
import { Button, Input } from "antd";
import { saveToolCredentials, type ToolCredentialField, type ToolItem } from "coara:shell";
import { ConfigEmpty, ConfigGroup, configClamp, configRow, configTitle } from "./configChrome";

const CATEGORY_LABELS: Record<string, string> = {
  filesystem: "文件",
  web: "网络",
  agent: "委派",
  media: "媒体",
  shell: "运行时",
  code: "代码",
  planning: "规划",
  system: "系统",
  communication: "通信",
  records: "记录",
  skill: "技能",
  ws: "工作空间",
  scheduling: "调度",
  general: "通用",
};

interface Props {
  tools: ToolItem[];
  onSaved?: () => void;
}

function toolMeta(tool: ToolItem): string {
  const bits: string[] = [];
  if (tool.status === "needs_config" && !tool.credentials?.length) bits.push("需配置");
  if (tool.deferred) bits.push("按需加载");
  if (tool.description) bits.push(tool.description);
  if (tool.config_hint && !tool.credentials?.length) bits.push(tool.config_hint);
  return bits.join(" · ");
}

function CredentialRow({
  toolName,
  field,
  onSaved,
}: {
  toolName: string;
  field: ToolCredentialField;
  onSaved?: () => void;
}) {
  const filled = field.secret ? Boolean(field.set) : Boolean(field.value);
  const [draft, setDraft] = useState(field.secret ? "" : (field.value ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    const value = draft.trim();
    if (!value) return;
    setSaving(true);
    setError("");
    try {
      await saveToolCredentials(toolName, { [field.id]: value });
      if (field.secret) setDraft("");
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  const control = field.secret ? (
    <Input.Password
      size={filled ? "small" : "middle"}
      placeholder={filled ? "输入新 Key 覆盖" : "粘贴 API Key，回车保存"}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onPressEnter={() => void save()}
      style={{ flex: 1, maxWidth: filled ? 240 : 420, fontSize: 13 }}
    />
  ) : (
    <Input
      placeholder={field.label}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onPressEnter={() => void save()}
      style={{ flex: 1, maxWidth: 420, fontSize: 13 }}
    />
  );

  return (
    <div style={{ marginTop: filled ? 6 : 10 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)", whiteSpace: "nowrap", flexShrink: 0 }}>{field.label}</span>
        {control}
        <Button
          size={filled ? "small" : "middle"}
          type={filled ? "text" : "default"}
          loading={saving}
          onClick={() => void save()}
        >
          保存
        </Button>
      </div>
      {error ? <div style={{ marginTop: 4, fontSize: 12, color: "var(--coara-danger)" }}>{error}</div> : null}
    </div>
  );
}

export function ToolsSection({ tools, onSaved }: Props) {
  const groups = useMemo(() => {
    const map = new Map<string, ToolItem[]>();
    for (const tool of tools) {
      const label = CATEGORY_LABELS[tool.category] || "其他";
      if (!map.has(label)) map.set(label, []);
      map.get(label)!.push(tool);
    }
    return [...map.entries()].map(([label, items]) => ({ label, items }));
  }, [tools]);

  if (tools.length === 0) {
    return <ConfigEmpty>未发现已注册的工具</ConfigEmpty>;
  }

  return (
    <div>
      {groups.map((group, index) => (
        <ConfigGroup key={group.label} title={group.label} first={index === 0}>
          {group.items.map((tool) => {
            const meta = toolMeta(tool);
            return (
              <div key={tool.name} style={configRow}>
                <div style={configTitle}>{tool.name}</div>
                {meta ? (
                  <div style={configClamp} title={meta}>
                    {meta}
                  </div>
                ) : null}
                {tool.credentials?.map((field) => (
                  <CredentialRow key={field.id} toolName={tool.name} field={field} onSaved={onSaved} />
                ))}
              </div>
            );
          })}
        </ConfigGroup>
      ))}
    </div>
  );
}
