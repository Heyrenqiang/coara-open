/** looksLikeFsPath 回归。运行：npm run test:paths */
import { looksLikeFsPath } from "./pathDetect";

let failed = 0;

const badPaths = [
  "扇入/顺序",
  "范围/方法/规则",
  "/new",
  "/compact",
  "/status",
  "/etc",
  "submit",
  "bar",
  "AGENTS.md",
  "https://example.com",
];
for (const bad of badPaths) {
  if (looksLikeFsPath(bad)) {
    failed++;
    console.error(`FAIL looksLikeFsPath(${bad}) = true`);
  }
}

const goodPaths = [
  "src/foo.py",
  "/etc/a/b",
  "D:/x/y.md",
  "src/coara",
  "D:\\code_ws\\v8\\src",
  "./src/ui/",
  "文档/说明.md",
  "D:/项目/src",
];
for (const good of goodPaths) {
  if (!looksLikeFsPath(good)) {
    failed++;
    console.error(`FAIL looksLikeFsPath(${good}) = false`);
  }
}

if (failed) process.exit(1);
console.log(`path-detect selftest OK (${badPaths.length + goodPaths.length} assertions)`);
