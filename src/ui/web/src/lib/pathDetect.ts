/** Markdown 链接 href 是否为「文件 / 文件夹」路径（网页用 http(s) 另判）。
 *  宁缺毋滥：只服务显式 `[文案](路径)`，不做正文裸路径扫描。 */

/** 路径段：无空白、无分隔符、无 Windows 非法字符；允许中文等非 ASCII */
const SEG = String.raw`[^\s\\/<>"|*?]+`;

/** 剥离尾部斜杠后的规范化（文件夹常带 / 或 \\） */
function stripTrailingSep(s: string): string {
  return s.replace(/[/\\]+$/, "");
}

/** 相对路径防误判：须有文件后缀，或至少一段含 ASCII（排除「扇入/顺序」类） */
function relativePathPlausible(s: string): boolean {
  const body = s.replace(/^\.{1,2}[\\/]/, "");
  const parts = body.split(/[\\/]/).filter(Boolean);
  if (parts.length < 2) return false;
  const last = parts[parts.length - 1] ?? "";
  if (/\.[A-Za-z0-9]{1,12}$/.test(last)) return true;
  return parts.some((p) => /[A-Za-z0-9]/.test(p));
}

/** 文件或文件夹路径（绝对 / 相对多段） */
export function looksLikeFsPath(raw: string): boolean {
  const trimmed = raw.trim();
  if (!trimmed) return false;
  const s = stripTrailingSep(trimmed);
  if (s.length < 3 || s.length > 240) return false;
  if (/\s/.test(s)) return false;
  if (/:\/\//.test(s)) return false; // URL 走网页通道
  // Windows 绝对路径：D:\... 或 D:/...（段可含中文）
  if (new RegExp(String.raw`^[A-Za-z]:[\\/]${SEG}([\\/]${SEG})*$`).test(s)) return true;
  // POSIX 绝对 / 家目录：至少两段（排除 /new 等单段斜杠命令）
  if (new RegExp(String.raw`^/${SEG}([\\/]${SEG})+$`).test(s)) return true;
  if (new RegExp(String.raw`^~[\\/]${SEG}([\\/]${SEG})*$`).test(s)) return true;
  // 相对路径：多段 + 防误判
  if (new RegExp(String.raw`^(\.{1,2}[\\/])?${SEG}([\\/]${SEG})+$`).test(s) && relativePathPlausible(s)) {
    return true;
  }
  return false;
}
