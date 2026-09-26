/** Store reconcile selftest. Run: npx tsx src/lib/store.selftest.ts */
import {
  chatRowKey,
  resetPendingFramesForTest,
  resetSubagentChunkMarksForTest,
  useStore,
} from "./store.ts";
import { buildToolLineGroups, workRowsOf } from "./toolLineGroups.ts";
import { anchorAtViewportTop, BOTTOM_ANCHOR, decideScroll } from "./chatScroll.ts";
import { isHiddenToolLine } from "./toolVisibility.ts";

function assert(cond: unknown, msg: string): void {
  if (!cond) throw new Error(msg);
}

// frames carry workspace_dir/session_id
const TEST_DIR = "/ws/t";
const TEST_SID = "sid-t";
const TEST_EPOCH = `${TEST_DIR}::root`;

function reset(): void {
  // clear module-level frame buffer
  resetPendingFramesForTest();
  resetSubagentChunkMarksForTest();
  useStore.setState({
    messages: [],
    turnActive: false,
    currentTurnId: null,
    turnStartedAt: null,
    workspaceDir: TEST_DIR,
    sessionId: TEST_SID,
    // reset gate/skeleton/hydrate bits
    hydratedSeq: 0,
    viewReady: false,
    skeletonSince: null,
    needsHydrate: false,
    lineEpoch: null,
    subagentRows: [],
    subagentOutput: {},
    subagentDiffs: {},
    subagentBriefs: {},
  });
}

function h(msg: Parameters<ReturnType<typeof useStore.getState>["handleServerMessage"]>[0]): void {
  const st = useStore.getState();
  useStore.getState().handleServerMessage({
    workspace_dir: st.workspaceDir ?? undefined,
    session_id: st.sessionId ?? undefined,
    ...msg,
  });
}

/** h() with fixed TEST_DIR/TEST_SID. */
function hBound(msg: Record<string, unknown>): void {
  const st = useStore.getState();
  st.handleServerMessage({
    workspace_dir: TEST_DIR,
    session_id: TEST_SID,
    ...msg,
  } as unknown as Parameters<typeof st.handleServerMessage>[0]);
}

// claim: reconnect replay
{
  reset();
  const s = useStore.getState;
  // 首次：乐观泡认领 → S1 权威帧认领
  s().addUserMessage("你好");
  h({ type: "user_message", content: "你好", source: "web", turn_id: "t1" });
  assert(s().messages.length === 1, "S1: claim optimistic bubble, no append");
  assert(s().messages[0].turn_id === "t1", "S1: claimed bubble bound to t1");
  assert(s().messages[0].optimistic !== true, "S1: optimistic cleared");
  h({ type: "turn_start", turn_id: "t1", source: "web" });
  // turn_start 不再创建空气泡（正文块到达时自建），只标记回合进行中。
  assert(s().messages.length === 1, "S1: turn_start marks turn active, no empty bubble");
  assert(s().turnActive === true, "S1: turn active");
  assert(s().currentTurnId === "t1", "S1: current turn bound");

  // 第一次重连回放：user_message 先于 turn_start（服务端真实帧序）
  reset();
  h({ type: "user_message", content: "你好", source: "web", turn_id: "t1" });
  h({ type: "turn_start", turn_id: "t1", source: "web" });
  let users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 1, "replay#1: exactly one user bubble");
  assert(users[0].text === "你好", "replay#1: user text");
  assert(users[0].turn_id === "t1", "replay#1: replay bubble bound to t1");

  // 第二次重连回放同一回合：不得把上轮回放泡当作乐观泡认领后重复追加
  h({ type: "user_message", content: "你好", source: "web", turn_id: "t1" });
  h({ type: "turn_start", turn_id: "t1", source: "web" });
  users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 1, "replay#2: still exactly one user bubble (no duplicate)");
  // turn_start 不建空气泡——正文 chunk 到达才建气泡。
  const assistants = s().messages.filter((m) => m.role === "assistant");
  assert(assistants.length === 0, "replay#2: no assistant bubble until chunk arrives");

  // 他端 source 不触碰 web 气泡
  h({ type: "user_message", content: "你好", source: "cli", turn_id: "t9" });
  assert(s().messages.filter((m) => m.role === "user").length === 1, "cli source ignored");
}

// claim: optimistic vs bound
{
  reset();
  const s = useStore.getState;
  // 已绑定回合的权威泡（回放路径产生，non-optimistic）
  h({ type: "user_message", content: "问题A", source: "web", turn_id: "tA" });
  h({ type: "turn_start", turn_id: "tA", source: "web" });
  // 用户紧接着又发一条（乐观泡，尚无 turn_id）
  s().addUserMessage("问题B");
  // 权威帧到达：认领最近的无 tid 乐观泡，文本覆盖为权威内容
  h({ type: "user_message", content: "问题B", source: "web", turn_id: "tB" });
  const users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 2, "variant: two distinct user bubbles");
  assert(users[0].turn_id === "tA", "variant: first bubble untouched");
  assert(users[1].turn_id === "tB" && users[1].optimistic !== true, "variant: newest claimed");
}

// hydrate: anchor window
{
  reset();
  const s = useStore.getState;
  const REP = "同文回复正文足够长以参与byKey配对";
  s().loadHistory([
    { role: "user", text: "在吗" },
    { role: "assistant", text: REP },
    { role: "user", text: "在吗" },
    { role: "assistant", text: REP },
  ]);
  h({ type: "user_message", content: "第三次", source: "web", turn_id: "t5" });
  h({ type: "turn_start", turn_id: "t5", source: "web" });
  h({ type: "chunk", text: "新回答", turn_id: "t5", source: "web" });
  const before = s().messages.length;
  assert(before === 6, "setup: 4 history + 2 live tail (user + assistant)");

  // hydrate 返回旧历史（磁带有落盘延迟，看不到 t5）
  s().loadHistory([
    { role: "user", text: "在吗" },
    { role: "assistant", text: REP },
    { role: "user", text: "在吗" },
    { role: "assistant", text: REP },
  ]);
  const texts = s().messages.map((m) => m.text);
  assert(s().messages.length === 6, "anchor: live tail preserved, no old dup");
  assert(
    texts.join("|") === `在吗|${REP}|在吗|${REP}|第三次|新回答`,
    "anchor: order intact, no repeated old segment",
  );
}

// hydrate: in-window tail
{
  reset();
  const s = useStore.getState;
  // 同文助手回复须长于 byKey 门槛（≤16 字符不参与 byKey 配对）
  const OLD = "旧回答正文足够长以参与byKey配对";
  s().loadHistory([
    { role: "user", text: "旧问题" },
    { role: "assistant", text: OLD },
  ]);
  h({ type: "user_message", content: "新问题", source: "web", turn_id: "t6" });
  h({ type: "turn_start", turn_id: "t6", source: "web" });
  h({ type: "chunk", text: "新回答", turn_id: "t6", source: "web" });
  s().loadHistory([
    { role: "user", text: "旧问题" },
    { role: "assistant", text: OLD },
  ]);
  const m = s().messages;
  assert(m.length === 4, "in-window: live tail kept");
  assert(m[2].text === "新问题" && m[3].text === "新回答", "in-window: tail intact");
}

// reconcile: append-only / epoch rebuild
{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "甲", seq: 1 },
      { role: "assistant", text: "乙", seq: 2 },
    ],
    2,
    { epoch: TEST_EPOCH },
  );
  // 同一条线、同一 epoch：快照内容跟屏上完全不重合 → 仍然只追加
  s().loadHistory(
    [
      { role: "user", text: "丙", seq: 3 },
      { role: "assistant", text: "丁", seq: 4 },
    ],
    4,
    { epoch: TEST_EPOCH },
  );
  assert(
    s().messages.map((m) => m.text).join("|") === "甲|乙|丙|丁",
    `same line: append-only reconcile keeps displayed rows, got ${s().messages.map((m) => m.text).join("|")}`,
  );
  // 线重建（epoch 变）：整表换成快照，顺序＝view_seq 升序
  s().loadHistory(
    [
      { role: "assistant", text: "庚", seq: 9 },
      { role: "user", text: "己", seq: 8 },
    ],
    9,
    { epoch: "epoch-2" },
  );
  assert(
    s().messages.map((m) => m.text).join("|") === "己|庚",
    `line rebuild: order equals view_seq asc, got ${s().messages.map((m) => m.text).join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  // 首次 hydrate：seq 1/2/3（已落带历史）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "问2", seq: 3 },
    ],
    3,
  );
  // 实时尾部到达（未落带，无 seq）：一条 assistant 回复 + 一条乐观用户消息
  h({ type: "chunk", text: "答2-实时", source: "web" });
  s().addUserMessage("问3-乐观");
  const before = s().messages.length;
  assert(before === 5, "seq setup: 3 hydrated + 2 live tail");

  // 再次 hydrate（含更新的已落带历史 seq 1-5，覆盖了「答2」已落带版本）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "问2", seq: 3 },
      { role: "assistant", text: "答2", seq: 4 },
    ],
    4,
  );
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "问1|答1|问2|答2-实时|问3-乐观|答2",
    `seq reconcile: displayed rows untouched, unmatched authority appended, got ${texts.join("|")}`,
  );
  // 游标只由权威提交写：内容到哪以屏上为准，不再用于裁量
  assert(s().hydratedSeq === 4, "seq reconcile: hydratedSeq records the snapshot edge");
}

{
  reset();
  const s = useStore.getState;
  h({ type: "user_message", content: "旧问", source: "web", turn_id: "t1", view_seq: 50 } as unknown as Parameters<typeof h>[0]);
  h({ type: "chunk", text: "旧答", turn_id: "t1", source: "web", view_seq: 51 } as unknown as Parameters<typeof h>[0]);
  h({ type: "chunk", text: "尾答", turn_id: "t1", source: "web", view_seq: 102 } as unknown as Parameters<typeof h>[0]);
  assert(
    s().messages.map((m) => m.text).join("|") === "旧问|旧答|尾答",
    "orphan setup: replay rows on screen",
  );
  // 快照（行级窗口裁掉了回合前段）
  s().loadHistory(
    [
      { role: "user", text: "快照问", seq: 100 },
      { role: "assistant", text: "快照答", seq: 101 },
      { role: "assistant", text: "尾答", seq: 102 },
      { role: "assistant", text: "快照尾", seq: 103 },
    ],
    103,
    { epoch: TEST_EPOCH },
  );
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "旧问|旧答|快照问|快照答|尾答|快照尾",
    `orphan reposition: with-seq orphans inserted by seq, not appended at tail, got ${texts.join("|")}`,
  );
  // 顺序自检：全表 seq 单调（孤儿归位后不出现乱序段）
  const seqs = s().messages.filter((m) => m.seq !== undefined).map((m) => m.seq as number);
  for (let i = 1; i < seqs.length; i++) {
    assert(seqs[i] >= seqs[i - 1], `orphan reposition: seq order monotonic, got ${seqs.join(",")}`);
  }
}

// orphan: optimistic stays tail
{
  reset();
  const s = useStore.getState;
  h({ type: "chunk", text: "前段", turn_id: "t1", source: "web", view_seq: 50 } as unknown as Parameters<typeof h>[0]);
  s().addUserMessage("乐观新问"); // 无 seq 乐观泡
  s().loadHistory(
    [
      { role: "user", text: "快照问", seq: 100 },
      { role: "assistant", text: "快照答", seq: 101 },
    ],
    101,
    { epoch: TEST_EPOCH },
  );
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "前段|快照问|快照答|乐观新问",
    `orphan reposition: no-seq optimistic bubble stays at tail, got ${texts.join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  // 已被裁剪过的列表：最旧行从 seq=10 起（1-9 已被 _capMessages 丢掉）
  s().loadHistory(
    [
      { role: "user", text: "问10", seq: 10 },
      { role: "assistant", text: "答10", seq: 11 },
    ],
    11,
  );
  // hydrate 快照窗口覆盖 seq 1-12
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "问5", seq: 5 },
      { role: "user", text: "问10", seq: 10 },
      { role: "assistant", text: "答10", seq: 11 },
      { role: "assistant", text: "答11", seq: 12 },
    ],
    12,
  );
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "问10|答10|答11",
    `seq floor: capped history must not resurrect at tail, got ${texts.join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
  );
  // 用户新发一条（乐观泡，无 seq）：权威 user_message 帧还没到
  s().addUserMessage("新问题");
  // 服务端已在回这个回合：带上一条紧邻游标的 view_seq 的 chunk 先到
  h({ type: "chunk", text: "回答中", source: "web", view_seq: 3 } as unknown as Parameters<typeof h>[0]);
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "问1|答1|新问题|回答中",
    `live anchor: no-seq user bubble must stay above later seq frames, got ${texts.join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  s().addUserMessage("原文", undefined, undefined, "cid-1");
  // 模拟重挂载净化：乐观标记被剥掉（旧判据在这里就失效了）
  s().sanitizeResidentMessages();
  assert(s().messages[0].optimistic !== true, "client-id: optimistic cleared by sanitize");
  h({
    type: "user_message",
    content: "原文（服务端规范化后的版本）",
    source: "web",
    turn_id: "t1",
    client_msg_id: "cid-1",
  } as unknown as Parameters<typeof h>[0]);
  const users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 1, `client-id: exact pairing keeps one bubble, got ${users.length}`);
  assert(
    users[0].text === "原文（服务端规范化后的版本）",
    "client-id: authoritative text applied",
  );
  assert(users[0].clientMsgId === "cid-1", "client-id: kept for later hydrate pairing");
}

