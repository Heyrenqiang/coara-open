/**
 * Self-test for 草案自动保存调度（台账 台账216）：
 * dirty 比对、打开不触发、保存串行化（在途期间排队、完成后只补最新一次）。
 * Run: npx tsx src/features/workflow/editor/draft-autosave.selftest.ts
 */
import { AutosaveScheduler, documentsEqual, stableStringify } from './draft-autosave';

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

type Doc = Record<string, unknown>;

/** 手动时钟：schedule 只记录回调，fire 才触发 */
function makeManualClock() {
  let pending: (() => void) | null = null;
  return {
    schedule: (fn: () => void, _ms: number) => {
      pending = fn;
      return () => {
        if (pending === fn) pending = null;
      };
    },
    fire: () => {
      const fn = pending;
      pending = null;
      fn?.();
    },
    hasPending: () => pending !== null,
  };
}

/** 让 flush 内部的 await 链走完 */
function tick(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

const docA: Doc = { name: 'a', steps: { s1: { type: 'run' } } };
const docB: Doc = { name: 'b', steps: { s1: { type: 'run' } } };
const docC: Doc = { name: 'c', steps: { s1: { type: 'run' } } };
const docD: Doc = { name: 'd', steps: { s1: { type: 'run' } } };

// ── dirty 比对 ──────────────────────────────────────────────
{
  assert(
    documentsEqual({ a: 1, b: { c: 2, d: [3, 4] } }, { b: { d: [3, 4], c: 2 }, a: 1 }),
    'documentsEqual 应忽略对象 key 顺序',
  );
  assert(!documentsEqual({ a: 1 }, { a: 2 }), 'documentsEqual 应识别内容差异');
  assert(documentsEqual({ a: 1 }, { a: 1, b: undefined }), 'undefined 键应视为缺失');
  assert(stableStringify({ a: 1, b: undefined }) === '{"a":1}', 'stableStringify 应跳过 undefined');
  assert(!documentsEqual(docA, docB), 'docA/docB 不应相等');
}

// ── 打开草稿不触发保存（markSaved 基线） ──────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => { saved.push(doc); },
    schedule: clock.schedule,
  });
  scheduler.markSaved(docA);
  scheduler.request(docA); // 加载后 effect 触发，内容与基线一致
  assert(!clock.hasPending(), '打开草稿不应调度保存');
  clock.fire();
  await tick();
  assert(saved.length === 0, `打开草稿不应保存，实际保存 ${saved.length} 次`);
  scheduler.dispose();
}

// ── 无基线（加载失败）不保存 ──────────────────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => { saved.push(doc); },
    schedule: clock.schedule,
  });
  scheduler.request(docA);
  assert(!clock.hasPending(), '无基线不应调度保存');
  clock.fire();
  await tick();
  assert(saved.length === 0, '无基线不应保存（避免空文档覆盖磁盘草稿）');
  scheduler.dispose();
}

// ── 内容变化 → 防抖后保存一次 ────────────────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const savingStates: boolean[] = [];
  const errors: (string | null)[] = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => { saved.push(doc); },
    schedule: clock.schedule,
    onSavingChange: (s) => savingStates.push(s),
    onError: (m) => errors.push(m),
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  assert(clock.hasPending(), '内容变化应调度保存');
  clock.fire();
  await tick();
  assert(saved.length === 1 && saved[0] === docB, `应保存 docB 一次，实际 ${saved.length} 次`);
  assert(
    savingStates.join(',') === 'true,false',
    `saving 应按 true→false 翻转，实际 ${savingStates.join(',')}`,
  );
  assert(errors.length === 1 && errors[0] === null, '成功后应清除错误');
  scheduler.dispose();
}

// ── 回到基线内容取消待发保存 ─────────────────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => { saved.push(doc); },
    schedule: clock.schedule,
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  assert(clock.hasPending(), '变化后应有待发保存');
  scheduler.request(docA); // 用户撤销回已保存内容
  assert(!clock.hasPending(), '回到基线应取消待发保存');
  clock.fire();
  await tick();
  assert(saved.length === 0, '回到基线不应保存');
  scheduler.dispose();
}

// ── 串行化：在途期间多次变化，完成后只补最新一次 ─────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const gates: Array<() => void> = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: (doc) => {
      saved.push(doc);
      return new Promise<void>((resolve) => gates.push(resolve));
    },
    schedule: clock.schedule,
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  clock.fire(); // 保存 B 进入在途（不 resolve）
  assert(saved.length === 1 && saved[0] === docB, '在途保存应为 docB');

  scheduler.request(docC); // 在途期间到达：排队
  scheduler.request(docD); // 再到达：C 被丢弃，只留最新 D
  assert(!clock.hasPending(), '在途期间不应再起防抖定时器');

  gates[0](); // B 完成
  await tick();
  assert(saved.length === 2 && saved[1] === docD, `完成后应只补最新 docD，实际 ${JSON.stringify(saved)}`);

  gates[1](); // D 完成
  await tick();
  assert(saved.length === 2, `不应有第三次保存，实际 ${saved.length} 次`);
  scheduler.dispose();
}

