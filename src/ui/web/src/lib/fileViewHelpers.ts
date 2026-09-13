/** FileView 内文本友好预览辅助（JSON pretty / CSV 表）。 */

const CSV_MAX_ROWS = 200;
const CSV_MAX_COLS = 30;

export function parentFsPath(path: string): string | null {
  const trimmed = path.replace(/[/\\]+$/, "");
  if (!trimmed) return null;
  const parts = trimmed.split(/[/\\]/).filter((p) => p.length > 0);
  // Windows: D: or D:\ alone — no parent
  if (/^[A-Za-z]:$/.test(trimmed)) return null;
  if (parts.length <= 1) {
    // POSIX root "/" or single segment relative — no useful parent for listing
    if (trimmed.startsWith("/") && parts.length <= 1) return null;
    if (!trimmed.includes("/") && !trimmed.includes("\\")) return null;
  }
  const useWin = /\\/.test(path) && !path.startsWith("/");
  const sep = useWin ? "\\" : "/";
  if (useWin && /^[A-Za-z]:/.test(parts[0] ?? "")) {
    if (parts.length <= 1) return null;
    return parts.slice(0, -1).join(sep);
  }
  if (trimmed.startsWith("/")) {
    if (parts.length <= 1) return "/";
    return "/" + parts.slice(0, -1).join("/");
  }
  if (parts.length <= 1) return null;
  return parts.slice(0, -1).join(sep);
}

export interface PathCrumb {
  label: string;
  path: string;
}

/** 面包屑：每段对应从根到该段的绝对/相对路径。 */
export function pathBreadcrumbs(path: string): PathCrumb[] {
  const trimmed = path.replace(/[/\\]+$/, "") || path;
  if (!trimmed) return [];
  const useWin = /\\/.test(path) && !path.startsWith("/");
  const sep = useWin ? "\\" : "/";
  const parts = trimmed.split(/[/\\]/).filter((p) => p.length > 0);
  const crumbs: PathCrumb[] = [];
  if (trimmed.startsWith("/") && !useWin) {
    let acc = "";
    for (const part of parts) {
      acc += "/" + part;
      crumbs.push({ label: part, path: acc });
    }
    return crumbs;
  }
  let acc = "";
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]!;
    acc = i === 0 ? part : `${acc}${sep}${part}`;
    crumbs.push({ label: part, path: acc });
  }
  return crumbs;
}

export function tryPrettyJson(content: string): string | null {
  const t = content.trim();
  if (!t || (t[0] !== "{" && t[0] !== "[")) return null;
  try {
    return JSON.stringify(JSON.parse(t), null, 2);
  } catch {
    return null;
  }
}

export interface CsvPreview {
  headers: string[];
  rows: string[][];
  truncatedRows: boolean;
  truncatedCols: boolean;
}

/** 简易 CSV/TSV 解析（不处理全部 RFC 边角，失败返回 null）。 */
export function tryParseCsvPreview(content: string, language?: string): CsvPreview | null {
  const delim = language === "tsv" ? "\t" : ",";
  const lines = content.replace(/^\uFEFF/, "").split(/\r?\n/).filter((ln) => ln.length > 0);
  if (lines.length < 1) return null;
  const parseLine = (line: string): string[] => {
    const cells: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i]!;
      if (inQ) {
        if (ch === '"') {
          if (line[i + 1] === '"') {
            cur += '"';
            i++;
          } else {
            inQ = false;
          }
        } else {
          cur += ch;
        }
      } else if (ch === '"') {
        inQ = true;
      } else if (ch === delim) {
        cells.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    cells.push(cur);
    return cells;
  };
  const all = lines.map(parseLine);
  const width = Math.max(...all.map((r) => r.length), 0);
  if (width < 2 && all.length < 2) return null;
  const truncatedCols = width > CSV_MAX_COLS;
  const truncatedRows = all.length > CSV_MAX_ROWS;
  const sliceW = Math.min(width, CSV_MAX_COLS);
  const headers = all[0]!.slice(0, sliceW);
  while (headers.length < sliceW) headers.push("");
  const rows = all.slice(1, CSV_MAX_ROWS).map((r) => {
    const row = r.slice(0, sliceW);
    while (row.length < sliceW) row.push("");
    return row;
  });
  return { headers, rows, truncatedRows, truncatedCols };
}
