import { tokenQuery } from "./auth";

export interface AccountStatus {
  logged_in: boolean;
  email?: string;
  device_name?: string;
  /** 门禁状态：active/trial/need_login/locked_payment/... */
  gate_state?: string;
  gate_reason?: string;
  /** 累计活跃时长（秒），仅登录后下发，用于展示配额进度 */
  quota_active_seconds?: number;
}

const ERROR_MESSAGES: Record<string, string> = {
  email_invalid: "邮箱格式不正确",
  resend_too_fast: "发送太频繁，请稍后再试",
  code_invalid: "验证码错误",
  code_expired: "验证码已过期，请重新发送",
  token_expired: "登录已过期（连续 7 天未使用），请重新验证",
  device_limit: "设备数已达上限（每账户 3 台）",
  device_id_required: "设备标识缺失",
  server_unreachable: "无法连接许可服务，请检查网络",
};

async function parseError(res: Response): Promise<Error> {
  let detail = "";
  try {
    const data = await res.json();
    detail = typeof data?.detail === "string" ? data.detail : "";
  } catch {
    /* ignore */
  }
  return new Error(ERROR_MESSAGES[detail] || `请求失败（${res.status}）`);
}

export async function fetchAccountStatus(): Promise<AccountStatus> {
  const res = await fetch(`/api/v1/account/status${tokenQuery()}`);
  if (!res.ok) return { logged_in: false };
  return res.json();
}

export async function requestAccountCode(email: string): Promise<void> {
  const res = await fetch(`/api/v1/account/request-code${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!res.ok) throw await parseError(res);
}

export async function verifyAccountCode(
  email: string,
  code: string
): Promise<void> {
  const res = await fetch(`/api/v1/account/verify${tokenQuery()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code }),
  });
  if (!res.ok) throw await parseError(res);
}

export async function logoutAccount(): Promise<void> {
  const res = await fetch(`/api/v1/account/logout${tokenQuery()}`, {
    method: "POST",
  });
  if (!res.ok) throw await parseError(res);
}