// ── 在途期间内容与在途相同：无需补保存 ───────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const gates: Array<() => void> = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: (doc) => {
      saved.push(doc);
      return new Promise<void>((resolve) => gates.push(resolve));
    },
    schedule: clock.schedule,
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  clock.fire();
  scheduler.request(docC);
  scheduler.request(docB); // 又回到在途内容
  gates[0]();
  await tick();
  assert(saved.length === 1, `在途内容已是最新时不应补保存，实际 ${saved.length} 次`);
  scheduler.dispose();
}

// ── 保存失败：不热重试，等下一次 request 再试 ────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const errors: (string | null)[] = [];
  let fail = true;
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => {
      saved.push(doc);
      if (fail) throw new Error('网络错误');
    },
    schedule: clock.schedule,
    onError: (m) => errors.push(m),
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  clock.fire();
  await tick();
  await tick();
  assert(saved.length === 1, `失败后不应自动热重试，实际保存 ${saved.length} 次`);
  assert(errors.length === 1 && errors[0] === '网络错误', `失败应上报错误，实际 ${JSON.stringify(errors)}`);

  fail = false;
  scheduler.request(docB); // 内容仍与基线不同：允许再次保存
  assert(clock.hasPending(), '失败后再次 request 应重新调度');
  clock.fire();
  await tick();
  assert(saved.length === 2 && saved[1] === docB, '下一次 request 应重试成功');
  assert(errors.length === 2 && errors[1] === null, '成功后应清除错误');
  scheduler.dispose();
}

// ── dispose：取消待发，不再保存 ──────────────────────────────
{
  const clock = makeManualClock();
  const saved: Doc[] = [];
  const scheduler = new AutosaveScheduler<Doc>({
    save: async (doc) => { saved.push(doc); },
    schedule: clock.schedule,
  });
  scheduler.markSaved(docA);
  scheduler.request(docB);
  scheduler.dispose();
  assert(!clock.hasPending(), 'dispose 应取消待发保存');
  clock.fire();
  await tick();
  assert(saved.length === 0, 'dispose 后不应保存');
}

// ── 旧调度器迟到回调不碰新草稿 UI state（台账 台账341）─────────────
// 模拟 WorkflowEditor 的守卫：回调碰 UI state 前先判 active（autosaveRef.current），
// 新调度器接管时复位保存指示
{
  const clockA = makeManualClock();
  const clockB = makeManualClock();
  const gates: Array<() => void> = [];
  // 模拟组件 UI state
  let wdlText = '';
  let saving = false;
  let saveError = '';
  // 模拟 WorkflowEditor 的 autosaveRef：当前生效的调度器
  let active: AutosaveScheduler<Doc> | null = null;

  const makeScheduler = (clock: ReturnType<typeof makeManualClock>, wdl: string) => {
    const scheduler = new AutosaveScheduler<Doc>({
      save: async (_doc) => {
        // 网络请求（emit + PUT）：无论新旧调度器都完成
        await new Promise<void>((resolve) => gates.push(resolve));
        // 迟到回调碰 UI state 前先判 active
        if (active === scheduler) wdlText = wdl;
      },
      schedule: clock.schedule,
      onSavingChange: (s) => { if (active === scheduler) saving = s; },
      onError: (m) => { if (active === scheduler) saveError = m || ''; },
    });
    active = scheduler;
    // 新调度器接管时复位保存指示
    saving = false;
    saveError = '';
    return scheduler;
  };

  const schedulerA = makeScheduler(clockA, 'wdl-B');
  schedulerA.markSaved(docA);
  schedulerA.request(docB);
  clockA.fire(); // A 的在途保存开始（挂起在 gate）
  assert(saving === true, 'A 在途保存应亮起保存指示');

  // 切换草稿：dispose 旧调度器，新建 B 并成为 active
  schedulerA.dispose();
  const schedulerB = makeScheduler(clockB, 'wdl-D');
  schedulerB.markSaved(docC);
  assert(saving === false, '切换草稿应复位保存指示');

  gates[0](); // A 的在途保存迟到完成
  await tick();
  await tick();
  assert(wdlText === '', `旧调度器迟到回调不应覆盖文本框，实际 "${wdlText}"`);
  assert(saveError === '', `旧调度器迟到回调不应写错误，实际 "${saveError}"`);

  // 新调度器正常工作：UI state 正常更新
  schedulerB.request(docD);
  clockB.fire();
  assert(saving === true, 'B 在途保存应亮起保存指示');
  gates[1]();
  await tick();
  assert(wdlText === 'wdl-D', `新调度器应正常写文本框，实际 "${wdlText}"`);
  assert(saving === false, 'B 保存完成应熄灭保存指示');
  schedulerB.dispose();
}

console.log('draft-autosave.selftest: OK');
