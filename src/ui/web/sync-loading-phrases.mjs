/** 加载词库同步：单一事实源是 src/records/loading_phrases.json（required-assets
 *  打包项）。web 端复用同一份——tsc/vite 之前把 CLI 源复制到 src/lib/，
 *  避免双份漂移。npm prebuild / predev 钩子自动调用。 */
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const src = fileURLToPath(new URL("../../records/loading_phrases.json", import.meta.url));
const dest = fileURLToPath(new URL("./src/lib/loading_phrases.json", import.meta.url));
copyFileSync(src, dest);
console.log("[sync-loading-phrases] synced from src/records/loading_phrases.json");
