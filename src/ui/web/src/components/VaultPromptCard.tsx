import { useState, useEffect, type KeyboardEvent } from "react";
import { Modal, Input, Button, Space, Typography, message } from "antd";
import { LockOutlined, CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { useStore } from "../lib/store";
import { getWS } from "../lib/ws";

const { Paragraph, Text } = Typography;

/**
 * Vault unlock — same Modal shell as approval (InteractionDialog).
 * Password goes via vault_reply; cancellation goes via vault_cancel.
 * Neither enters the agent / LLM context.
 */
export function VaultPromptCard() {
  const vaultPrompt = useStore((s) => s.vaultPrompt);
  const vaultResult = useStore((s) => s.vaultResult);
  const clearVaultPrompt = useStore((s) => s.clearVaultPrompt);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (vaultResult) {
      setSubmitting(false);
    }
  }, [vaultResult]);

  useEffect(() => {
    if (!vaultPrompt) {
      setSubmitting(false);
      setPassword("");
    }
  }, [vaultPrompt]);

  if (!vaultPrompt) return null;

  const handleSubmit = () => {
    const pw = password.trim();
    if (!pw || submitting) return;
    if (!getWS().connected) {
      message.error("连接已断开，请等待重连后再试");
      setSubmitting(false);
      return;
    }
    setSubmitting(true);
    getWS().send({ type: "vault_reply", password: pw });
    setPassword("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleDismiss = () => {
    // Notify the backend that the user dismissed the prompt so the blocked
    // vault tool call can return immediately instead of waiting for timeout.
    getWS().send({ type: "vault_cancel" });
    clearVaultPrompt();
    setPassword("");
  };

  return (
    <Modal
      open={!!vaultPrompt}
      title={
        <Space>
          <LockOutlined style={{ color: "var(--coara-warning)" }} />
          <span>{vaultPrompt.title || "保险柜解锁"}</span>
        </Space>
      }
      closable
      onCancel={handleDismiss}
      maskClosable={false}
      width={520}
      footer={[
        <Button key="cancel" onClick={handleDismiss}>
          取消
        </Button>,
        <Button
          key="unlock"
          type="primary"
          loading={submitting}
          disabled={!password.trim()}
          onClick={handleSubmit}
        >
          {vaultPrompt.initialized ? "解锁" : "创建并解锁"}
        </Button>,
      ]}
    >
      <Paragraph style={{ marginBottom: 16, fontSize: 14 }}>
        {vaultPrompt.question || vaultPrompt.hint}
      </Paragraph>
      <Input.Password
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入主密码"
        autoFocus
        disabled={submitting}
        size="large"
      />
      {vaultResult && (
        <div style={{ marginTop: 12, color: vaultResult.ok ? "var(--coara-success-strong)" : "var(--coara-danger-strong)" }}>
          <Space>
            {vaultResult.ok ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
            <Text style={{ color: "inherit" }}>{vaultResult.message}</Text>
          </Space>
        </div>
      )}
      <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
        密码不会进入 AI 对话。设置/改密请在 PC 使用 coara vault init / passwd。
      </Paragraph>
    </Modal>
  );
}
