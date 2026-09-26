#!/usr/bin/env node
/**
 * 设计令牌门禁 —— src/ 内除 theme/tokens.ts 外，禁止出现裸色值。
 *
 * 依据：docs/Web设计体系.md §3「令牌：单一真源」§5「治理」。
 *
 * 用法：
 *   node scripts/check-tokens.mjs          报告模式：列出清单，退出 0（当前为清债期）
 *   node scripts/check-tokens.mjs --fail   门禁模式：发现裸色值退出 1（清零后接入构建/CI）
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));
// 放行清单：令牌真源 + 画布专属令牌表（React Flow 节点/端口/状态色，
// 全站令牌无对应语义；文件头有与 tokens.ts 的关系说明）。
const ALLOW = new Set(["theme/tokens.ts", "features/workflow/editor/theme.ts"]);

const HEX = /#[0-9a-fA-F]{3,8}\b/g;
const RGB = /\brgba?\(/g;
const SCAN_EXT = [".ts", ".tsx", ".css"];

function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, out);
    } else if (SCAN_EXT.some((e) => entry.name.endsWith(e))) {
      out.push(full);
    }
  }
  return out;
}

function rel(p) {
  return relative(SRC, p).split(sep).join("/");
}

const files = walk(SRC);
const findings = [];
let hexTotal = 0;
let rgbTotal = 0;

for (const file of files) {
  const r = rel(file);
  if (ALLOW.has(r)) continue;
  const text = readFileSync(file, "utf8").split("\n");
  const hits = [];
  text.forEach((line, i) => {
    const hex = line.match(HEX) || [];
    const rgb = line.match(RGB) || [];
    if (hex.length || rgb.length) {
      hexTotal += hex.length;
      rgbTotal += rgb.length;
      hits.push({ line: i + 1, hex: hex.length, rgb: rgb.length });
    }
  });
  if (hits.length) findings.push({ file: r, hits });
}

findings.sort((a, b) => b.hits.length - a.hits.length);

console.log("设计令牌门禁 · 裸色值报告");
console.log(`扫描：${files.length} 个源文件（放行 ${[...ALLOW].join(", ")}）`);
console.log(`裸 hex：${hexTotal} 处　半径 rgba/rgb：${rgbTotal} 处\n`);

if (findings.length === 0) {
  console.log("✓ 无裸色值。");
  process.exit(0);
}

console.log("按文件（降序）：");
for (const f of findings) {
  const hex = f.hits.reduce((n, h) => n + h.hex, 0);
  const rgb = f.hits.reduce((n, h) => n + h.rgb, 0);
  console.log(`  ${f.file}  —— hex ${hex}，rgba ${rgb}（${f.hits.length} 行）`);
}

if (process.argv.includes("--fail")) {
  console.error("\n✗ 门禁未通过：请把裸色值改为 tokens.ts 的语义令牌 / var(--coara-*)。");
  process.exit(1);
}
console.log("\n（报告模式：退出 0。清债完成后请以 --fail 接入构建。）");
process.exit(0);
