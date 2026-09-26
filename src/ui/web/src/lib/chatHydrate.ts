/** 权威快照入口（/api/session/messages）：挂载 hydrate 与 WS 重连尾部补拉共用。
 *  全量＝线身份变/无内容；增量＝viewReady 时只取 maxSeq 之后（前缀冻结）。 */
import { fetchSessionMessages } from "./api";
import { useStore } from "./store";

/** 增量重叠窗：游标偏移时同 seq 原地替换，不重影。 */
export const HYDRATE_OVERLAP = 50;

/** 单次取量 ≤ 服务端 _MAX_HISTORY_LIMIT(500)，≥ _DEFAULT_HISTORY_LIMIT(100)。 */
export const HYDRATE_FETCH_LIMIT = 200;

/** 拉并提交一次 hydrate。边界未定不拉；silent 失败只 console。 */
export function runSessionHydrate(
  targetDir: string,
  { forceFull = false, sameBoundaryTopUp = false, silent = false, onError }: {
    forceFull?: boolean;
    sameBoundaryTopUp?: boolean;
    silent?: boolean;
    onError?: (err: unknown) => void;
  } = {},
): boolean {
  const st = useStore.getState();
  const generation = st.bumpHydrateGeneration();
  // after 只决定拉多少；对账一律只追加 + 只改内容
  const maxSeq = st.messages.reduce((acc, m) => Math.max(acc, m.seq ?? 0), 0);
  const after =
    !forceFull && (st.viewReady || sameBoundaryTopUp) && st.messages.length > 0 && maxSeq > 0
      ? Math.max(0, maxSeq - HYDRATE_OVERLAP)
      : undefined;
  fetchSessionMessages(HYDRATE_FETCH_LIMIT, after, { workspaceDir: targetDir })
    .then((data) => {
      const cur = useStore.getState();
      if (cur.workspaceDir !== targetDir) return;
      if (cur.hydrateGeneration !== generation) return;
      const epoch = String(data.epoch ?? "").trim();
      // epoch 变＝线重建：增量作废，改全量
      if (after !== undefined && epoch && cur.lineEpoch && epoch !== cur.lineEpoch) {
        runSessionHydrate(targetDir, { forceFull: true, silent });
        return;
      }
      const rt = data.runtime;
      const rtDir = String(rt?.workspace_dir ?? "").trim();
      const runtimeInBoundary = Boolean(rt) && (!rtDir || rtDir === targetDir);
      const truncated = data.subagent_truncated;
      if (truncated) {
        const dropped =
          (truncated.results ?? 0) + (truncated.diffs ?? 0) + (truncated.briefs ?? 0) + (truncated.texts ?? 0);
        if (dropped > 0) {
          console.debug(`子智能体折叠区已封顶：更早过程已省略 ${dropped} 项`, truncated);
        }
      }
      cur.loadHistory(data.messages, data.latest_seq, {
        workspaceDir: targetDir,
        generation,
        incremental: after !== undefined,
        epoch,
        total: data.total,
        ...(runtimeInBoundary && rt ? { runtime: rt } : {}),
        subagentResults: data.subagent_results,
        subagentDiffs: data.subagent_diffs,
        subagentBriefs: data.subagent_briefs,
        subagentTexts: data.subagent_texts,
      });
    })
    .catch((err) => {
      console.error("Failed to hydrate session messages:", err);
      if (!silent) onError?.(err);
    });
  return true;
}

/** 重连尾部补拉：仅 viewReady 且已有内容时按 maxSeq 补后缀。 */
export function topUpAfterReconnect(): void {
  const st = useStore.getState();
  const dir = st.workspaceDir;
  if (!dir) return;
  if (!st.viewReady || st.messages.length === 0) return;
  runSessionHydrate(dir, { silent: true });
}
