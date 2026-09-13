/** fileViewHelpers 自测。运行：npx tsx src/lib/fileViewHelpers.selftest.ts */
import {
  parentFsPath,
  pathBreadcrumbs,
  tryParseCsvPreview,
  tryPrettyJson,
} from "./fileViewHelpers";

let failed = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) {
    failed++;
    console.error("FAIL", msg);
  }
}

assert(parentFsPath(String.raw`D:\a\b\c.py`) === String.raw`D:\a\b`, "win parent");
assert(parentFsPath("/a/b/c") === "/a/b", "posix parent");
assert(tryPrettyJson('{"a":1}')?.includes("\n") === true, "pretty json");
assert(tryPrettyJson("not json") === null, "bad json");
const csv = tryParseCsvPreview("a,b\n1,2\n3,4", "csv");
assert(csv != null && csv.headers[0] === "a" && csv.rows.length === 2, "csv parse");
const crumbs = pathBreadcrumbs(String.raw`D:\proj\src`);
assert(crumbs.length === 3 && crumbs[2]?.path === String.raw`D:\proj\src`, "crumbs");

if (failed) process.exit(1);
console.log("fileViewHelpers selftest OK");
