import { useEffect, useRef, useState } from "react";
import { Button, Card, Input, Typography, message } from "antd";
import { requestAccountCode, verifyAccountCode } from "../../lib/account";

/** 登录页：邮箱 → 邮箱验证码 → 完成登录（首验即注册）。embedded 时去掉整页外壳（嵌进个人页用）。 */
export function AccountLogin({
  onSuccess,
  embedded,
}: {
  onSuccess: () => void;
  embedded?: boolean;
}) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  const startCooldown = () => {
    setCooldown(60);
    timerRef.current = setInterval(() => {
      setCooldown((c) => {
        if (c <= 1 && timerRef.current) {
          clearInterval(timerRef.current);
          timerRef.current = null;
        }
        return c - 1;
      });
    }, 1000);
  };

  const sendCode = async () => {
    if (!email.trim()) {
      message.warning("请填写邮箱");
      return;
    }
    setSending(true);
    try {
      await requestAccountCode(email.trim());
      setCodeSent(true);
      startCooldown();
      message.success(`验证码已发送至 ${email.trim()}`);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "发送失败");
    } finally {
      setSending(false);
    }
  };

  const verify = async () => {
    if (!code.trim()) {
      message.warning("请填写验证码");
      return;
    }
    setVerifying(true);
    try {
      await verifyAccountCode(email.trim(), code.trim());
      message.success("登录成功");
      onSuccess();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "登录失败");
    } finally {
      setVerifying(false);
    }
  };

  const card = (
    <Card style={{ width: 380, maxWidth: "100%" }} styles={{ body: { padding: 32 } }}>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        登录考拉账户
      </Typography.Title>
        <Input
          placeholder="邮箱"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={{ marginBottom: 12 }}
          disabled={codeSent}
        />
        {codeSent && (
          <Input
            placeholder="验证码"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            style={{ marginBottom: 12 }}
            onPressEnter={verify}
            maxLength={6}
          />
        )}
      {codeSent ? (
        <div style={{ display: "flex", gap: 8 }}>
          <Button onClick={sendCode} loading={sending} disabled={cooldown > 0}>
            {cooldown > 0 ? `重新发送（${cooldown}s）` : "重新发送"}
          </Button>
          <Button type="primary" onClick={verify} loading={verifying} block>
            登录
          </Button>
        </div>
      ) : (
        <Button type="primary" onClick={sendCode} loading={sending} block>
          发送验证码
        </Button>
      )}
    </Card>
  );

  if (embedded) return card;
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--coara-bg-subtle)",
      }}
    >
      {card}
    </div>
  );
}
