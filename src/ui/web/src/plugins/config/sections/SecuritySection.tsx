import { ConfigGroup, configMeta, configRow, configTitle } from "./configChrome";

interface SecurityConfig {
  call_policy?: Record<string, string[]>;
  sandbox?: {
    enabled_for_untrusted?: boolean;
    blocked_commands?: string[];
    blocked_paths?: string[];
    blocked_hosts?: string[];
    blocked_env?: string[];
  };
}

interface Props {
  config: SecurityConfig;
}

function Row({ title, value }: { title: string; value: string }) {
  return (
    <div style={configRow}>
      <div style={configTitle}>{title}</div>
      <div style={{ ...configMeta, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{value}</div>
    </div>
  );
}

export function SecuritySection({ config }: Props) {
  const sandbox = config.sandbox || {};
  const policy = config.call_policy || {};
  const policyText = Object.entries(policy)
    .map(([name, modes]) => `${name}：${(modes || []).join("、")}`)
    .join("\n");

  return (
    <ConfigGroup title="安全">
      <Row title="沙箱" value={sandbox.enabled_for_untrusted ? "对不可信调用方启用" : "关闭"} />
      {sandbox.blocked_commands && sandbox.blocked_commands.length > 0 ? (
        <Row title="阻止命令" value={sandbox.blocked_commands.join("、")} />
      ) : null}
      {sandbox.blocked_paths && sandbox.blocked_paths.length > 0 ? (
        <Row title="阻止路径" value={sandbox.blocked_paths.join("、")} />
      ) : null}
      {sandbox.blocked_hosts && sandbox.blocked_hosts.length > 0 ? (
        <Row title="阻止主机" value={sandbox.blocked_hosts.join("、")} />
      ) : null}
      {policyText ? <Row title="调用策略" value={policyText} /> : null}
    </ConfigGroup>
  );
}