// hydrate: client_msg_id pair
{
  reset();
  const s = useStore.getState;
  s().addUserMessage("原文", undefined, undefined, "cid-2");
  s().sanitizeResidentMessages();
  const liveId = s().messages[0].id;
  s().loadHistory(
    [
      { role: "user", text: "原文（服务端规范化后的版本）", seq: 1, client_msg_id: "cid-2" },
      { role: "assistant", text: "收到", seq: 2 },
    ],
    2,
  );
  const users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 1, `hydrate pair: client-id pairing keeps one bubble, got ${users.length}`);
  assert(users[0].text === "原文（服务端规范化后的版本）", "hydrate pair: authoritative text applied");
  assert(users[0].id === liveId, "hydrate pair: live row id reused, no remount");
}

// seq reconcile: mapped + live tail
{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1);
  // 实时 chunk 气泡（autoCreated 无 seq；落带失败的帧才没有 view_seq）
  const LONG = "思考结果正文足够长以参与byKey配对";
  h({ type: "chunk", text: LONG, source: "web" });
  assert(s().messages.length === 2, "dedup setup: 1 hydrated + 1 live chunk");
  // hydrate 带来同文的落带版（seq=2）：实时版应被去重，只剩落带版一条。
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: LONG, seq: 2 },
    ],
    2,
  );
  const assistants = s().messages.filter((m) => m.role === "assistant");
  assert(assistants.length === 1, `dedup: live chunk deduped against persisted copy, got ${assistants.length}`);
  assert(assistants[0].text === LONG && assistants[0].seq === 2, "dedup: persisted copy kept");
}

// diff dedup
{
  reset();
  const s = useStore.getState;
  const sampleDiff = {
    path: "a.py",
    hunks: [{ old_start: 1, old_lines: 1, new_start: 1, new_lines: 1, lines: ["+x"] }],
  };
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1);
  h({ type: "diff", diff_lines: sampleDiff, source: "web" });
  assert(s().messages.length === 2, "diff dedup setup");
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "", diff: sampleDiff, seq: 2 },
    ],
    2,
  );
  assert(s().messages.filter((m) => m.diff).length === 1, "diff dedup: one diff block");
}

// hydrate: keep same-text users
{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "继续", seq: 1 },
      { role: "user", text: "继续", seq: 2 },
    ],
    2,
  );
  assert(
    s().messages.filter((m) => m.role === "user" && m.text === "继续").length === 2,
    "dup text hydrate: both persisted user bubbles kept",
  );
}

// hydrate: never delete displayed
{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "生成图片", seq: 1 }], 1);
  useStore.setState({
    messages: [
      ...s().messages,
      {
        id: "claimed-old",
        role: "user",
        text: "生成图片",
        optimistic: false,
        turn_id: "t-old",
      },
    ],
  });
  const beforeIds = s().messages.map((m) => m.id);
  s().loadHistory(
    [
      { role: "user", text: "生成图片", seq: 1 },
      { role: "assistant", text: "好的", seq: 2 },
    ],
    2,
  );
  const after = s().messages;
  assert(
    after.slice(0, beforeIds.length).map((m) => m.id).join() === beforeIds.join(),
    "hydrate: displayed rows keep their ids and positions",
  );
  assert(
    after.filter((m) => m.role === "user" && m.text === "生成图片").length === 2,
    "hydrate: displayed rows are never deleted (text alone is not an identity)",
  );
  assert(after[after.length - 1].role === "assistant", "hydrate: new authority appended at the tail");
}

// hydrate: keep optimistic
{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "你好", seq: 1 }], 1);
  s().addUserMessage("你好");
  s().loadHistory([{ role: "user", text: "你好", seq: 1 }], 1);
  assert(
    s().messages.filter((m) => m.role === "user").length === 2,
    "optimistic dup text: displayed rows are never deleted by reconcile",
  );
}

// /new: divider
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: "/ws/a", sessionIdByWorkspace: {} });
  s().loadHistory([{ role: "user", text: "A" }, { role: "assistant", text: "B" }], 2, {
    workspaceDir: "/ws/a",
  });
  assert(s().messages.length === 2, "before /new: 2 messages");
  s().resetForNewSession("sid-b");
  assert(s().sessionId === "sid-b", "/new: sessionId annotation updated");
  assert(s().workspaceDir === "/ws/a", "/new: workspace boundary unchanged");
  assert(s().messages.length === 3, "/new: old messages kept + divider");
  assert(s().messages[2].dividerLabel === "新会话", "/new: divider label");
  assert(s().messages[2].role === "assistant", "/new: divider role");
  // 会话键随新段更新（内容一律不入本地）
  assert(s().sessionIdByWorkspace["/ws/a"] === "sid-b", "/new: workspace session key updated");
  // 新消息继续追加在分隔线后
  s().addUserMessage("C");
  assert(s().messages.length === 4, "/new: new message appends after divider");
  assert(s().messages[3].text === "C", "/new: appended message");
}

// ws switch: empty
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: "/ws/a", sessionIdByWorkspace: {} });
  s().loadHistory([{ role: "user", text: "A" }], 1, { workspaceDir: "/ws/a" });
  s().resetForWorkspaceSwitch("/ws/c");
  assert(s().workspaceDir === "/ws/c", "ws switch: workspaceDir updated");
  assert(s().messages.length === 0, "ws switch: target space has its own (empty) line");
}

// ws switch: no cache content
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: "/ws/a", sessionIdByWorkspace: {} });
  s().loadHistory([{ role: "user", text: "A" }], 1, { workspaceDir: "/ws/a" });
  // 先切到 /ws/c 再切回 /ws/a：只记得会话键，内容仍为空、等 hydrate 一次到位
  s().resetForWorkspaceSwitch("/ws/c");
  s().resetForWorkspaceSwitch("/ws/a");
  assert(s().workspaceDir === "/ws/a", "ws switch back: workspaceDir restored");
  assert(s().messages.length === 0, "ws switch back: no cached content shown");
}

// post-/new hydrate
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: "/ws/a", sessionIdByWorkspace: {} });
  s().loadHistory([{ role: "user", text: "A" }, { role: "assistant", text: "B" }], 2, {
    workspaceDir: "/ws/a",
  });
  s().resetForNewSession("sid-b");
  s().addUserMessage("C");
  // 刷新/重连触发权威 hydrate（此处为 C 的落带版）
  s().loadHistory([{ role: "user", text: "C", seq: 10 }], 10, { workspaceDir: "/ws/a" });
  const texts = s().messages.map((m) => m.text);
  assert(texts.includes("A") && texts.includes("B"), "post-/new hydrate: old kept");
  assert(
    s().messages.filter((m) => m.text === "C").length === 1,
    "post-/new hydrate: live tail deduped against mapped",
  );
  assert(s().messages.some((m) => m.dividerLabel === "新会话"), "post-/new hydrate: divider kept");
}

// stale hydrate guard
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: "/ws/a", hydrateGeneration: 1 });
  s().loadHistory([{ role: "user", text: "A" }], 1, { workspaceDir: "/ws/b", generation: 1 });
  assert(s().messages.length === 0, "stale workspace guard");
  s().loadHistory([{ role: "user", text: "A" }], 1, { workspaceDir: "/ws/a", generation: 2 });
  assert(s().messages.length === 0, "stale generation guard");
  s().loadHistory([{ role: "user", text: "A" }], 1, { workspaceDir: "/ws/a", generation: 1 });
  assert(s().messages.length === 1, "matching guard applies");
}

// cli diff gated
{
  reset();
  const s = useStore.getState;
  const sampleDiff = {
    path: "b.py",
    hunks: [{ old_start: 1, old_lines: 1, new_start: 1, new_lines: 1, lines: ["+y"] }],
  };
  h({ type: "diff", diff_lines: sampleDiff, source: "cli" });
  assert(s().messages.length === 0, "cli diff gated");
  h({ type: "diff", diff_lines: sampleDiff, source: "web" });
  assert(s().messages.length === 1, "web diff shown");
}

