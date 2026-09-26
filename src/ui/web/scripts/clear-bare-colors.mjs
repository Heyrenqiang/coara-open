#!/usr/bin/env node
/**
 * 一次性裸色值清零脚本（迁移顺序第 2 步）。
 * 依据：docs/Web设计体系.md §3。
 *
 * 映射原则：
 *  - 语义色 → 已有语义令牌（顺带合并重复色，见 REPORTS 里的像素影响清单）
 *  - 内容调色板（语法高亮 / diff / 代码画布 / 状态）→ 专用令牌组
 *  - 阴影 → 整条 token 化（避免 rgba 碎散）
 *  - var(--coara-x, #hex) 的 fallback 一律剥掉（令牌已保证存在）
 *
 * 用法：node scripts/clear-bare-colors.mjs [--dry]
 */
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));
const SKIP = new Set(["theme/tokens.ts"]);
const SCAN_EXT = [".ts", ".tsx", ".css"];

/* 全局 hex 映射（键一律小写） */
const GLOBAL = {
  "#ffffff": "var(--coara-surface)",
  "#fff": "var(--coara-surface)",
  "#fafafa": "var(--coara-bg-subtle)",
  "#fbfbfc": "var(--coara-bg-subtle)",
  "#f5f5f5": "var(--coara-bg-subtle)",
  "#f3f4f6": "var(--coara-bg-subtle)",
  "#f7f7f7": "var(--coara-bg-subtle)",
  "#f7f7f8": "var(--coara-bg-subtle)",
  "#f0f0f0": "var(--coara-border-faint)",
  "#ececec": "var(--coara-border-soft)",
  "#e5e5e5": "var(--coara-border-muted)",
  "#e5e7eb": "var(--coara-border-muted)",
  "#d9d9d9": "var(--coara-border)",
  "#d4d4d4": "var(--coara-border)",
  "#d1d5db": "var(--coara-border)",
  "#d0d4d9": "var(--coara-border)",
  "#c0c4cc": "var(--coara-text-tertiary)",
  "#9ca3af": "var(--coara-text-tertiary)",
  "#8c8c8c": "var(--coara-text-faint)",
  "#6b7280": "var(--coara-text-secondary)",
  "#6b6b6b": "var(--coara-text-muted)",
  "#595959": "var(--coara-text-secondary)",
  "#262626": "var(--coara-text)",
  "#1f2937": "var(--coara-text)",
  "#111827": "var(--coara-text)",
  "#141414": "var(--coara-text)",
  "#000000": "var(--coara-text-active)",
  "#e8e8e8": "var(--coara-bubble-user)",
  "#3b82f6": "var(--coara-accent)",
  "#1677ff": "var(--coara-accent)",
  "#eff6ff": "var(--coara-accent-subtle)",
  "#f0f9ff": "var(--coara-accent-tint-a)",
  "#e0f2fe": "var(--coara-accent-tint-b)",
  "#faad14": "var(--coara-warning)",
  "#f59e0b": "var(--coara-warning)",
  "#22c55e": "var(--coara-success)",
  "#10b981": "var(--coara-success)",
  "#16a34a": "var(--coara-success)",
  "#166534": "var(--coara-success-strong)",
  "#ef4444": "var(--coara-error)",
  "#dc2626": "var(--coara-danger)",
  "#cf1322": "var(--coara-danger)",
  "#991b1b": "var(--coara-danger-strong)",
  "#0e7490": "var(--coara-progress)",
  "#0f1115": "var(--coara-code-canvas)",
  "#161a20": "var(--coara-code-bar)",
  "#1f242c": "var(--coara-code-border)",
  "#e6edf3": "var(--coara-code-text)",
  "#7d8590": "var(--coara-code-muted)",
  "#3fb950": "var(--coara-code-dot)",
  "#e6ffec": "var(--coara-diff-add-bg)",
  "#ffebe9": "var(--coara-diff-del-bg)",
  "#1a7f37": "var(--coara-diff-add-fg)",
  "#0a3069": "var(--coara-syntax-string)",
  "#0550ae": "var(--coara-syntax-number)",
  "#6639ba": "var(--coara-syntax-title)",
  "#953800": "var(--coara-syntax-variable)",
};

/* 按文件覆盖：#cf222e 在语法高亮与 diff 两种语境下语义不同 */
const OVERRIDES = {
  "index.css": { "#cf222e": "var(--coara-syntax-red)" },
  "components/DiffBlocksView.tsx": { "#cf222e": "var(--coara-diff-del-fg)" },
};

