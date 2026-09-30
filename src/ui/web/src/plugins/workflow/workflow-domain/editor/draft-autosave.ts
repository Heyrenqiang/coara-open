/**
 * 工作流草案自动保存调度：dirty 比对 + 保存串行化（台账 台账216）。
 *
 * 纯逻辑模块，不依赖 React / DOM，便于 selftest：
 *   - dirty 检查：内容与上次成功保存（或打开草稿时的基线）一致则不发保存
 *   - 串行化：同一时刻最多一个在途保存；在途期间到达的新内容排队，
 *     在途完成后只补最新一次（旧的排队内容被丢弃，避免旧盖新）
 *   - 打开草稿不触发保存：loadDraft 后调用 markSaved 登记基线
 *
 * Run selftest: npx tsx src/features/workflow/editor/draft-autosave.selftest.ts
 */

/** 递归按 key 排序后序列化，用于内容级相等比对（key 顺序差异不算变化）。 */
export function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(',')}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  const parts: string[] = [];
  for (const key of keys) {
    const v = obj[key];
    if (v === undefined) continue;
    parts.push(`${JSON.stringify(key)}:${stableStringify(v)}`);
  }
  return `{${parts.join(',')}}`;
}

/** 内容级相等（忽略对象 key 顺序）。 */
export function documentsEqual(a: unknown, b: unknown): boolean {
  return stableStringify(a) === stableStringify(b);
}

interface AutosaveOptions<T> {
  /** 实际保存动作（emit + PUT 由调用方实现） */
  save: (payload: T) => Promise<void>;
  /** 相等判定，默认 documentsEqual */
  equals?: (a: T, b: T) => boolean;
  /** 防抖毫秒数，默认 800 */
  debounceMs?: number;
  /** 定时器注入（测试用手动时钟）；返回取消函数 */
  schedule?: (fn: () => void, ms: number) => () => void;
  onSavingChange?: (saving: boolean) => void;
  /** 成功时传 null 清除错误，失败传错误文案 */
  onError?: (message: string | null) => void;
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

export class AutosaveScheduler<T> {
  /** 上次成功保存（或打开草稿登记）的内容；null = 尚无基线（如加载失败），跳过保存 */
  private baseline: T | null = null;
  /** 在途保存的内容 */
  private inFlight: T | null = null;
  /** 等待保存的最新内容 */
  private queued: T | null = null;
  private cancelTimer: (() => void) | null = null;
  private disposed = false;
  private saving = false;

  private readonly equals: (a: T, b: T) => boolean;
  private readonly debounceMs: number;
  private readonly schedule: (fn: () => void, ms: number) => () => void;

  constructor(private readonly opts: AutosaveOptions<T>) {
    this.equals = opts.equals ?? ((a, b) => documentsEqual(a, b));
    this.debounceMs = opts.debounceMs ?? 800;
    this.schedule = opts.schedule ?? defaultSchedule;
  }

  /** 打开草稿后登记基线：当前内容视为已保存，打开本身不触发保存 */
  markSaved(payload: T): void {
    if (this.disposed) return;
    this.baseline = payload;
  }

  /** 文档变化时请求一次自动保存；内容未变化则忽略 */
  request(payload: T): void {
    if (this.disposed) return;
    // 无基线（草稿加载失败）时不保存，避免把空文档盖到磁盘草稿上
    if (this.baseline === null) return;
    if (this.inFlight !== null) {
      // 在途期间只保留最新一次；与在途内容相同则无需再排
      this.queued = this.equals(payload, this.inFlight) ? null : payload;
      return;
    }
    if (this.equals(payload, this.baseline)) {
      // 回到已保存状态：取消待发的保存
      this.clearTimer();
      this.queued = null;
      return;
    }
    this.queued = payload;
    this.restartTimer();
  }

  /** 取消待发保存并停止后续动作（组件卸载 / 切换草稿时调用） */
  dispose(): void {
    this.disposed = true;
    this.clearTimer();
    this.queued = null;
  }

  /** 有未落盘的本地编辑（待发或在途）：远程刷新应让路，避免盖掉用户输入 */
  hasPending(): boolean {
    return this.cancelTimer !== null || this.inFlight !== null || this.queued !== null;
  }

  private clearTimer(): void {
    this.cancelTimer?.();
    this.cancelTimer = null;
  }

  private restartTimer(): void {
    this.clearTimer();
    this.cancelTimer = this.schedule(() => {
      this.cancelTimer = null;
      void this.flush();
    }, this.debounceMs);
  }

  private setSaving(next: boolean): void {
    if (next === this.saving) return;
    this.saving = next;
    this.opts.onSavingChange?.(next);
  }

  private async flush(): Promise<void> {
    const payload = this.queued;
    this.queued = null;
    if (payload === null || this.disposed) return;
    this.inFlight = payload;
    this.setSaving(true);
    let failed = false;
    try {
      await this.opts.save(payload);
      this.baseline = payload;
      this.opts.onError?.(null);
    } catch (e) {
      failed = true;
      this.opts.onError?.(e instanceof Error ? e.message : String(e));
    } finally {
      this.inFlight = null;
      this.setSaving(false);
    }
    if (this.disposed) return;
    const latest = this.queued;
    this.queued = null;
    if (latest === null) return;
    // 在途保存已覆盖最新内容：无需补
    if (this.baseline !== null && this.equals(latest, this.baseline)) return;
    // 相同内容刚保存失败：立即重试会变热循环，等下一次 request 再试
    if (failed && this.equals(latest, payload)) return;
    this.queued = latest;
    void this.flush();
  }
}