// beyond-cursor kept
{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
  );
  // 手动注入一条「更新落带」消息（seq=5，超出当前游标 2）
  useStore.setState({
    messages: [
      ...s().messages,
      { id: "x1", role: "user", text: "问-新落带", seq: 5 },
    ],
  });
  // 再次 hydrate 同一窗口（latestSeq 仍为 2）：seq=5 的消息超出游标，保留。
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
  );
  const texts = s().messages.map((m) => m.text);
  assert(
    texts.join("|") === "问1|答1|问-新落带",
    `seq reconcile: beyond-cursor persisted msg kept, got ${texts.join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  // 1: 第一轮正文（一个气泡）
  h({ type: "turn_start", turn_id: "t1", source: "web" });
  h({ type: "chunk", text: "输出1", source: "web" });
  // 2: 用户插入跟话（乐观泡 + 权威帧）
  s().addUserMessage("跟话2");
  h({ type: "user_message", content: "跟话2", source: "web", turn_id: "t1" });
  // 3/4/5: 后续三轮正文——每条 chunk 各一个独立气泡
  h({ type: "chunk", text: "输出3", source: "web" });
  h({ type: "chunk", text: "输出4", source: "web" });
  h({ type: "chunk", text: "输出5", source: "web" });
  const m = s().messages;
  const assistants = m.filter((x) => x.role === "assistant");
  assert(assistants.length === 4, "segment: each chunk is its own bubble");
  assert(assistants[0].text === "输出1", "segment: bubble 1");
  assert(assistants[1].text === "输出3", "segment: bubble 2");
  assert(assistants[2].text === "输出4", "segment: bubble 3");
  assert(assistants[3].text === "输出5", "segment: bubble 4");
  // 顺序：输出1 在 跟话2 之前，跟话2 在 输出3/4/5 之前
  const order = m.map((x) => x.role);
  assert(
    order.join(",") === "assistant,user,assistant,assistant,assistant",
    "segment: chronological order preserved",
  );
  const userIdx = m.findIndex((x) => x.role === "user");
  const firstA = m.findIndex((x) => x.role === "assistant");
  const lastA = m.length - 1 - [...m].reverse().findIndex((x) => x.role === "assistant");
  assert(firstA < userIdx && userIdx < lastA, "segment: user bubble sits between assistant bubbles");
}

{
  reset();
  const s = useStore.getState;
  h({ type: "chunk", text: "cli 输出", source: "cli" });
  h({ type: "chunk", text: "无 source 脏帧" });
  h({ type: "chunk", text: "attach 输出", source: "cli-attached" });
  assert(s().messages.length === 0, "non-web chunk: no bubbles");
  assert(s().turnActive === false, "non-web chunk: spinner stays off");

  h({ type: "state", data: { runtime: { session_id: "s1", status: "idle", provider: "x", model: "y", running: true, turn_source: "cli" } } });
  assert(s().turnActive === false, "heartbeat: cli running must not light web spinner");

  h({ type: "state", data: { runtime: { session_id: "s1", status: "idle", provider: "x", model: "y", running: true, turn_source: "web" } } });
  assert(s().turnActive === true, "heartbeat: web running restores spinner");

  h({ type: "state", data: { runtime: { session_id: "s1", status: "idle", provider: "x", model: "y", running: true, turn_source: "matrix" } } });
  assert(s().turnActive === false, "heartbeat: foreign turn clears wrongly lit spinner");
}

{
  reset();
  const s = useStore.getState;
  h({
    type: "command_result",
    result: {
      output: "已切换 → kimi/k3",
      action: "none",
      data: { provider: "kimi", model: "k3" },
      exit_session: false,
    },
  });
  assert(s().messages.length === 1, "model switch: one bubble");
  assert(s().messages[0].dividerLabel === "已切换模型 kimi·k3", "model switch: divider label");
  assert(s().messages[0].isCommandResult !== true, "model switch: not command card");

  h({
    type: "command_result",
    result: {
      output: "可用模型（当前 kimi/k3）：\n* 1. kimi·k3",
      action: "none",
      data: { current: "kimi/k3", choices: [] },
      exit_session: false,
    },
  });
  assert(s().messages.length === 2, "model list: still a bubble");
  assert(s().messages[1].isCommandResult === true, "model list: keeps command card");
  assert(!s().messages[1].dividerLabel, "model list: no divider");

  h({
    type: "command_result",
    result: {
      output: "已切换到工作空间 demo\n/tmp/demo",
      action: "switch_workspace",
      data: { name: "demo" },
      exit_session: false,
    },
  });
  assert(s().messages[2].dividerLabel === "已切换到工作空间 demo", "ws switch: divider");
}

// silent command
{
  reset();
  const s = useStore.getState;
  useStore.setState({ pendingCommand: "/model" });
  h({
    type: "command_result",
    result: {
      output: "",
      action: "none",
      data: { navigate: "/config?focus=models", open_url: "http://127.0.0.1:8080/config?focus=models" },
      exit_session: false,
    },
  });
  assert(s().messages.length === 0, "silent command: no bubble");
  assert(s().pendingNav === "/config?focus=models", "silent command: navigate still fires");
  assert(s().pendingCommand === null, "silent command: pendingCommand cleared");
}

// compact: clear pending
{
  reset();
  const s = useStore.getState;
  useStore.setState({ pendingCommand: "/compact" });
  s().handleServerMessage({
    type: "command_result",
    result: {
      output: "已压缩（LLM 摘要）：10 → 3 条消息",
      action: "none",
      data: { compressed: true },
      exit_session: false,
    },
  });
  assert(s().pendingCommand === null, "compact: pendingCommand cleared without workspace_dir");
  assert(s().messages.length === 1, "compact: divider row appended");
  assert(s().messages[0].dividerLabel === "已压缩", "compact: silent receipt renders divider");
}

{
  reset();
  const s = useStore.getState;
  useStore.setState({ pendingCommand: "/compact" });
  h({
    type: "command_result",
    result: {
      output: "已压缩（LLM 摘要）：10 → 3 条消息",
      action: "none",
      data: { compressed: true },
      exit_session: false,
    },
    view_seq: 42,
  });
  assert(s().pendingCommand === null, "compact stamped: pending cleared");
  assert(s().messages[0].seq === 42, "compact stamped: view_seq lands on bubble");
}

// llm_switched: no divider
{
  reset();
  const s = useStore.getState;
  s().addUserMessage("帮我改配置");
  h({
    type: "llm_switched",
    provider: "kimi",
    model: "k3",
    origin_source: "cli",
  });
  assert(s().messages.length === 1, "llm_switched: no divider inserted");
  assert(s().messages[0].text === "帮我改配置", "llm_switched: user message untouched");
}
{
  reset();
  const s = useStore.getState;
  s().addUserMessage("/model kimi/k3");
  h({
    type: "command_result",
    result: {
      output: "已切换 → kimi/k3",
      action: "none",
      data: { provider: "kimi", model: "k3" },
      exit_session: false,
    },
  });
  assert(s().messages.length === 2, "cmd divider: appended after user input");
  assert(s().messages[1].dividerLabel === "已切换模型 kimi·k3", "cmd divider: label");
  assert(s().messages[0].text === "/model kimi/k3", "cmd divider: user kept above");
}

// replay: known seq dropped
{
  reset();
  const s = useStore.getState;
  // hydrate 权威已含「问1(1)/答1(2)」
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
  );
  const beforeIds = s().messages.map((m) => m.id).join();
  const beforeLen = s().messages.length;
  // 服务端 replay 帧（replayed=true，带序号）到达：序号已在屏上 ⇒ 全部丢弃
  h({ type: "turn_start", turn_id: "t1", source: "web", replayed: true, view_seq: 3 } as never);
  h({ type: "user_message", content: "问1", source: "web", turn_id: "t1", replayed: true, view_seq: 1 } as never);
  h({ type: "chunk", text: "答1（正文改了也不该再上屏）", source: "web", replayed: true, view_seq: 2 } as never);
  h({ type: "turn_end", turn_id: "t1", reason: "complete", replayed: true, view_seq: 4 } as never);
  assert(s().messages.length === beforeLen, "replay dup: frames with a known seq are dropped");
  assert(s().messages.map((m) => m.id).join() === beforeIds, "replay dup: rows untouched (ids stable)");
  assert(s().messages[0].text === "问1" && s().messages[1].text === "答1", "replay dup: content intact");

  // 进行中回合：旧回合 turn_end replay 不得清 spinner
  reset();
  h({ type: "user_message", content: "问2", source: "web", turn_id: "t2" });
  h({ type: "turn_start", turn_id: "t2", source: "web" });
  h({ type: "chunk", text: "答2-进行中", source: "web" });
  assert(s().turnActive === true, "turn_end replay: active turn lit");
  h({ type: "turn_end", turn_id: "t1", reason: "complete", source: "web", replayed: true });
  assert(s().turnActive === true, "turn_end replay: stale turn_end ignored");

  // 真实新帧（序号不在屏上）照常追加
  const mid = s().messages.length;
  h({ type: "user_message", content: "问3", source: "web", turn_id: "t3", view_seq: 5 } as never);
  h({ type: "chunk", text: "答3", source: "web", view_seq: 6 } as never);
  assert(s().messages.length === mid + 2, "replay dup: live frames still render");
}

{
  reset();
  const s = useStore.getState;
  // 残留瞬态标记的消息（如流式被打断后切走）
  useStore.setState({
    messages: [
      { id: "m1", role: "user", text: "问题", optimistic: true },
      { id: "m2", role: "assistant", text: "回答中", streaming: true },
      { id: "m3", role: "user", text: "跟话", pendingInject: true },
      { id: "m4", role: "assistant", text: "正常" },
    ] as never,
  });
  s().sanitizeResidentMessages();
  const msgs = s().messages;
  assert(msgs[0].optimistic !== true, "sanitize: optimistic stripped");
  assert(msgs[1].streaming !== true, "sanitize: streaming stripped");
  assert(msgs[2].pendingInject !== true, "sanitize: pendingInject stripped");
  assert(msgs[3].text === "正常", "sanitize: clean message untouched");
  // 无瞬态标记时不触发 set（引用不变，避免多余重渲染）
  const before = s().messages;
  s().sanitizeResidentMessages();
  assert(s().messages === before, "sanitize: no-op keeps reference");
}

{
  reset();
  const s = useStore.getState;
  // 实时侧
  const ANS = "答A 正文足够长以参与byKey配对";
  s().addUserMessage("问A");
  h({ type: "chunk", text: ANS, source: "web" });
  const liveUserId = s().messages.find((m) => m.role === "user")!.id;
  const liveAsstId = s().messages.find((m) => m.role === "assistant")!.id;
  // hydrate 权威到达（同内容、带 seq）：应原位复用 id，不是另起新对象追加
  s().loadHistory(
    [
      { role: "user", text: "问A", seq: 1 },
      { role: "assistant", text: ANS, seq: 2 },
    ],
    2,
  );
  const m = s().messages;
  assert(m.length === 2, "reconcile: no duplicate rows");
  assert(m[0].id === liveUserId, "reconcile: user row reuses live id (React key stable)");
  assert(m[1].id === liveAsstId, "reconcile: assistant row reuses live id");
  assert(m[0].seq === 1 && m[1].seq === 2, "reconcile: seq backfilled from authority");
  assert(m[0].optimistic !== true, "reconcile: optimistic stripped by authority overwrite");
}

// reconcile: identical hydrate
{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "老问题", seq: 1 },
      { role: "assistant", text: "老回答", seq: 2 },
    ],
    2,
  );
  const firstIds = s().messages.map((x) => x.id);
  // 第二次 hydrate 内容不变：同 seq 行必须复用第一次的 id（引用可比对）
  s().loadHistory(
    [
      { role: "user", text: "老问题", seq: 1 },
      { role: "assistant", text: "老回答", seq: 2 },
    ],
    2,
  );
  const secondIds = s().messages.map((x) => x.id);
  assert(
    firstIds.join() === secondIds.join(),
    "reconcile: identical hydrate reuses ids (no re-render storm)",
  );
}

// reconcile: keep displayed dups
{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "继续", seq: 1 },
      { role: "user", text: "继续", seq: 2 },
    ],
    2,
  );
  const firstIds = s().messages.map((x) => x.id);
  // 实时又来一条同文（未落带，如断线重发的乐观泡）
  s().addUserMessage("继续");
  s().loadHistory(
    [
      { role: "user", text: "继续", seq: 1 },
      { role: "user", text: "继续", seq: 2 },
    ],
    2,
  );
  const dups = s().messages.filter((m) => m.role === "user" && m.text === "继续");
  assert(dups.length === 3, `reconcile: displayed dup-text rows kept, got ${dups.length}`);
  assert(
    s().messages.slice(0, firstIds.length).map((x) => x.id).join() === firstIds.join(),
    "reconcile: persisted rows reuse ids in place",
  );
  // 下一条 hydrate 把它带回来（seq 3）：命中未落带的那一条（内容 key 相同）就地接管
  s().loadHistory(
    [
      { role: "user", text: "继续", seq: 1 },
      { role: "user", text: "继续", seq: 2 },
      { role: "user", text: "继续", seq: 3 },
    ],
    3,
  );
  assert(
    s().messages.filter((m) => m.role === "user" && m.text === "继续").length === 3 &&
      s().messages.length === 3,
    `reconcile: third dup claimed in place (no new row), got ${s().messages.length} rows`,
  );
}

// reconcile: keep live tail
{
  reset();
  const s = useStore.getState;
  useStore.setState({
    messages: [{ id: "live", role: "user", text: "真实跟话" }] as never,
  });
  s().loadHistory([{ role: "user", text: "落带提问", seq: 1 }], 1);
  const texts = s().messages.map((m) => m.text);
  assert(texts.includes("真实跟话"), "reconcile: live tail kept");
  assert(texts.includes("落带提问"), "reconcile: authoritative row present");
}

// llm_switched: never
{
  reset();
  const s = useStore.getState;
  h({ type: "llm_switched", origin_source: "cli", provider: "kimi", model: "k3" });
  assert(s().messages.length === 0, "llm_switched: no divider, local or foreign");
}

{
  reset();
  const s = useStore.getState;
  // 刷新头几百毫秒：边界未定
  useStore.setState({ workspaceDir: null, sessionId: null, messages: [], viewReady: false });
  hBound({ type: "user_message", content: "边界外的提问", source: "web", turn_id: "b1" });
  hBound({ type: "turn_start", turn_id: "b1", source: "web" });
  hBound({ type: "chunk", text: "边界外的回答", source: "web", turn_id: "b1" });
  assert(s().messages.length === 0, "buffer: content frames held while boundary unknown");
  // state 帧把边界落定 → 缓冲按到达序回放，一条不丢，守卫与去重照旧生效
  hBound({
    type: "state",
    data: {
      runtime: { session_id: TEST_SID, workspace_dir: TEST_DIR, running: true, turn_source: "web" },
    },
  });
  assert(s().workspaceDir === TEST_DIR, "buffer: boundary landed from state frame");
  const replayed = s().messages.map((m) => m.text).join("|");
  assert(replayed === "边界外的提问|边界外的回答", `buffer: replay in arrival order, got ${replayed}`);
  assert(s().turnActive === true && s().currentTurnId === "b1", "buffer: replayed turn state applied");
  // 边界已定后再来的帧照常直通（不再入缓冲）
  hBound({ type: "chunk", text: "直通回答", source: "web", turn_id: "b1" });
  assert(s().messages.length === 3, "buffer: later frames pass straight through");
}

// buffer: overflow 300
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: null, sessionId: null, messages: [], viewReady: false });
  const originalWarn = console.warn;
  let warned = 0;
  console.warn = () => {
    warned += 1;
  };
  for (let i = 0; i < 305; i++) hBound({ type: "chunk", text: `c${i}`, source: "web" });
  console.warn = originalWarn;
  assert(warned === 1, `buffer overflow: warned once, got ${warned}`);
  hBound({
    type: "state",
    data: { runtime: { session_id: TEST_SID, workspace_dir: TEST_DIR } },
  });
  assert(s().messages.length === 300, `buffer overflow: bounded at 300, got ${s().messages.length}`);
  assert(s().messages[0].text === "c5", `buffer overflow: oldest dropped, first=${s().messages[0].text}`);
}

{
  reset();
  const s = useStore.getState;
  const seen: { msgCount: number; turnActive: boolean; sessionId: string | null; viewReady: boolean }[] = [];
  const unsub = useStore.subscribe((st) => {
    seen.push({
      msgCount: st.messages.length,
      turnActive: st.turnActive,
      sessionId: st.sessionId,
      viewReady: st.viewReady,
    });
  });
  s().loadHistory(
    [
      { role: "user", text: "快照提问", seq: 1 },
      { role: "assistant", text: "快照回答", seq: 2 },
    ],
    2,
    { runtime: { session_id: "sid-rt", running: true, turn_source: "web" } },
  );
  unsub();
  const withContent = seen.filter((x) => x.msgCount > 0);
  assert(withContent.length >= 1, "atomic: snapshot commit observed");
  assert(
    withContent.every((x) => x.turnActive === true && x.sessionId === "sid-rt"),
    "atomic: runtime committed together with messages (no two-step render)",
  );
  assert(withContent.every((x) => x.viewReady === true), "atomic: viewReady lands in the same commit");
  assert(s().turnActive === true && s().sessionId === "sid-rt", "atomic: final state");
  assert(s().currentTurnId === null, "atomic: 快照不臆造 turn_id");
  // 快照说非 web 回合在跑：不亮 web spinner（口径与实时帧一致）
  s().loadHistory([{ role: "user", text: "快照提问", seq: 1 }], 1, {
    runtime: { session_id: "sid-rt", running: true, turn_source: "cli" },
  });
  assert(s().turnActive === false, "atomic: non-web turn_source keeps web spinner off");
}

{
  reset();
  const s = useStore.getState;
  assert(s().viewReady === false, "viewReady: false before authority lands");
  s().loadHistory([{ role: "user", text: "甲", seq: 1 }], 1);
  assert(s().viewReady === true, "viewReady: true once a snapshot commits");
  s().resetForWorkspaceSwitch("/ws/other");
  assert(
    s().viewReady === false && s().messages.length === 0,
    "viewReady: back to skeleton on workspace switch",
  );
  s().loadHistory([{ role: "assistant", text: "乙", seq: 1 }], 1, { workspaceDir: "/ws/other" });
  assert(s().viewReady === true, "viewReady: true after target space's snapshot");
  s().resetForNewSession("sid-new");
  assert(s().viewReady === false, "viewReady: back to skeleton on new session segment");
  assert(s().turnStartedAt === null, "/new: turnStartedAt cleared (no stale elapsed time)");
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "派两个子智能体", seq: 1 }], 1);
  const before = s().messages.length;
  useStore.setState({ subagentOutput: { "call-fold": { text: "中间产出", result: "最终结论" } } });
  hBound({
    type: "continuation_input_injected",
    user_texts: ["接续一句"],
    subagent_texts: ["[sa-coaras-1] 子智能体结论"],
    subagent_sources: ["web"],
  });
  assert(s().messages.length === before, "subagent result: no chat bubble appended");
  assert(
    s().subagentOutput["call-fold"]?.result === "最终结论",
    "subagent result: tool-line folding output untouched",
  );
  // 折叠区数据来源仍是 subagent_result 帧
  h({ type: "subagent_result", tool_call_id: "call-fold", text: "最终结论", source: "web" });
  assert(
    s().subagentOutput["call-fold"]?.result === "最终结论",
    "subagent_result still feeds the delegate tool line folding",
  );
}

{
  reset();
  const s = useStore.getState;
  // 实时已攒下的中间产出（只有实时帧这一条来源）
  useStore.setState({ subagentOutput: { c1: { text: "中间产出", result: "" } } });
  const seen: { msgCount: number; out: Record<string, { text: string; result: string }> }[] = [];
  const unsub = useStore.subscribe((st) => {
    seen.push({ msgCount: st.messages.length, out: st.subagentOutput });
  });
  s().loadHistory(
    [
      { role: "user", text: "派了子智能体", seq: 1 },
      { role: "assistant", text: "都回来了", seq: 2 },
    ],
    2,
    { subagentResults: { c1: "子智能体一的最终答复", c2: "子智能体二的最终答复", c3: "   " } },
  );
  unsub();
  assert(
    s().subagentOutput.c1.result === "子智能体一的最终答复",
    "snapshot result: non-empty value wins",
  );
  assert(
    s().subagentOutput.c1.text === "中间产出",
    "snapshot result: live mid-output kept",
  );
  assert(
    s().subagentOutput.c2?.result === "子智能体二的最终答复" && s().subagentOutput.c2?.text === "",
    "snapshot result: missing call created as {text:'', result}",
  );
  assert(s().subagentOutput.c3 === undefined, "snapshot result: blank value creates nothing");
  const texts = s().messages.map((m) => m.text).join("|");
  assert(!texts.includes("最终答复"), `subagent results must not become messages, got ${texts}`);
  const withContent = seen.filter((x) => x.msgCount > 0);
  assert(
    withContent.length >= 1 && withContent.every((x) => x.out.c1?.result === "子智能体一的最终答复"),
    "atomic: subagent results land in the same commit as messages",
  );
  // 快照空值不得抹掉实时已有的最终答复（空≠权威）
  s().loadHistory([{ role: "assistant", text: "都回来了", seq: 2 }], 2, {
    subagentResults: { c1: "" },
  });
  assert(
    s().subagentOutput.c1.result === "子智能体一的最终答复",
    "snapshot result: empty value never wipes the live result",
  );
}

{
  reset();
  const s = useStore.getState;
  hBound({
    type: "subagent_chunk",
    tool_call_id: "call-x",
    text: "第一段",
    source: "web",
    turn_id: "t1",
    seq: 3,
  });
  assert(s().subagentOutput["call-x"]?.text === "第一段", "chunk watermark: first piece accumulated");
  // 同一帧（同 turn、同 seq）被重连回放：不得再拼一遍——B 修的就是这条重复累加
  hBound({
    type: "subagent_chunk",
    tool_call_id: "call-x",
    text: "第一段",
    source: "web",
    turn_id: "t1",
    seq: 3,
    replayed: true,
  });
  assert(
    s().subagentOutput["call-x"]?.text === "第一段",
    `chunk watermark: replay deduped, got ${s().subagentOutput["call-x"]?.text}`,
  );
  // 丢帧窗口内的新帧（seq 更大、带 replayed）：必须放行——只用 replayed 判会把新帧一起丢
  hBound({
    type: "subagent_chunk",
    tool_call_id: "call-x",
    text: "第二段",
    source: "web",
    turn_id: "t1",
    seq: 4,
    replayed: true,
  });
  assert(
    s().subagentOutput["call-x"]?.text === "第一段第二段",
    `chunk watermark: new frame passes even when replayed, got ${s().subagentOutput["call-x"]?.text}`,
  );
  // 新回合（turn_id 变）：水位重开，seq 从小开始也照收
  hBound({
    type: "subagent_chunk",
    tool_call_id: "call-x",
    text: "第三段",
    source: "web",
    turn_id: "t2",
    seq: 1,
  });
  assert(
    s().subagentOutput["call-x"]?.text === "第一段第二段第三段",
    `chunk watermark: new turn resets the watermark, got ${s().subagentOutput["call-x"]?.text}`,
  );
  // 老服务端帧上没有序号：带 replayed 丢弃，不带则放行
  hBound({ type: "subagent_chunk", tool_call_id: "call-y", text: "老帧", source: "web", replayed: true });
  assert(s().subagentOutput["call-y"] === undefined, "chunk watermark: seq-less replayed frame dropped");
  hBound({ type: "subagent_chunk", tool_call_id: "call-y", text: "老帧", source: "web" });
  assert(s().subagentOutput["call-y"]?.text === "老帧", "chunk watermark: seq-less live frame kept");
  // 边界未定期的过程帧：入缓冲 → 回放后仍在（不按 seq 裁剪）
  useStore.setState({ workspaceDir: null, sessionId: null, subagentOutput: {} });
  hBound({
    type: "subagent_chunk",
    tool_call_id: "call-z",
    text: "边界外的过程帧",
    source: "web",
    turn_id: "t9",
    seq: 7,
  });
  hBound({ type: "state", data: { runtime: { session_id: TEST_SID, workspace_dir: TEST_DIR } } });
  assert(
    s().subagentOutput["call-z"]?.text === "边界外的过程帧",
    "chunk watermark: replayed from the boundary buffer, not dropped",
  );
}

// overflow: keep process frames
{
  reset();
  const s = useStore.getState;
  useStore.setState({ workspaceDir: null, sessionId: null, messages: [], subagentOutput: {}, viewReady: false });
  const originalWarn = console.warn;
  console.warn = () => {};
  for (let i = 0; i < 4; i++) {
    hBound({ type: "subagent_chunk", tool_call_id: "keep", text: `p${i}`, source: "web" });
  }
  for (let i = 0; i < 302; i++) hBound({ type: "chunk", text: `c${i}`, source: "web" });
  console.warn = originalWarn;
  hBound({ type: "state", data: { runtime: { session_id: TEST_SID, workspace_dir: TEST_DIR } } });
  assert(
    s().subagentOutput["keep"]?.text === "p0p1p2p3",
    `overflow: process frames kept, got ${s().subagentOutput["keep"]?.text}`,
  );
  assert(
    s().messages.length === 296,
    `overflow: droppable content dropped first, got ${s().messages.length}`,
  );
  assert(
    s().messages[0].text === "c6",
    `overflow: oldest content dropped first, first=${s().messages[0].text}`,
  );
}

{
  reset();
  const s = useStore.getState;
  const sampleDiff = {
    path: "a.py",
    added: 1,
    removed: 0,
    hunks: [[{ kind: "add", oldNum: 0, newNum: 1, code: "+x" }]],
  };
  hBound({
    type: "tool",
    text: "read(D:\\code_ws\\v8\\a.py)",
    ok: true,
    tool_name: "read",
    tool_call_id: "inner-1",
    duration_ms: 12,
    source: "web",
    parent_tool_call_id: "call-d1",
    view_seq: 7,
  });
  hBound({
    type: "diff",
    diff_lines: sampleDiff,
    source: "web",
    parent_tool_call_id: "call-d1",
    view_seq: 8,
  });
  assert(s().messages.length === 0, "subagent frames: never appended to the main stream");
  const folded = s().subagentDiffs["call-d1"] ?? [];
  assert(folded.length === 2, `subagent frames: folded under the parent call, got ${folded.length}`);
  assert(
    folded[0].tool?.label.startsWith("read(") === true && folded[1].diff === sampleDiff,
    "subagent frames: tool row then diff block, in view_seq order",
  );
  // 断连回放/快照叠加：同一帧再来一次，按 view_seq 去重
  hBound({
    type: "tool",
    text: "read(D:\\code_ws\\v8\\a.py)",
    ok: true,
    tool_call_id: "inner-1",
    source: "web",
    parent_tool_call_id: "call-d1",
    view_seq: 7,
  });
  assert((s().subagentDiffs["call-d1"] ?? []).length === 2, "subagent frames: deduped by view_seq");
  // 不带 parent_tool_call_id 的工具行照旧进正文流（不能连别的工具行一起折走）
  h({ type: "tool", text: "read(b.py)", ok: true, source: "web" });
  assert(s().messages.length === 1 && s().messages[0].tool?.label === "read(b.py)", "ordinary tool rows stay in the stream");
}

{
  reset();
  const s = useStore.getState;
  const seen: { msgs: number; folded: number }[] = [];
  const unsub = useStore.subscribe((st) => {
    seen.push({ msgs: st.messages.length, folded: st.subagentDiffs["call-d2"]?.length ?? 0 });
  });
  s().loadHistory([{ role: "user", text: "派了子智能体", seq: 1 }], 9, {
    epoch: TEST_EPOCH,
    subagentResults: { "call-d2": "最终答复" },
    subagentDiffs: {
      "call-d2": [
        { type: "tool", text: "grep(x)", ok: true, tool_call_id: "inner-2", view_seq: 3 },
        {
          type: "diff",
          diff_lines: { path: "b.py", added: 1, removed: 1, hunks: [[{ kind: "add", oldNum: 0, newNum: 1, code: "+y" }]] },
          view_seq: 4,
        },
      ],
    },
  });
  unsub();
  const folded = s().subagentDiffs["call-d2"] ?? [];
  assert(folded.length === 2, `snapshot: folded frames merged, got ${folded.length}`);
  assert(folded[0].seq === 3 && folded[1].seq === 4, "snapshot: folded frames sorted by view_seq");
  assert(s().subagentOutput["call-d2"]?.result === "最终答复", "snapshot: result mapped into the folding output");
  const texts = s().messages.map((m) => m.text).join("|");
  assert(!texts.includes("最终答复") && !texts.includes("grep"), `snapshot: folding content stays out of messages, got ${texts}`);
  const withContent = seen.filter((x) => x.msgs > 0);
  assert(
    withContent.length >= 1 && withContent.every((x) => x.folded === 2),
    "snapshot: folded frames land in the same commit as messages",
  );
  assert(s().lineEpoch === TEST_EPOCH && s().hydratedSeq === 9, "snapshot: epoch + cursor committed");
}

{
  reset();
  const s = useStore.getState;
  useStore.setState({
    workspaceDir: "/ws/old",
    sessionId: "sid-old",
    viewReady: true,
    hydratedSeq: 12,
    lineEpoch: "old-epoch",
    subagentDiffs: { "call-old": [] },
  });
  s().resetForWorkspaceSwitch("/ws/new", {
    messages: [
      { role: "user", text: "新空间提问", seq: 3 },
      { role: "assistant", text: "新空间回答", seq: 4 },
    ],
    latest_seq: 9,
    epoch: "new-epoch",
    session_id: "sid-new",
    workspace_dir: "/ws/new",
    subagent_results: { "call-new": "新空间的最终答复" },
    subagent_diffs: { "call-new": [{ type: "tool", text: "read(z.py)", ok: true, view_seq: 5 }] },
    runtime: { session_id: "sid-new", running: true, turn_source: "web", workspace_dir: "/ws/new" },
  });
  assert(s().workspaceDir === "/ws/new" && s().sessionId === "sid-new", "switch snapshot: boundary + session landed");
  assert(
    s().messages.map((m) => m.text).join("|") === "新空间提问|新空间回答",
    "switch snapshot: content committed in the same round trip",
  );
  assert(
    s().viewReady === true && s().lineEpoch === "new-epoch" && s().hydratedSeq === 9,
    "switch snapshot: skeleton off + line identity + cursor in one commit",
  );
  assert(s().subagentOutput["call-new"]?.result === "新空间的最终答复", "switch snapshot: folded results mapped");
  assert((s().subagentDiffs["call-new"] ?? []).length === 1, "switch snapshot: folded frames mapped");
  assert(s().subagentDiffs["call-old"] === undefined, "switch snapshot: previous line folding cleared");
  assert(s().turnActive === true, "switch snapshot: runtime applied (web turn lights the spinner)");
  // 旧服务端/异常态：没有 snapshot 就退回原流程
  s().resetForWorkspaceSwitch("/ws/legacy");
  assert(
    s().messages.length === 0 && s().viewReady === false && s().lineEpoch === null,
    "switch without snapshot: fallback to skeleton + hydrate",
  );
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答2", seq: 2 },
    ],
    5,
    { epoch: TEST_EPOCH },
  );
  assert(s().lineEpoch === TEST_EPOCH && s().hydratedSeq === 5, "seq setup: snapshot edge recorded");
  // 服务端 replay 与快照叠加的那一窗：序号 2 已在屏上 ⇒ 整帧丢弃
  hBound({ type: "chunk", text: "重放的第2帧", source: "web", view_seq: 2, turn_id: "t1" });
  assert(s().messages.length === 2 && s().hydratedSeq === 5, "seq: known-seq frame dropped");
  // 序号不在屏上：照常追加（与游标之间隔着空洞也照收）
  hBound({ type: "chunk", text: "第6帧", source: "web", view_seq: 6, turn_id: "t1" });
  assert(
    s().messages.map((m) => m.text).join("|") === "问1|答2|第6帧",
    "seq: unknown-seq frame appended",
  );
  assert(s().hydratedSeq === 5, "seq: 游标只由权威提交写，不再随帧推进");
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1, { epoch: TEST_EPOCH });
  assert(s().viewReady === true && s().lineEpoch === TEST_EPOCH, "gap setup: line settled at seq 1");
  hBound({ type: "chunk", text: "跳到第4帧的回答", source: "web", view_seq: 4, turn_id: "t9" });
  assert(
    s().messages.map((m) => m.text).join("|") === "问1|跳到第4帧的回答",
    `gap: frame applied immediately (never buffered), got ${s().messages.map((m) => m.text).join("|")}`,
  );
  assert(s().hydratedSeq === 1, "gap: 没有补齐、没有游标推进");
}

{
  reset();
  const s = useStore.getState;
  const longBrief = "<任务指令>\n目标：把 A 做完\n验收：跑通测试";
  hBound({
    type: "user_message",
    content: longBrief,
    source: "web",
    delegate_brief: true,
    parent_tool_call_id: "call-b1",
    turn_id: "t-b1",
  });
  assert(s().messages.length === 0, "brief: never appended to the message stream");
  assert(
    s().subagentBriefs["call-b1"] === longBrief,
    "brief: folded under the parent call, newlines kept verbatim",
  );
  // 老数据兜底：只靠「以 <任务指令> 开头」判
  hBound({
    type: "user_message",
    content: "<任务指令> 老格式的指令",
    source: "web",
    parent_tool_call_id: "call-b2",
  });
  assert(
    s().subagentBriefs["call-b2"] === "<任务指令> 老格式的指令" && s().messages.length === 0,
    "brief: legacy text heuristic",
  );
  // 同 call 取最新
  hBound({
    type: "user_message",
    content: "第二条指令",
    source: "web",
    delegate_brief: true,
    parent_tool_call_id: "call-b1",
  });
  assert(s().subagentBriefs["call-b1"] === "第二条指令", "brief: same call keeps the latest");
  // 真·用户消息照旧进正文流（不能被 brief 判据误伤）
  h({ type: "user_message", content: "这是一条真正的用户消息", source: "web", turn_id: "t-real" });
  assert(
    s().messages.length === 1 && s().messages[0].text === "这是一条真正的用户消息",
    "brief: real user messages stay in the stream",
  );
  // 指令不混进产出/答复那一份（两者语义不同、渲染面不同）
  assert(s().subagentOutput["call-b1"] === undefined, "brief: not mixed into subagentOutput");
}

{
  reset();
  const s = useStore.getState;
  const seen: { msgs: number; briefs: number }[] = [];
  const unsub = useStore.subscribe((st) => {
    seen.push({ msgs: st.messages.length, briefs: Object.keys(st.subagentBriefs).length });
  });
  s().loadHistory([{ role: "user", text: "派活", seq: 1 }], 4, {
    epoch: TEST_EPOCH,
    subagentBriefs: { "call-c1": "<任务指令> 快照里的指令", "call-c2": "   " },
  });
  unsub();
  assert(s().subagentBriefs["call-c1"] === "<任务指令> 快照里的指令", "snapshot briefs: merged");
  assert(s().subagentBriefs["call-c2"] === undefined, "snapshot briefs: blank value creates nothing");
  assert(!s().messages.some((m) => m.text.includes("快照里的指令")), "snapshot briefs: never part of messages");
  const withContent = seen.filter((x) => x.msgs > 0);
  assert(
    withContent.length >= 1 && withContent.every((x) => x.briefs === 1),
    "snapshot briefs: land in the same commit as messages",
  );
  // 空值不覆盖已有的实时指令（空 ≠ 权威）
  s().loadHistory([{ role: "user", text: "派活", seq: 1 }], 4, { subagentBriefs: { "call-c1": "" } });
  assert(
    s().subagentBriefs["call-c1"] === "<任务指令> 快照里的指令",
    "snapshot briefs: empty value never wipes the live brief",
  );
}

{
  const treeRow = (nodeId: string, label: string, depth = 1, active = false): Parameters<typeof buildToolLineGroups>[0]["work"][number] => ({
    nodeId,
    depth,
    label,
    kind: "tool",
    active,
    isError: false,
    pending: false,
    startedAt: 0,
  });
  const groups = buildToolLineGroups({
    brief: "<任务指令>\n目标：A",
    work: [treeRow("w1", "read(x)")],
    frames: [{ id: "f1", role: "assistant", text: "", tool: { label: "grep(y)", ok: true } }],
    body: "产出正文",
    result: "最终答复",
  });
  const order = groups.map((g) => g.id).join("|");
  assert(order === "brief|process|result", `accordion: fixed group order (三组), got ${order}`);
  // 组名与文档（docs/消息渲染契约.md §五 折叠区契约）
  const titles = groups.map((g) => g.title).join("|");
  assert(
    titles === "任务指令|过程|最终结果",
    `accordion: group titles match the docs, got ${titles}`,
  );
  const byId = new Map(groups.map((g) => [g.id, g]));
  assert(byId.get("brief")?.defaultOpen === false, "accordion: 任务指令默认收起");
  assert(
    ["process", "result"].every((id) => byId.get(id)?.defaultOpen === true),
    "accordion: the other groups default to expanded",
  );
  // 组头不挂规模提示（09-26 排版重设计：行尾数字是噪音，项数/字数一律去掉）
  assert(
    groups.every((g) => g.hint === ""),
    `accordion: 组头不带项数/字数提示, got ${groups.map((g) => g.hint).join(",")}`,
  );
  // 过程组内容：① 落带帧在前 → ② 活动树补给行在后（树行 call 不在帧集合里）
  const entries = byId.get("process")?.entries ?? [];
  assert(
    entries.length === 2 && entries[0].frame !== undefined && entries[1].row?.nodeId === "w1",
    `accordion: 帧在前、树行补给在后, got ${entries.map((e) => (e.frame ? "frame" : e.row?.nodeId)).join(",")}`,
  );
  // 过程正文挂在 process 组内（没有「工作行」「过程输出」这些独立组）
  assert(byId.get("process")?.text === "产出正文", "accordion: 过程正文就在 process 组里");
  assert(byId.get("result")?.text === "最终答复", "accordion: 最终结果是独立的一组");
  assert(
    !groups.some((g) => g.title === "工作行" || g.title === "过程输出" || g.id === ("body" as never)),
    "accordion: 没有工作行/过程输出这些独立组",
  );
  // 同一 tool_call_id 同时有树行与帧：过程组里只出现一次（以帧为准）
  const dedup = buildToolLineGroups({
    brief: "",
    work: [treeRow("call-1", "read(a.py)")],
    frames: [
      { id: "f9", role: "assistant", text: "", tool: { label: "read(a.py)", ok: true, tool_call_id: "call-1" } },
    ],
    body: "",
    result: "",
  });
  const dedupEntries = dedup[0]?.entries ?? [];
  assert(
    dedup.length === 1 && dedup[0].id === "process" && dedupEntries.length === 1 && dedupEntries[0].frame !== undefined,
    `accordion: 同 call 的树行不重复出现、以帧为准, got ${dedupEntries.length} 条`,
  );
  assert(dedup[0].hint === "", `accordion: 去重后组头不带计数, got ${dedup[0].hint}`);
  // 帧还没到的在跑工具：由树行补进过程组（行首仍显示 ◌）
  const pendingOnly = buildToolLineGroups({
    brief: "",
    work: [treeRow("call-2", "shell(build)", 1, true)],
    frames: [],
    body: "",
    result: "",
  });
  assert(
    pendingOnly.length === 1 && pendingOnly[0].entries?.[0].row?.nodeId === "call-2",
    "accordion: 帧未到的在跑工具由树行补进过程组",
  );
  assert(pendingOnly[0].entries?.[0].row?.active === true, "accordion: 树行保留运行态（行首 ◌）");
  // 空组不出现：全空＝一组都没有＝不可展开（与旧面板「全空不可展开」同一判据）
  const empty = buildToolLineGroups({ brief: "   ", work: [], frames: [], body: " ", result: "" });
  assert(empty.length === 0, "accordion: empty groups omitted ⇒ row not expandable");
  // 只有指令：仍可展开，且默认是收起的那一组
  const onlyBrief = buildToolLineGroups({ brief: "指令", work: [], frames: [], body: "", result: "" });
  assert(
    onlyBrief.length === 1 && onlyBrief[0].id === "brief" && onlyBrief[0].defaultOpen === false,
    "accordion: brief-only row is expandable with the brief collapsed",
  );
  // 只有过程正文（没有帧也没有树行）：过程组照样出现（判据是「三者任一」）
  const bodyOnly = buildToolLineGroups({ brief: "", work: [], frames: [], body: "只有过程正文", result: "" });
  assert(
    bodyOnly.length === 1 && bodyOnly[0].id === "process" && bodyOnly[0].text === "只有过程正文",
    `accordion: 只有过程正文时过程组仍出现, got ${bodyOnly.map((g) => g.hint).join(",")}`,
  );
  // 长指令同样只留标题（组头不再带字数，长文本不会撑开标题栏）
  const longBrief = buildToolLineGroups({ brief: "x".repeat(1234), work: [], frames: [], body: "", result: "" });
  assert(longBrief[0].hint === "", `accordion: 长文本组头不带字数, got ${longBrief[0].hint}`);
}

{
  reset();
  const s = useStore.getState;
  hBound({ type: "chunk", text: "答5", source: "web", turn_id: "t1", view_seq: 5 });
  hBound({ type: "chunk", text: "答4", source: "web", turn_id: "t1", view_seq: 4 });
  hBound({ type: "chunk", text: "答3", source: "web", turn_id: "t1", view_seq: 3 });
  assert(
    s().messages.map((m) => m.text).join("|") === "答5|答4|答3",
    `order: late frames append (displayed rows never move), got ${s().messages.map((m) => m.text).join("|")}`,
  );
  assert(
    s().messages.map((m) => m.seq).join(",") === "5,4,3",
    "order: rows carry their view_seq (the rebuild has the key to re-sort)",
  );
}

{
  reset();
  const s = useStore.getState;
  // 已渲染内容：seq 10
  hBound({ type: "chunk", text: "答10", source: "web", turn_id: "t1", view_seq: 10 });
  assert(s().messages.length === 1, "buffer replay setup: one rendered row");
  // 边界未定（刷新窗口）：seq 8 迟到，只能先入缓冲
  useStore.setState({ workspaceDir: null, sessionId: null });
  hBound({ type: "chunk", text: "答8", source: "web", turn_id: "t1", view_seq: 8 });
  assert(s().messages.length === 1, "buffer replay: frame held while the boundary is unknown");
  // 边界落定 → 回放：追加在尾部（已显示的答10 一动不动）
  hBound({
    type: "state",
    data: { runtime: { session_id: TEST_SID, workspace_dir: TEST_DIR } },
  });
  assert(
    s().messages.map((m) => m.text).join("|") === "答10|答8",
    `buffer replay: replayed frame appended, got ${s().messages.map((m) => m.text).join("|")}`,
  );
}

{
  reset();
  const s = useStore.getState;
  hBound({ type: "chunk", text: "答1", source: "web", turn_id: "t1", view_seq: 1 });
  hBound({ type: "chunk", text: "答3", source: "web", turn_id: "t1", view_seq: 3 });
  // 乱序到达的折叠帧：指令（seq 2）与子智能体工具行（seq 4）——只进折叠区
  hBound({
    type: "user_message",
    content: "<任务指令> 去做 A",
    source: "web",
    delegate_brief: true,
    parent_tool_call_id: "call-o1",
    view_seq: 2,
  });
  hBound({
    type: "tool",
    text: "read(a.py)",
    ok: true,
    source: "web",
    parent_tool_call_id: "call-o1",
    view_seq: 4,
  });
  assert(
    s().messages.map((m) => m.text).join("|") === "答1|答3",
    `folding frames: never enter the message stream (order untouched), got ${s().messages.map((m) => m.text).join("|")}`,
  );
  assert(s().subagentBriefs["call-o1"] === "<任务指令> 去做 A", "folding frames: brief folded");
  assert((s().subagentDiffs["call-o1"] ?? []).length === 1, "folding frames: tool row folded");
  // 迟到的正文帧（seq 2）按到达序追加在尾部（不插回中间——已显示行不动）
  hBound({ type: "chunk", text: "答2", source: "web", turn_id: "t1", view_seq: 2 });
  assert(
    s().messages.map((m) => m.text).join("|") === "答1|答3|答2",
    `late chunk appends at the tail, got ${s().messages.map((m) => m.text).join("|")}`,
  );
}

{
  const rows = [
    { nodeId: "call-d", depth: 0, label: "delegate coaras: 摸底", kind: "delegate", active: false, isError: false, pending: false, startedAt: 0 },
    { nodeId: "sa-1", depth: 1, label: "coaras(摸底)", kind: "subagent", active: false, isError: false, pending: false, startedAt: 0 },
    { nodeId: "t-1", depth: 2, label: "read(a.py)", kind: "read", active: false, isError: false, pending: false, startedAt: 0 },
  ];
  const work = workRowsOf(rows, "call-d");
  assert(work.length === 1 && work[0].nodeId === "t-1", `work rows: only descendants' tool rows, got ${work.map((r) => r.nodeId).join(",")}`);
  // 嵌套子智能体（子智能体的子智能体）同样只留工具行
  const nested = [
    { nodeId: "call-d", depth: 0, label: "delegate coaras: A", kind: "delegate", active: false, isError: false, pending: false, startedAt: 0 },
    { nodeId: "sa-1", depth: 1, label: "coaras(A)", kind: "subagent", active: false, isError: false, pending: false, startedAt: 0 },
    { nodeId: "sa-2", depth: 2, label: "aide(B)", kind: "subagent", active: false, isError: false, pending: false, startedAt: 0 },
    { nodeId: "t-9", depth: 3, label: "grep(x)", kind: "grep", active: false, isError: false, pending: false, startedAt: 0 },
  ];
  const nestedWork = workRowsOf(nested, "call-d");
  assert(
    nestedWork.map((r) => r.nodeId).join(",") === "sa-2,t-9",
    `work rows: deeper subagent rows kept (flattened), got ${nestedWork.map((r) => r.nodeId).join(",")}`,
  );
  // 没有该 root 时按空处理（不抛）
  assert(workRowsOf(rows, "missing").length === 0, "work rows: unknown root → empty");
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
    { epoch: TEST_EPOCH },
  );
  s().addUserMessage("新问题");
  const optimistic = s().messages[s().messages.length - 1];
  assert(
    optimistic.text === "新问题" && optimistic.optimistic === true,
    "no-jump: optimistic bubble sits at the tail",
  );
  const startIndex = s().messages.length - 1;
  // 认领前后每次提交都记下这一行的下标：任何一次都不允许它挪窝
  const moves: number[] = [];
  const unsub = useStore.subscribe((st) => {
    const idx = st.messages.findIndex((m) => m.id === optimistic.id);
    if (idx >= 0) moves.push(idx);
  });
  hBound({
    type: "user_message",
    content: "新问题",
    source: "web",
    turn_id: "t9",
    view_seq: 3,
  });
  unsub();
  const after = s().messages;
  const idx = after.findIndex((m) => m.id === optimistic.id);
  assert(after.length === 3, `no-jump: claimed in place, not duplicated (len=${after.length})`);
  assert(idx === after.length - 1, `no-jump: still the last row, idx=${idx}/${after.length - 1}`);
  assert(
    after[idx].optimistic !== true && after[idx].turn_id === "t9" && after[idx].seq === 3,
    "no-jump: claimed by the authoritative frame and carrying its view_seq",
  );
  assert(
    moves.length > 0 && moves.every((i) => i === startIndex),
    `no-jump: never re-positioned across commits, got [${moves.join(",")}]`,
  );
  // 随后被快照对账接管：仍是同一行、仍停在尾部（不新增重复行）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "新问题", seq: 3 },
    ],
    3,
  );
  assert(s().messages.length === 3, "no-jump: hydrate pairs the same row (no duplicate)");
  assert(
    s().messages.findIndex((m) => m.id === optimistic.id) === 2,
    "no-jump: still the tail row after hydrate",
  );
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    5,
    { epoch: TEST_EPOCH },
  );
  assert(s().skeletonSince === null, "snapshot: skeleton closed");
  s().resetForNewSession(TEST_SID);
  assert(s().viewReady === false && s().skeletonSince !== null, "/new: back to the skeleton");
  assert(s().needsHydrate === true, "/new: asks ChatView for a hydrate (E)");
  // 已上屏的行不因重复帧再上屏：序号已在屏上 ⇒ 丢弃；不在屏上 ⇒ 追加
  hBound({ type: "chunk", text: "重放的旧帧", source: "web", view_seq: 2, turn_id: "t1" });
  assert(!s().messages.some((m) => m.text === "重放的旧帧"), "/new: known-seq frame still dropped");
  hBound({ type: "chunk", text: "接上的新帧", source: "web", view_seq: 6, turn_id: "t1" });
  assert(s().messages.some((m) => m.text === "接上的新帧"), "/new: new frame still applied");
  // 权威提交把骨架与补拉请求一起收口
  s().loadHistory(
    [
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "接上的新帧", seq: 6 },
    ],
    6,
  );
  assert(
    s().viewReady === true && s().skeletonSince === null && s().needsHydrate === false,
    "commit: closes the skeleton + the hydrate request",
  );
  // 切空间无快照（旧服务端）：同样记一笔补拉请求
  s().resetForWorkspaceSwitch("/ws/legacy2");
  assert(
    s().needsHydrate === true && s().viewReady === false && s().messages.length === 0,
    "switch without snapshot: skeleton + hydrate request + empty view",
  );
  s().consumeNeedsHydrate();
  assert(s().needsHydrate === false, "consumeNeedsHydrate: cleared");
}