/* 整条阴影字符串 → token */
const SHADOWS = [
  ["0 0 0 3px rgba(59,130,246,0.15)", "var(--coara-focus-ring)"],
  ["0 1px 2px rgba(0,0,0,0.03)", "var(--coara-shadow-xs)"],
  ["0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.04)", "var(--coara-shadow-pop)"],
  ["0 2px 4px rgba(0,0,0,0.1)", "var(--coara-shadow-btn)"],
  ["0 4px 8px rgba(0,0,0,0.15)", "var(--coara-shadow-btn-hover)"],
  ["0 2px 8px rgba(59,130,246,0.1)", "var(--coara-shadow-accent-sm)"],
  ["0 8px 28px rgba(0, 0, 0, 0.12)", "var(--coara-shadow-pop-lg)"],
  ["0 2px 8px rgba(0, 0, 0, 0.15), 0 8px 24px rgba(0, 0, 0, 0.12)", "var(--coara-shadow-float)"],
  ["0 4px 12px rgba(0, 0, 0, 0.2), 0 12px 32px rgba(0, 0, 0, 0.16)", "var(--coara-shadow-float-hover)"],
  ["0 1px 4px rgba(0, 0, 0, 0.15)", "var(--coara-shadow-chip)"],
  ["0 2px 12px rgba(0,0,0,0.12)", "var(--coara-shadow-modal)"],
  ["0 4px 12px rgba(0,0,0,0.05)", "var(--coara-shadow-hover)"],
  ["0 1px 2px rgba(59, 130, 246, 0.15)", "var(--coara-shadow-primary)"],
  ["0 2px 6px rgba(59, 130, 246, 0.2)", "var(--coara-shadow-primary-hover)"],
];

/* 剩余独立 rgba → 洗色/滚条令牌 */
const RGBA = [
  ["rgba(59, 130, 246, 0.08)", "var(--coara-accent-wash)"],
  ["rgba(239, 68, 68, 0.04)", "var(--coara-danger-wash)"],
  ["rgba(0, 0, 0, 0.12)", "var(--coara-scroll-thumb)"],
  ["rgba(0, 0, 0, 0.22)", "var(--coara-scroll-thumb-hover)"],
];

function walk(dir, out = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.name === "node_modules") continue;
    const full = join(dir, e.name);
    if (e.isDirectory()) walk(full, out);
    else if (SCAN_EXT.some((x) => e.name.endsWith(x))) out.push(full);
  }
  return out;
}

const hexKeys = [
  ...new Set([
    ...Object.keys(GLOBAL),
    ...Object.values(OVERRIDES).flatMap((o) => Object.keys(o)),
  ]),
].sort((a, b) => b.length - a.length);
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const HEX_RE = new RegExp(hexKeys.map(esc).join("|"), "gi");
/* var(--coara-x, #hex) 的 fallback */
const FALLBACK_RE = /var\(\s*(--coara-[a-z0-9-]+)\s*,\s*#[0-9a-fA-F]{3,8}\s*\)/g;

const dry = process.argv.includes("--dry");
let totalFiles = 0;
let totalHits = 0;

for (const file of walk(SRC)) {
  const rel = relative(SRC, file).split(sep).join("/");
  if (SKIP.has(rel)) continue;
  const before = readFileSync(file, "utf8");
  let text = before;
  let hits = 0;

  text = text.replace(FALLBACK_RE, (_m, v) => {
    hits++;
    return `var(${v})`;
  });

  for (const [from, to] of SHADOWS) {
    if (text.includes(from)) {
      hits += text.split(from).length - 1;
      text = text.split(from).join(to);
    }
  }

  const ov = OVERRIDES[rel] || {};
  text = text.replace(HEX_RE, (m) => {
    hits++;
    const key = m.toLowerCase();
    return ov[key] ?? GLOBAL[key] ?? m;
  });

  for (const [from, to] of RGBA) {
    if (text.includes(from)) {
      hits += text.split(from).length - 1;
      text = text.split(from).join(to);
    }
  }

  if (text !== before) {
    totalFiles++;
    totalHits += hits;
    if (!dry) writeFileSync(file, text, "utf8");
    console.log(`  ${rel}  ${hits} 处`);
  }
}

console.log(`\n${dry ? "[dry-run] " : ""}改写 ${totalFiles} 个文件，共 ${totalHits} 处。`);
