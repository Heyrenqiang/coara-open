import { useEffect, useState } from "react";
import { Modal, Button, Space, Typography } from "antd";
import { ExclamationCircleOutlined } from "@ant-design/icons";
import { useStore } from "../../lib/store";
import { getWS } from "../../lib/ws";

const { Paragraph, Text } = Typography;

/** 审批出现时的提示音：双音蜂鸣，WebAudio 合成，无音频资源依赖。
 *  浏览器自动播放策略下可能在首次用户交互前被静音，静默失败即可。 */
function playApprovalChime() {
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    void ctx.resume();
    const t0 = ctx.currentTime;
    [880, 1174.66].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      const start = t0 + i * 0.14;
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(0.22, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, start + 0.4);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + 0.45);
    });
    window.setTimeout(() => void ctx.close(), 1200);
  } catch {
    // 无音频环境时忽略
  }
}

export function InteractionDialog() {
  const pending = useStore((s) => s.pendingInteraction);
  const clearInteraction = useStore((s) => s.clearInteraction);
  const approvalId = pending?.approval_id;
  const deadline =
    pending?.created_at_ms && pending?.timeout_s
      ? pending.created_at_ms + pending.timeout_s * 1000
      : null;
  const [expired, setExpired] = useState(false);

  // 审批弹出瞬间：提示音 + 标签页标题闪烁（后台标签页也能察觉）
  useEffect(() => {
    if (!approvalId) return;
    playApprovalChime();
    const original = document.title;
    const timer = window.setInterval(() => {
      document.title = document.title.startsWith("【需要确认】")
        ? original
        : `【需要确认】${original}`;
    }, 900);
    return () => {
      window.clearInterval(timer);
      document.title = original;
    };
  }, [approvalId]);

  // 本地倒计时自失效：到期禁用按钮并提示超时（服务端终态帧到达也会关）
  useEffect(() => {
    setExpired(false);
    if (!approvalId || !deadline) return;
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      setExpired(true);
      return;
    }
    const timer = window.setTimeout(() => setExpired(true), remaining);
    return () => window.clearTimeout(timer);
  }, [approvalId, deadline]);

  if (!pending) return null;

  const handleApproval = (approved: boolean) => {
    if (expired) return;
    getWS().send({ type: "approval_reply", approval_id: pending.approval_id, approved });
    clearInteraction();
  };

  return (
    <Modal
      open={!!pending}
      title={
        <Space>
          <ExclamationCircleOutlined style={{ color: "var(--coara-warning)" }} />
          <span>需要确认</span>
        </Space>
      }
      closable={false}
      maskClosable={false}
      width={520}
      footer={[
        <Button key="deny" danger disabled={expired} onClick={() => handleApproval(false)}>
          {pending.options?.[1]?.label || "拒绝"}
        </Button>,
        <Button key="approve" type="primary" disabled={expired} onClick={() => handleApproval(true)}>
          {pending.options?.[0]?.label || "确认"}
        </Button>,
      ]}
    >
      {pending.workspace && (
        <Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
          来自空间：{pending.workspace}
        </Paragraph>
      )}
      <Paragraph style={{ marginBottom: expired ? 8 : 16, fontSize: 14 }}>
        {pending.question}
      </Paragraph>
      {expired && (
        <Text type="danger" style={{ fontSize: 13 }}>
          该确认已超时，请等待处理结果或重试。
        </Text>
      )}
    </Modal>
  );
}