{
  reset();
  const s = useStore.getState;
  // 列表里已有两条空 assistant 行（queued 占位 + 一条已落带的空行）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "", seq: 2 }, // 已落带的空行（如 turn_start 占位）
    ],
    2,
  );
  // 实时再产生一条空行（queued 占位，无 seq）
  useStore.setState({ turnActive: true, currentTurnId: "t9" });
  h({ type: "turn_queued", turn_id: "t9", source: "web" });
  const emptyBefore = s().messages.filter(
    (m) => m.role === "assistant" && !m.diff && !m.tool && !m.dividerLabel && !m.text.trim(),
  ).length;
  assert(emptyBefore === 2, `byKey 收窄 setup: 两条空行（落带 + queued 占位），got ${emptyBefore}`);
  // hydrate 带回同两条空行（seq 1/2 已落带）+ 一条新空行（seq 3）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "", seq: 2 },
      { role: "assistant", text: "", seq: 3 },
    ],
    3,
  );
  const empties = s().messages.filter(
    (m) => m.role === "assistant" && !m.diff && !m.tool && !m.dividerLabel && !m.text.trim(),
  );
  // byKey 禁配下
  assert(empties.length === 3, `byKey 收窄: 空行不错配（3 条各自独立），got ${empties.length}`);
  const seqs = empties.map((m) => String(m.seq ?? "")).join(",");
  assert(seqs === "2,,3", `byKey 收窄: seq 命中原位 + 孤儿保留 + 新帧追加，got ${seqs}`);
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1);
  // 实时行「好的」（2 字符，禁配）
  h({ type: "chunk", text: "好的", source: "web" });
  // 快照带回同文短行（无 seq：服务端还没给它序号）→ 禁配 → 两行各自独立
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "好的" },
    ],
    1,
  );
  const shortRows = s().messages.filter((m) => m.text === "好的");
  assert(shortRows.length === 2, `阈值 16: 短同文禁配（两行独立），got ${shortRows.length}`);
  // 长文本（>16 字符）同场景仍配对：快照行认领实时行的 id，不新增行
  const longText = "这是一条超过十六个字符的助手回复正文";
  h({ type: "chunk", text: longText, source: "web" });
  const before = s().messages.length;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: longText },
    ],
    1,
  );
  assert(
    s().messages.length === before && s().messages.filter((m) => m.text === longText).length === 1,
    `阈值 16: 长同文按内容 key 配对（不新增行），got ${s().messages.length} vs ${before}`,
  );
}

