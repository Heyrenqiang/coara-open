import { tokenQuery } from "./auth";

/** 宝箱状态（PC /api/v1/vault/status）。 */
export interface VaultStatus {
  enabled: boolean;
  initialized: boolean;
  unlocked: boolean;
  entries: number;
}

const ERROR_MESSAGES: Record<string, string> = {
  vault_disabled: "保险柜已停用",
  already_initialized: "已设置过密码，只能修改或重置",
  not_initialized: "尚未设置密码",
  wrong_password: "原密码错误",
  weak_password: "密码强度不足：至少 8 位且字母数字混合",
  toggle_failed: "切换失败，请查看服务端日志",
};

async function parse(res: Response): Promise<VaultStatus> {
  let data: { detail?: string } & Partial<VaultStatus> = {};
  try {
    data = await res.json();
  } catch {
    /* ignore */
  }
  if (!res.ok) {
    throw new Error(ERROR_MESSAGES[data?.detail ?? ""] || `请求失败（${res.status}）`);
  }
  return data as VaultStatus;
}

export async function fetchVaultStatus(): Promise<VaultStatus> {
  return fetch(`/api/v1/vault/status${tokenQuery()}`).then(parse);
}
