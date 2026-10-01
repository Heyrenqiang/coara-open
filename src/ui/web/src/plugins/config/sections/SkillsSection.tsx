import { useEffect, useState } from "react";
import { Button, Input, Spin, Switch, message } from "antd";
import { fetchSkillContent, saveSkillContent, setSkillDeferred } from "coara:shell";
import { ConfigEmpty, ConfigMore, configClamp, configRow, configTitle } from "./configChrome";

const SOURCE_LABELS: Record<string, string> = {
  global: "全局",
  workspace: "工作空间",
  extra: "扩展",
};

interface Props {
  skillsPool: Array<{ name: string; description?: string; source?: string; deferred?: boolean }>;
  defaultInclude: string[] | undefined;
  onChanged?: () => void;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "ready"; frontmatterRaw: string; originalBody: string; readonly: boolean };

function SkillPanelBody({ name }: { name: string }) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);

  const load = () => {
    setState({ status: "loading" });
    fetchSkillContent(name)
      .then((c) => {
        setBody(c.body);
        setState({ status: "ready", frontmatterRaw: c.frontmatter_raw, originalBody: c.body, readonly: c.readonly });
      })
      .catch((e) => setState({ status: "error", error: e instanceof Error ? e.message : "加载失败" }));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [name]);

  if (state.status === "loading") {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: "12px 0" }}>
        <Spin size="small" />
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>{state.error}</span>
        <Button size="small" type="text" onClick={load}>
          重试
        </Button>
      </div>
    );
  }

  const dirty = body !== state.originalBody;

  const onSave = async () => {
    setSaving(true);
    try {
      await saveSkillContent(name, body);
      setState({ status: "ready", frontmatterRaw: state.frontmatterRaw, originalBody: body, readonly: state.readonly });
      message.success("已保存");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {state.frontmatterRaw ? (
        <pre
          style={{
            margin: 0,
            fontSize: 12,
            fontFamily: "'Fragment Mono', 'Cascadia Mono', Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-all",
            color: "var(--coara-text-tertiary)",
          }}
        >
          {state.frontmatterRaw}
        </pre>
      ) : null}
      <Input.TextArea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        autoSize={{ minRows: 8, maxRows: 30 }}
        readOnly={state.readonly}
        style={{ fontFamily: "'Fragment Mono', 'Cascadia Mono', Consolas, monospace", fontSize: 13 }}
      />
      {!state.readonly ? (
        <div>
          <Button size="small" type="text" disabled={!dirty} loading={saving} onClick={onSave}>
            保存
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function DeferredSwitch({ skill, onChanged }: { skill: { name: string; deferred?: boolean }; onChanged?: () => void }) {
  const [checked, setChecked] = useState(Boolean(skill.deferred));
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setChecked(Boolean(skill.deferred));
  }, [skill.deferred]);

  const onToggle = async (next: boolean) => {
    setSaving(true);
    try {
      await setSkillDeferred(skill.name, next);
      setChecked(next);
      message.success(next ? "已挂起，新会话生效" : "已常驻，新会话生效");
      onChanged?.();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
      <Switch size="small" checked={checked} loading={saving} onChange={(v) => void onToggle(v)} />
      <span style={{ fontSize: 12, color: "var(--coara-text-tertiary)" }}>挂起（不常驻提示词，对话中按需激活）</span>
    </div>
  );
}

export function SkillsSection({ skillsPool, defaultInclude, onChanged }: Props) {
  const safeDefaultInclude = defaultInclude || [];
  const [openName, setOpenName] = useState<string | null>(null);

  if (skillsPool.length === 0) {
    return <ConfigEmpty>技能池中未发现技能</ConfigEmpty>;
  }

  return (
    <div>
      {skillsPool.map((skill) => {
        const bits = [SOURCE_LABELS[skill.source || "extra"] || "扩展"];
        if (skill.deferred) bits.push("挂起");
        if (safeDefaultInclude.includes(skill.name)) bits.push("默认加载");
        if (skill.description) bits.push(skill.description);
        const open = openName === skill.name;
        return (
          <div key={skill.name} style={configRow}>
            <div style={configTitle}>{skill.name}</div>
            <div style={configClamp} title={bits.join(" · ")}>
              {bits.join(" · ")}
            </div>
            <DeferredSwitch skill={skill} onChanged={onChanged} />
            <ConfigMore open={open} onToggle={() => setOpenName(open ? null : skill.name)}>
              <SkillPanelBody name={skill.name} />
            </ConfigMore>
          </div>
        );
      })}
    </div>
  );
}