{
  reset();
  const s = useStore.getState;
  const LONG = "这是一条足够长的助手回复用于验证就近配对窗口";
  // 首屏重建前屏上已有 200 条同文实时行（无 seq，viewReady 未置）
  const liveIds: string[] = [];
  for (let i = 0; i < 200; i++) {
    h({ type: "chunk", text: LONG, source: "web" });
    liveIds.push(s().messages[s().messages.length - 1].id);
  }
  // 权威快照（replace 分支）：59 条短行（禁配）+ 末行同文长回复 seq 60
  const snap: { role: "assistant"; text: string; seq: number }[] = [];
  for (let i = 1; i <= 59; i++) snap.push({ role: "assistant", text: `内容${i}`, seq: i });
  snap.push({ role: "assistant", text: LONG, seq: 60 });
  s().loadHistory(snap, 60);
  const seq60 = s().messages.find((m) => m.seq === 60);
  assert(
    seq60 !== undefined && seq60.id === liveIds[149],
    `byKey 就近: 按尾部相对位置配对（取 live[149]），got ${
      seq60 ? liveIds.indexOf(seq60.id) : "none"
    }`,
  );
  // 窗口外的同文行留作孤儿，不被吞
  assert(
    s().messages.filter((m) => m.text === LONG).length === 200,
    `byKey 就近: 200 条同文行一条不少，got ${s().messages.filter((m) => m.text === LONG).length}`,
  );
}

