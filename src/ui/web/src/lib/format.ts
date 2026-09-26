/** Web 端时间/日期展示的唯一来源：各视图一律从这里取，不再各自实现 formatTime。 */

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function parseLocal(iso: string): Date | null {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 完整时间：YYYY-MM-DD HH:mm:ss（本地时区）；空值 "—"，非法值回退原串。 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseLocal(iso);
  if (!d) return iso;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(
    d.getMinutes(),
  )}:${pad2(d.getSeconds())}`;
}

/** 列表行时间（已按日分组）：HH:mm。 */
export function formatTimeHm(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseLocal(iso);
  if (!d) return iso;
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** 分组键：YYYY-MM-DD（本地时区）；无法解析时返回空串。 */
export function formatDateGroupKey(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = parseLocal(iso);
  if (!d) return "";
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** 日期分组标题：今天 / 昨天 / YYYY-MM-DD / 未知日期。 */
export function formatDateGroupLabel(dateKey: string): string {
  if (!dateKey || dateKey === "_unknown") return "未知日期";
  const now = new Date();
  const todayKey = formatDateGroupKey(now.toISOString());
  if (dateKey === todayKey) return "今天";
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (dateKey === formatDateGroupKey(yesterday.toISOString())) return "昨天";
  return dateKey;
}