{
  reset();
  const s = useStore.getState;
  // 构造 60 行历史：下标 1（seq 2）是「在的」，下标 59（seq 60）也是「在的」
  const history: { role: "user" | "assistant"; text: string; seq: number }[] = [];
  for (let i = 0; i < 60; i++) {
    history.push({
      role: i % 2 === 0 ? "user" : "assistant",
      text: i === 1 || i === 59 ? "在的" : `内容${i}`,
      seq: i + 1,
    });
  }
  s().loadHistory(history, 60);
  // 实时尾部再发一条「在的」（无 seq）
  h({ type: "chunk", text: "在的", source: "web" });
  // hydrate 快照只覆盖尾部 10 行（seq 51-60），包含 seq 60 的「在的」
  const tail = history.slice(50).map((r) => ({ ...r }));
  s().loadHistory(tail, 60);
  const inDe = s().messages.filter((m) => m.text === "在的");
  assert(inDe.length === 3, `短同文: 三条「在的」各自独立（seq 2 + seq 60 + 实时），got ${inDe.length}`);
  const seq60 = s().messages.find((m) => m.seq === 60);
  assert(seq60 !== undefined && seq60.text === "在的", "短同文: seq 60 命中尾部快照");
  const seq2 = s().messages.find((m) => m.seq === 2);
  assert(seq2 !== undefined && seq2.text === "在的", "短同文: seq 2 保留原位（不被错配走）");
}

{
  reset();
  const s = useStore.getState;
  const dead = Array.from({ length: 10 }, (_, i) => `d${i}`);
  const output: Record<string, { text: string; result: string }> = {};
  const diffs: Record<string, unknown[]> = {};
  const briefs: Record<string, string> = {};
  for (const k of dead) {
    output[k] = { text: "", result: `结果-${k}` };
    diffs[k] = [];
    briefs[k] = `指令-${k}`;
  }
  // 插在最前的两个已经离窗口最远（后面还有 8 个更新的）：它们该被回收
  useStore.setState({
    subagentOutput: { ...output, "call-keep": { text: "", result: "留着" } },
    subagentDiffs: { ...diffs, "call-keep": [] },
    subagentBriefs: { ...briefs, "call-keep": "留着" },
  });
  s().loadHistory(
    [
      {
        role: "assistant",
        text: "",
        tool: { label: "delegate coaras: 留着", ok: true, tool_call_id: "call-keep" },
        seq: 1,
      } as never,
    ],
    1,
    { epoch: TEST_EPOCH },
  );
  assert(s().subagentOutput["call-keep"] !== undefined, "prune: delegate row still in messages ⇒ kept");
  assert(s().subagentOutput["d0"] === undefined, "prune: long-gone call folded output dropped");
  assert(s().subagentDiffs["d0"] === undefined && s().subagentBriefs["d0"] === undefined, "prune: all three maps pruned");
  assert(s().subagentOutput["d9"] !== undefined, "prune: recent calls kept even without a tool row yet");
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1, { epoch: TEST_EPOCH });
  hBound({ type: "chunk", text: "第4帧", source: "web", view_seq: 4, turn_id: "t9" });
  hBound({ type: "chunk", text: "迟到的第3帧", source: "web", view_seq: 3, turn_id: "t9" });
  assert(
    s().messages.map((m) => m.text).join("|") === "问1|第4帧|迟到的第3帧",
    `late frame appends (displayed rows never move), got ${s().messages.map((m) => m.text).join("|")}`,
  );
  assert(s().hydratedSeq === 1, "no cursor bookkeeping left on the live path");
  // 整线重建（本地为空）＝唯一按 view_seq 升序重排的时机
  reset();
  const s2 = useStore.getState;
  s2().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "user", text: "第3帧", seq: 3 },
      { role: "assistant", text: "第4帧", seq: 4 },
    ],
    4,
    { epoch: TEST_EPOCH },
  );
  assert(
    s2().messages.map((m) => m.seq).join(",") === "1,3,4",
    "rebuild: order equals view_seq asc",
  );
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
    ],
    2,
    { epoch: TEST_EPOCH },
  );
  // 回合进行中发出的接续输入：乐观泡带「待注入」徽标、没有序号
  useStore.setState({ turnActive: true, currentTurnId: "t9", turnStartedAt: Date.now() });
  s().addUserMessage("（回合中）再补一句");
  const echo = s().messages[s().messages.length - 1];
  assert(
    echo.optimistic === true && echo.pendingInject === true && echo.seq === undefined,
    "dup: 接续输入先落成乐观泡（待注入、无序号）",
  );
  const startIndex = s().messages.length - 1;
  const moves: number[] = [];
  const unsub = useStore.subscribe((st) => {
    const i = st.messages.findIndex((m) => m.id === echo.id);
    if (i >= 0) moves.push(i);
  });
  // 权威帧：source=web、带 turn_id 与 view_seq、边界匹配
  hBound({
    type: "user_message",
    content: "（回合中）再补一句",
    source: "web",
    turn_id: "t9",
    view_seq: 3,
  });
  unsub();
  const users = s().messages.filter((m) => m.role === "user");
  assert(users.length === 2, `dup: 接续输入只剩一条（用户行共 2），got ${users.length}`);
  assert(users[1].id === echo.id, "dup: 认领的是同一个 id（没有新建第二条）");
  assert(users[1].pendingInject === true, "dup: user_message 认领不清徽标（等 continuation_input_injected）");
  assert(users[1].optimistic !== true && users[1].turn_id === "t9" && users[1].seq === 3, "dup: 已绑定回合与序号");
  // 真正「LLM 已读到」的信号到达后才清徽标
  hBound({
    type: "continuation_input_injected",
    source: "web",
    turn_id: "t9",
    user_texts: ["（回合中）再补一句"],
  });
  assert(
    s().messages.find((m) => m.id === echo.id)!.pendingInject !== true,
    "dup: continuation_input_injected 到达后徽标才清",
  );
  assert(
    moves.length > 0 && moves.every((i) => i === startIndex),
    `dup: 位置不被搬运，got [${moves.join(",")}]`,
  );

  // 续 ①：随后一次 hydrate（快照含这一行、seq 与权威帧一致）→ 仍只有一条、同 id
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 2 },
      { role: "user", text: "（回合中）再补一句", seq: 3 },
    ],
    3,
  );
  const afterHydrate = s().messages.filter((m) => m.role === "user");
  assert(afterHydrate.length === 2, `dup: hydrate 后仍只有一条，got ${afterHydrate.length}`);
  assert(afterHydrate[1].id === echo.id, "dup: hydrate 复用同一行 id");
  // 续 ②
  hBound({
    type: "user_message",
    content: "（回合中）再补一句",
    source: "web",
    turn_id: "t9",
    view_seq: 3,
  });
  const afterReplay = s().messages.filter((m) => m.role === "user");
  assert(afterReplay.length === 2, `dup: 重放同一帧仍只有一条，got ${afterReplay.length}`);
  assert(afterReplay[1].id === echo.id, "dup: 重放不新建行");
}

{
  reset();
  const s = useStore.getState;
  s().loadHistory([{ role: "user", text: "问1", seq: 1 }], 1, { epoch: TEST_EPOCH });
  // ① 乐观泡（有 trim 差异）：乐观标记一路兜住
  useStore.setState({ turnActive: true });
  s().addUserMessage("尾随空白的话  ");
  const echo = s().messages[s().messages.length - 1];
  hBound({ type: "user_message", content: "  尾随空白的话", source: "web", turn_id: "t9", view_seq: 2 });
  let users = s().messages.filter((m) => m.role === "user");
  assert(
    users.length === 2 && users[1].id === echo.id,
    `dup: 仅差首尾空白也要认领同一条（乐观），got ${users.length} 条`,
  );
  assert(users[1].text === "  尾随空白的话", "dup: 文本以权威帧内容为准");
  // ② 已被 hydrate 换成权威行（没有乐观标记、没有 turn_id）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 4 },
      { role: "user", text: "hydrate 里的那句  ", seq: 5 },
    ],
    5,
  );
  const hydratedRow = s().messages.find((m) => m.text === "hydrate 里的那句  ");
  assert(hydratedRow !== undefined, "dup: hydrate 行就位");
  hBound({ type: "user_message", content: "  hydrate 里的那句", source: "web", turn_id: "t10", view_seq: 5 });
  users = s().messages.filter((m) => m.role === "user");
  assert(
    users.length === 3,
    `dup: 同序号帧被整帧丢弃（不新建行；此前的 two 条本地行原地保留），got ${users.length} 条用户行`,
  );
  assert(
    users.filter((m) => m.text.trim() === "hydrate 里的那句").length === 1,
    "dup: 不因重复帧多出一条同文行",
  );
  assert(
    s().messages.find((m) => m.id === hydratedRow!.id)?.text === "hydrate 里的那句  ",
    "dup: 既有行 id 与内容原样保留（已显示行只读）",
  );
  // ③ 本地行既没有乐观标记也没有序号（remount 净化抹掉瞬态标记后的样子）：靠 trim 文本认领
  useStore.setState({
    messages: [
      ...s().messages,
      { id: "stripped-echo", role: "user", text: "被净化过的那句   " } as never,
    ],
  });
  hBound({ type: "user_message", content: "被净化过的那句", source: "web", turn_id: "t11", view_seq: 6 });
  const stripped = s().messages.find((m) => m.id === "stripped-echo");
  users = s().messages.filter((m) => m.role === "user");
  assert(
    users.length === 4 && stripped?.turn_id === "t11" && stripped?.seq === 6,
    `dup: 无标记无序号的行靠 trim 文本认领（不新建），用户行 ${users.length} 条`,
  );
  // ④ 接续输入的乐观泡被 error 帧误标成「发送失败」（optimistic 被清）后，权威帧到达
  reset();
  const s2 = useStore.getState;
  s2().loadHistory([{ role: "assistant", text: "答0", seq: 1 }], 1, { epoch: TEST_EPOCH });
  useStore.setState({ turnActive: true, currentTurnId: "t20" });
  s2().addUserMessage("（回合中）又被标记的那句");
  const echo2 = s2().messages[s2().messages.length - 1];
  // error 帧：尾部的接续输入不该被标成发送失败
  hBound({ type: "error", message: "某个回合内的错误", source: "web" });
  assert(
    s2().messages.find((m) => m.id === echo2.id)?.sendFailed !== true,
    "dup: 接续输入不被 error 帧标成发送失败（它不是本次发送）",
  );
  hBound({
    type: "user_message",
    content: "（回合中）又被标记的那句",
    source: "web",
    turn_id: "t20",
    view_seq: 2,
  });
  users = s2().messages.filter((m) => m.role === "user");
  assert(
    users.length === 1 && users[0].id === echo2.id && users[0].sendFailed !== true,
    `dup: error 之后权威帧仍认领同一行、且失败样式不残留，用户行 ${users.length} 条`,
  );
}

{
  const base = {
    newPageLife: false,
    userScrolledUp: false,
    workspaceSwitched: false,
    hasAnchor: false,
    skeletonToContent: false,
  };
  // ① 新页面生命 + 未上翻 → 滚底
  assert(
    decideScroll({ ...base, newPageLife: true }) === "bottom",
    "scroll: 刷新（新页面生命）默认贴底",
  );
  // ② 骨架转真实内容（刷新那次提交就是这种）→ 滚底
  assert(
    decideScroll({ ...base, newPageLife: true, skeletonToContent: true }) === "bottom",
    "scroll: 骨架期没有历史可翻 ⇒ 内容到位重贴底",
  );
  // ③ 存活期 + 用户上翻过 → 保位（不滚）
  assert(
    decideScroll({ ...base, userScrolledUp: true }) === "hold",
    "scroll: 存活期上翻过 ⇒ 保位，hydrate/新消息都不拉底",
  );
  // ④ 空间切换 + 有锚 → 还原锚（锚自带 atBottom，未上翻的切换继续贴底）
  assert(
    decideScroll({ ...base, workspaceSwitched: true, hasAnchor: true }) === "anchor",
    "scroll: 切走再切回 ⇒ 还原该空间锚点",
  );
  // ④' 空间切换但没有锚（本页面生命里第一次去该空间）→ 落到最新消息
  assert(
    decideScroll({ ...base, workspaceSwitched: true, hasAnchor: false }) === "bottom",
    "scroll: 首次进入某空间 ⇒ 贴底",
  );
  // 边界：两条优先级（切换 > 上翻、骨架转内容 > 上翻）
  assert(
    decideScroll({ ...base, workspaceSwitched: true, hasAnchor: true, userScrolledUp: true }) === "anchor",
    "scroll: 空间切换优先于上翻状态",
  );
  assert(
    decideScroll({ ...base, skeletonToContent: true, userScrolledUp: true }) === "bottom",
    "scroll: 骨架转真实内容优先于上翻状态",
  );
}

{
  // 折叠区（过程组）：tool A(1)、diff B(2)、tool B(3)、diff A(4) → A, diffA, B, diffB
  const toolFrame = (seq: number, callId: string, label: string) => ({
    id: `f-tool-${seq}`,
    role: "assistant" as const,
    text: "",
    tool: { label, ok: true, tool_call_id: callId },
    seq,
  });
  const diffFrame = (seq: number, callId: string) => ({
    id: `f-diff-${seq}`,
    role: "assistant" as const,
    text: "",
    diff: { path: `p${seq}.py`, added: 1, removed: 0, hunks: [[{ kind: "add", oldNum: 0, newNum: 1, code: `+${seq}` }]] },
    ...(callId ? { tool_call_id: callId } : {}),
    seq,
  });
  const frames = [
    toolFrame(1, "call-A", "read(a.py)"),
    diffFrame(2, "call-B"),
    toolFrame(3, "call-B", "edit(b.py)"),
    diffFrame(4, "call-A"),
  ];
  const groups = buildToolLineGroups({ brief: "", work: [], frames, body: "", result: "" });
  const entries = groups[0]?.entries ?? [];
  const shape = entries
    .map((e) => (e.frame?.tool ? `tool:${e.frame.tool.tool_call_id}` : e.frame?.diff ? `diff:${e.frame.tool_call_id}` : "?") )
    .join("|");
  assert(
    shape === "tool:call-A|diff:call-A|tool:call-B|diff:call-B",
    `diff pairing: 折叠区按 call 配对（A, diffA, B, diffB），got ${shape}`,
  );
  // 同一工具多个 diff：按 view_seq 升序排在行后
  const multi = buildToolLineGroups({
    brief: "",
    work: [],
    frames: [toolFrame(10, "call-M", "edit(m.py)"), diffFrame(12, "call-M"), diffFrame(11, "call-M")],
    body: "",
    result: "",
  });
  const multiShape = (multi[0]?.entries ?? [])
    .map((e) => (e.frame?.tool ? "tool" : `diff#${e.frame?.seq}`))
    .join("|");
  assert(
    multiShape === "tool|diff#11|diff#12",
    `diff pairing: 同工具多个 diff 按 view_seq 升序，got ${multiShape}`,
  );
  // 老帧（没有 tool_call_id）：退回原顺序，不丢
  const legacyFrames = [diffFrame(1, ""), toolFrame(2, "call-L", "read(l.py)"), diffFrame(3, "")];
  const legacy = buildToolLineGroups({ brief: "", work: [], frames: legacyFrames, body: "", result: "" });
  const legacyShape = (legacy[0]?.entries ?? [])
    .map((e) => (e.frame?.diff ? "diff" : "tool"))
    .join("|");
  assert(
    legacyShape === "diff|tool|diff" && (legacy[0]?.entries?.length ?? 0) === 3,
    `diff pairing: 无 call_id 的老帧保持原顺序且不丢，got ${legacyShape}`,
  );
  // 配不到工具行（工具行缺失）的 diff：留原位、不丢
  const orphan = buildToolLineGroups({
    brief: "",
    work: [],
    frames: [diffFrame(1, "call-ghost"), toolFrame(2, "call-X", "read(x.py)")],
    body: "",
    result: "",
  });
  const orphanShape = (orphan[0]?.entries ?? []).map((e) => (e.frame?.diff ? "diff" : "tool")).join("|");
  assert(
    orphanShape === "diff|tool",
    `diff pairing: 找不到工具行的 diff 留在原位，got ${orphanShape}`,
  );
}

{
  reset();
  const s = useStore.getState;
  const sampleDiff = (tag: string) => ({
    path: `${tag}.py`,
    added: 1,
    removed: 0,
    hunks: [[{ kind: "add", oldNum: 0, newNum: 1, code: `+${tag}` }]],
  });
  hBound({ type: "tool", text: "read(a.py)", ok: true, tool_call_id: "call-A", source: "web", view_seq: 1 });
  hBound({ type: "diff", diff_lines: sampleDiff("b"), tool_call_id: "call-B", source: "web", view_seq: 2 });
  // ptc 子调用的 id 形如 code:xxxx，规则相同
  hBound({ type: "tool", text: "edit(b.py)", ok: true, tool_call_id: "call-B", source: "web", view_seq: 3 });
  hBound({ type: "diff", diff_lines: sampleDiff("a"), tool_call_id: "call-A", source: "web", view_seq: 4 });
  hBound({ type: "tool", text: "shell(code:1)", ok: true, tool_call_id: "code:1", source: "web", view_seq: 5 });
  hBound({ type: "diff", diff_lines: sampleDiff("c"), tool_call_id: "code:1", source: "web", view_seq: 6 });
  const shape = s()
    .messages.filter((m) => m.tool || m.diff)
    .map((m) => (m.tool ? `tool:${m.tool.tool_call_id}` : `diff:${m.tool_call_id}`))
    .join("|");
  assert(
    shape === "tool:call-A|diff:call-B|tool:call-B|diff:call-A|tool:code:1|diff:code:1",
    `diff pairing (stream): 到达序即显示序（不插回历史），got ${shape}`,
  );
  // 老 diff（无 call_id）不被搬走、也不丢
  hBound({ type: "diff", diff_lines: sampleDiff("legacy"), source: "web", view_seq: 7 });
  const last = s().messages[s().messages.length - 1];
  assert(
    last.diff !== undefined && last.tool_call_id === undefined,
    "diff pairing (stream): 无 call_id 的老 diff 落在末尾、未被打乱",
  );
  assert(s().messages.length === 7, `diff pairing (stream): 一条不丢，got ${s().messages.length}`);
}

{
  // ① delegate wait
  assert(
    isHiddenToolLine({ label: "delegate wait", tool_name: "delegate" }) === true,
    "hidden tool: delegate wait 命中",
  );
  // ② send_file：新帧按 tool_name 判；老帧缺 tool_name 时按 label 兜底
  assert(
    isHiddenToolLine({ label: "send_file(D:\\code_ws\\v8\\a.md)", tool_name: "send_file" }) === true,
    "hidden tool: send_file（带 tool_name）命中",
  );
  assert(
    isHiddenToolLine({ label: "send_file(D:\\code_ws\\v8\\a.md)" }) === true,
    "hidden tool: send_file 老帧（缺 tool_name）按 label 兜底",
  );
  assert(isHiddenToolLine({ label: "send_file" }) === true, "hidden tool: 裸 send_file 也命中");
  // ③ plan：只隐 action=plan（它的效果就是计划审阅气泡）；enter/exit 是模式开关，要上屏
  assert(
    isHiddenToolLine({ label: "plan - plan", tool_name: "plan" }) === true,
    "hidden tool: plan action=plan 命中",
  );
  assert(
    isHiddenToolLine({ label: "plan - plan" }) === true,
    "hidden tool: plan 老帧（缺 tool_name）按 label 兜底",
  );
  assert(
    isHiddenToolLine({ label: "plan - enter", tool_name: "plan" }) === false,
    "hidden tool: plan enter 上屏",
  );
  assert(
    isHiddenToolLine({ label: "plan - exit" }) === false,
    "hidden tool: plan exit 上屏",
  );
  assert(isHiddenToolLine({ label: "plan", tool_name: "plan" }) === false, "hidden tool: 光秃 plan 不隐");
  // label 像 plan 但 tool_name 是别的工具：不按 label 隐藏
  assert(
    isHiddenToolLine({ label: "plan - plan", tool_name: "read" }) === false,
    "hidden tool: tool_name 不是 plan 时不按 label 隐藏",
  );
  // ③ 该显示的照旧显示
  assert(
    isHiddenToolLine({ label: "delegate coaras: 摸底…", tool_name: "delegate" }) === false,
    "hidden tool: delegate spawn 不隐",
  );
  assert(
    isHiddenToolLine({ label: "read(D:\\code_ws\\v8\\a.py)", tool_name: "read" }) === false,
    "hidden tool: 普通工具行不隐",
  );
  assert(
    isHiddenToolLine({ label: "delegate resume", tool_name: "delegate" }) === false &&
      isHiddenToolLine({ label: "delegate message", tool_name: "delegate" }) === false &&
      isHiddenToolLine({ label: "delegate stop", tool_name: "delegate" }) === false,
    "hidden tool: resume/message/stop 都保留可见",
  );
  assert(isHiddenToolLine(undefined) === false, "hidden tool: 没有工具行时不隐");
  assert(
    isHiddenToolLine({ label: "delegate wait" }) === true,
    "hidden tool: 缺 tool_name 的老帧按 label 判定",
  );
  // 取向：认不出来就不隐（tool_name 与 label 都对不上）
  assert(
    isHiddenToolLine({ label: "send_file_extra(a)", tool_name: "read" }) === false,
    "hidden tool: 认不出来就保留（宁显不隐错）",
  );
  // ④ 数据不动：三条 tool 帧都进 messages，只是渲染层跳过被隐的那条
  reset();
  const s = useStore.getState;
  hBound({ type: "tool", text: "delegate wait", tool_name: "delegate", tool_call_id: "call-w", source: "web", view_seq: 1 });
  hBound({
    type: "tool",
    text: "delegate coaras: 摸底",
    tool_name: "delegate",
    tool_call_id: "call-s",
    source: "web",
    view_seq: 2,
  });
  hBound({ type: "tool", text: "send_file(a.md)", tool_name: "send_file", tool_call_id: "call-f", source: "web", view_seq: 3 });
  hBound({ type: "tool", text: "read(a.py)", tool_name: "read", tool_call_id: "call-r", source: "web", view_seq: 4 });
  const toolRows = s().messages.filter((m) => m.tool);
  assert(toolRows.length === 4, `hidden tool: 四行都在 store 里，got ${toolRows.length}`);
  const hidden = toolRows.filter((m) => isHiddenToolLine(m.tool));
  assert(
    hidden.length === 2 && hidden.every((m) => m.tool?.label !== "read(a.py)") && hidden.some((m) => m.tool?.tool_name === "send_file"),
    `hidden tool: wait 与 send_file 被跳过，got ${hidden.map((m) => m.tool?.label).join(",")}`,
  );
  // ⑤ 折叠区同样排除：wait / send_file 帧都不占过程组条目（计数也不含它们）
  const folded = buildToolLineGroups({
    brief: "",
    work: [],
    frames: [
      { id: "fw", role: "assistant", text: "", tool: { label: "delegate wait", ok: true, tool_name: "delegate" } },
      { id: "ff", role: "assistant", text: "", tool: { label: "send_file(a.md)", ok: true, tool_name: "send_file" } },
      { id: "fr", role: "assistant", text: "", tool: { label: "read(a.py)", ok: true, tool_name: "read" } },
    ],
    body: "",
    result: "",
  });
  assert(
    folded[0]?.entries?.length === 1,
    `hidden tool: 折叠区条目排除 wait 与 send_file，got ${folded[0]?.entries?.length} 条`,
  );
}

{
  assert(
    anchorAtViewportTop([]) === BOTTOM_ANCHOR,
    "anchor measure: empty list falls back to bottom",
  );
  assert(
    anchorAtViewportTop([
      { key: "a", top: -300, bottom: -120 },
      { key: "b", top: -100, bottom: -8 },
    ]) === BOTTOM_ANCHOR,
    "anchor measure: rows fully above the viewport fall back to bottom",
  );
  const partial = anchorAtViewportTop([
    { key: "a", top: -300, bottom: -120 },
    { key: "b", top: -40, bottom: 60 },
    { key: "c", top: 70, bottom: 150 },
  ]);
  assert(
    partial.kind === "message" && partial.key === "b" && partial.offset === -40,
    `anchor measure: first partially visible row wins, got ${partial.key}@${partial.offset}`,
  );
  const first = anchorAtViewportTop([
    { key: "a", top: 12, bottom: 90 },
    { key: "b", top: 96, bottom: 180 },
  ]);
  assert(
    first.kind === "message" && first.key === "a" && first.offset === 12,
    `anchor measure: topmost fully visible row is the anchor, got ${first.key}@${first.offset}`,
  );
}

{
  reset();
  const s = useStore.getState;
  // 已在屏上的内容：一对问答 + 一条工具行（序号刻意留空档，好放迟到帧）
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 5 },
      { role: "assistant", text: "", tool: { label: "read(a.py)", ok: true, tool_call_id: "call-1" }, seq: 6 },
    ] as never,
    6,
    { epoch: TEST_EPOCH },
  );
  const keysOf = () =>
    s().messages.map((m) => `${chatRowKey(m)}|${m.text || (m.tool ? m.tool.label : "")}`);
  const before = keysOf();
  const prefixIntact = (tag: string) => {
    const now = keysOf();
    assert(
      now.slice(0, before.length).join() === before.join(),
      `${tag}: 已显示前缀的服务端键与内容逐项不变，got ${now.slice(0, before.length).join()}`,
    );
  };

  // ① 迟到帧（序号 3 不在屏上、比屏上的 5/6 小）：追加在尾部（旧实现在这里插进中间）
  hBound({ type: "chunk", text: "迟到的中间帧", source: "web", view_seq: 3, turn_id: "t-late" });
  prefixIntact("迟到帧");
  assert(
    keysOf()[before.length] === "s3|迟到的中间帧",
    `迟到帧: 只追加在尾部，got ${keysOf()[before.length]}`,
  );

  // ② 回放帧（序号 5 已在屏上、正文不同）：整帧丢弃，不新建也不覆盖
  hBound({ type: "chunk", text: "重放的答1", source: "web", view_seq: 5, turn_id: "t-replay", replayed: true } as never);
  prefixIntact("回放帧");
  assert(
    !s().messages.some((m) => m.text === "重放的答1"),
    "回放帧: 序号已在屏上 ⇒ 不上屏",
  );

  // ③ 同文再发：乐观泡追加在尾部；权威帧就地认领（只升 key 与内容，不搬位）
  s().addUserMessage("问1");
  prefixIntact("同文再发（乐观泡）");
  assert(
    keysOf()[keysOf().length - 1].startsWith("l"),
    "同文再发: 乐观泡在尾部（尚无服务端键 → 本地键兜底）",
  );
  hBound({ type: "user_message", content: "问1", source: "web", turn_id: "t-new", view_seq: 7 });
  prefixIntact("权威认领");
  assert(
    keysOf()[keysOf().length - 1] === "s7|问1",
    `权威认领: 就地升级为服务端键、仍在尾部，got ${keysOf()[keysOf().length - 1]}`,
  );
  assert(
    s().messages.filter((m) => m.role === "user" && m.text === "问1").length === 2,
    "权威认领: 不新增重复行（既有权威行 + 刚认领的那条）",
  );

  // ④ hydrate 对账（同一条线）：命中既有行只改内容，命不中的追加在尾部
  s().loadHistory(
    [
      { role: "user", text: "问1", seq: 1 },
      { role: "assistant", text: "答1", seq: 5 },
      { role: "assistant", text: "", tool: { label: "read(a.py)", ok: true, tool_call_id: "call-1" }, seq: 6 },
      { role: "assistant", text: "答8", seq: 8 },
    ] as never,
    8,
    {},
  );
  prefixIntact("hydrate 对账");
  assert(
    keysOf()[keysOf().length - 1] === "s8|答8",
    `hydrate 对账: 命不中的权威行追加在尾部，got ${keysOf()[keysOf().length - 1]}`,
  );

  // ⑤ /new 只动分隔线（追加一条，或就地改最后一条），已显示行一行不挪
  const beforeNew = keysOf();
  s().resetForNewSession("sid-next");
  assert(
    keysOf().slice(0, beforeNew.length).join() === beforeNew.join(),
    "/new: 已显示前缀逐项不变",
  );
  assert(
    keysOf()[keysOf().length - 1].startsWith("l") &&
      s().messages[s().messages.length - 1].dividerLabel === "新会话",
    "/new: 新分隔线只追加在尾部",
  );

  // ⑥ 整线重建（epoch 变）＝唯一允许换序换键的时机：整表换成快照，顺序＝view_seq 升序
  s().loadHistory(
    [
      { role: "assistant", text: "后", seq: 12 },
      { role: "user", text: "前", seq: 11 },
    ] as never,
    12,
    { epoch: "epoch-next" },
  );
  assert(
    s().messages.map((m) => m.seq).join(",") === "11,12" && s().messages.length === 2,
    `整线重建: 整表换成快照且顺序等于 view_seq 升序，got ${s().messages.map((m) => m.seq).join(",")}`,
  );
}

console.log("store selftest: all assertions passed");
