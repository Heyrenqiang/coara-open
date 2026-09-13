# coara Web 端显示与刷新框架

> 本文是 **Web 浏览器聊天区**（`subject=root`）的显示、刷新、多端隔离与折叠区契约的 Canonical 说明。
> 硬约束（单一事实、一条入屏通道、顺序不变量）见 [`Web设计体系.md`](./Web设计体系.md) §0.1–§0.4；本文写它在服务端与端上的实现。
> 实现入口：`src/ui/web/src/lib/store.ts` · `src/ui/web_server.py` · `src/ui/web_views.py` · `src/ui/turn_stream.py`
> 对照：[CLI 展示框架](./CLI_DISPLAY_FRAMEWORK.md) · [架构契约 §web 端数据载体](./架构契约.md) · [会话事件溯源](./SESSION_EVENT_SOURCING.md)（录像带冷备，**非** Web 聊天数据源）

---

## 1. 设计目标

Web 聊天页要把内核产出变成**可刷新、可切空间、可与 CLI/手机并行**的稳态 UI，同时遵守多端契约：

| 原则 | 含义 |
|------|------|
| **单一事实** | 屏幕上的一切只有一个来源：该空间的视图带（服务端权威）+ 它此刻的 runtime。端上不合成持久内容 |
| **一条入屏通道** | 只有两种输入——快照与实时帧；两者都带 `workspace_dir` / `session_id` / `view_seq`，由端上同一个 reducer 应用 |
| **序号对账** | `view_seq` 同线永久单调：已覆盖丢弃 / 接上推进 / 空洞就地补齐；不靠文本比对猜重复 |
| **原子提交** | 内容、runtime、线身份、折叠区在同一次状态提交里落定；中途只显示骨架，禁止「先一半后补齐」 |
| **零镜像** | Web 只显示 `source=web` 的回合产出；CLI/Matrix 回合不进 Web 气泡（同空间时正文按段归属，跟话另起段） |
| **持久化展示** | 聊天区每条可见内容在 `web_views` JSONL 中有落盘载体，刷新与实时读同一份数据 |

斜杠命令、错误 toast 等用户可见文案约定见 [`CONVENTIONS.md`](./CONVENTIONS.md) §用户可见文案。

---

## 2. 总架构：内核统一，三端各看各的

```text
                    ┌─────────────────────────────────┐
                    │         RootCoara 内核           │
                    │  process_message(source=…)       │
                    │  注入段 segment.source → 归属端   │
                    └──────────────┬──────────────────┘
                                   │ EndRegistry.deliver(source, session_id, frame, channel_id)
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
    cli-attached WS          Web TurnStream           Matrix room
    (attach 多连接)          (单活跃浏览器)            (手机/Android)
           │                       │                       │
    scrollback + 活动树      聊天气泡 + 侧栏工具          房间消息 + COARA_DIFF
    cli_shows_source()       isWebSource()              should_push_matrix()
```

- **命中判据与执行结果分离**：`EndRegistry.deliver()` 返回 `RouteResult(hit, value)` —— `hit` 是「查到了 sender」，与 sender 自己返回什么无关（同步 sender 返回 `None` 也是命中）。只用 `route()` 的返回值反推命中会把「同步 void」误判成「无通道」，进而多落一次带。
- **端判定单一事实源（Python）**：`src/coara/turn_source.py`；**（Web 前端）**：`store.ts` 内 `isWebSource(source) => source === "web"`。
- **Trace 出站主门**：`src/ui/trace_broadcast.py`（按 `source` / 共看同空间过滤；客户端再滤只是兜底）。

**端独立 vs 同空间共享（与 [架构契约](./架构契约.md) 一致）：**

| | 端可以不一样 | 共看同一空间才一致 |
|--|-------------|-------------------|
| 例子 | 气泡、工具侧栏、活动树、切空间 chrome | `message_history`、生效模型 + 顶栏模型显示、该空间 `/new` 后的 session |

---

## 3. Web 聊天区：两种输入，一个 reducer

```text
屏幕内容 ← 快照（权威，带 view_seq / epoch / runtime）
        ← 实时帧（WS，带 view_seq + 归属；编号大者接续，缺号者补齐）

端上另存：UI state（滚动位 / 展开态 / 草稿）+ 乐观回显（自己发的气泡、流式正文）
```

| 项 | 现状 |
|----|------|
| 内容缓存 | **已废除**：IndexedDB 预览层（`sessionCache.ts` / `idb.ts`）已删；`sessionViewCache` 只存「这个空间用哪个 sessionId」，不存内容；`messagesFromCache` 字段恒 `false`（保留是给「同 session 重挂载可否跳过 hydrate」留显式位） |
| 骨架态 | `viewReady`：当前空间内容是否已由权威快照落定（`loadHistory` 的 commit 是唯一置 true 点）；`skeletonSince` 为骨架起点，超阈值给明确文案与重连入口 |
| 对账闸门 | `gateReady`：线身份与游标是否可信。与 `viewReady` 刻意分成两位——`/new` 只换会话段（同线同游标），只该置骨架，不能把闸门关掉 |
| 线身份 | `lineEpoch` = `{workspace_dir}::{subject}[#世代]`；与快照不同＝整条线已重建，本地内容与折叠区一律作废全量重取 |

乐观回显的边界：用户自己发出的消息与正在流式生成的正文可以先上屏（表达「我做了什么」），权威数据到达时必须**等价替换**（同一条内容不变成两行）；除此之外端侧不合成任何持久内容。

---

## 4. 端到端数据流

```text
用户发 chat (WS)
  → web_server._handle_chat / _stream_chat_turn
  → TurnStream.emit(turn_start | user_message | chunk | tool | diff | subagent_* | turn_end)
       ├─ _record：补 seq / turn_id / source / subject / session_id / workspace_dir
       │    ├─ persist 回调（WebViewStore.make_persist）→ 分配 view_seq → 写进帧
       │    └─ ring buffer（重连 replay，≤200 帧）
       └─ 广播：attach 端定向发 route.channel_id；web 端发当前活跃连接

内核正文/diff（任意端发起、按段归属）
  → base._route_chunk_to_current_end / _route_tool_diff
  → EndRegistry.deliver(seg_source, session_id, frame, channel_id)
  → 命中：端 sender；未命中：逐级回退（回合发起端 → 会话归属端）→ 仍无则 WARNING 留痕
```

**落带路径**：主路径是 `web sender → TurnStream._record → WebViewStore.append_event`（含 standby 流）；旁路只有 delegate 的两处直接落带，且互斥、必带 web 源守卫——任务指令帧（`delegate._persist_delegate_task_view`，与实时 trace `user_message` 同源同字段）与「此刻无任何可命中通道」时子智能体最终答复的兜底落带（`_route_subagent_result`，命中通道即闭口）。落带先于广播——帧先拿到 `view_seq` 再发出去，端侧因此能与快照的 `latest_seq` 对账。

**无在飞回合流时用 standby 流**：连接级兜底 sender（全局槽 `(web, "")`，只注册不注销）按帧归属找回合流，找不到就按 `(session, workspace)` 建/复用一条 standby `TurnStream`（不挂 task、不进 `_turns`），走同一条 `_record` 落带 + 广播。上限 `_MAX_STANDBY_STREAMS = 8`。没有这条兜底，精确槽被误注销或回合中途刷新的帧会静默丢弃。

**微批**：`chunk` 与 `subagent_chunk` 在 16ms 窗口内合并成一帧（后者按 `tool_call_id` 分桶，每条 delegate 行一桶），削掉逐 token 一帧的开销。

**WS 首帧只带 runtime**：`state` 帧曾顺带投影全量 sessions（冷启数秒、载荷近 10MB，端上并不消费），现只保留 runtime——端上靠它拿 `session_id` / `workspace_dir` 定边界。心跳每 5s 只推轻量 runtime。

---

## 5. 刷新 / F5 / 切空间时序

### 5.1 订阅先于快照

1. WS 连接即订阅；此时 `workspaceDir` 可能还没落定
2. 边界未定的内容/回合类帧（`user_message` / `turn_start` / `chunk` / `diff` / `tool` …）先入缓冲（上限 300；溢出优先丢「能从快照补回」的落带帧，保住无第二来源的过程帧）
3. `state` 帧把 `workspaceDir` / `sessionId` 落定 → 缓冲按 `view_seq` 排序后回放（无序号帧排最后），回放帧走同一套守卫与去重，不再入缓冲
4. 快照落地后：`view_seq <= latest_seq` 的帧丢弃，其余按序应用，最后一次提交上屏

### 5.2 原子提交

`loadHistory` 的 commit 是**一次** `set`：`messages`（已过顺序归一）+ `hydratedSeq` + `lineEpoch` + `runtime`（含 turnActive / spinner）+ 折叠区三映射（`subagent_results` / `subagent_diffs` / `subagent_briefs`）。分两次 set 就是「先渲染消息、再补 spinner 与展开区」＝跳变。

### 5.3 切空间：一次往返

`POST /api/workspace/switch` 的响应直接带目标空间的权威快照（`_load_view_snapshot`，与 `/api/session/messages` 同源同形状）——端上「清边界 → 就地提交快照」一步完成，不再补发 `/api/session/messages`。这样一次切换只出现两种画面：骨架 → 目标内容。切空间**不落分隔帧**（服务端不落、端上也不自插：自插的线会被下一次 hydrate 冲掉并污染缓存）。

### 5.4 hydrate：全量与增量

- 请求带 `workspace_dir`（只读预取；服务端在该模式下只读那条线、不动任何运行时状态、不写他空间的 sidecar）
- 已有游标与内容时走增量：`after_view_seq = hydratedSeq - HYDRATE_OVERLAP(50)`，只补后缀，游标之前一个字不动
- 增量返回的 `epoch` 与本地不同 ⇒ 这次响应作废，改走一次全量（增量按 seq 拼接的前提是「同一条线」）
- 归属守卫：响应回来时 `workspaceDir` / `hydrateGeneration` / `runtime.workspace_dir` 任一不匹配即丢弃

### 5.5 进行中回合断连

回合与 WS **解耦**：`TurnStream` 在服务端继续跑，重连后 replay 尾部 + 快照已落带部分，两端叠加由 `view_seq` 去重（`replayed` 帧另有 `_isReplayDuplicate` 兜底）。重连还会重建在飞回合的端 sender，并重注册连接级兜底通道。

---

## 6. 序号对账与顺序不变量

### 6.1 三种走向（`handleServerMessage` 的 seq 闸门）

`frameSeq > 0` 且闸门可用且帧在边界内时，与游标 `hydratedSeq` 比：

| 情形 | 处理 |
|------|------|
| `frameSeq <= cursor`（已覆盖） | **内容行**帧丢弃（防 replay 与快照叠加双显）；不产生内容行的状态帧（`turn_end` 等）放行——它没有行可重复，丢的却是状态 |
| `frameSeq > cursor + 1`（空洞） | 本帧与其后的帧入缓冲，触发一次增量补齐（带退避重试，最多 3 次尝试；同一空洞不重复打请求），补齐提交后按序回放 |
| `frameSeq == cursor + 1`（接上） | 推进游标，帧照常走下面的 case 分支（顺序由顺序归一保证） |

无 `view_seq` 的帧（子智能体过程帧等白名单 `UNPERSISTED_FRAME_TYPES`）不参与任何 seq 裁量，直接放行。

### 6.2 顺序不变量与归一

端上 `messages` 必须始终满足两条：

1. **落带段**（带 `seq`）按 `seq` 升序；
2. **未落带实时尾部**（无 `seq`）按到达序，排在落带段之后。

`_orderedMessages` 在每次落定时过一次（`_withRow` 与 `loadHistory` 的 commit 都调），同 `seq` 的后来者丢弃＝同一帧被实时/回放/快照三路各送一次时的幂等。快路径：已满足不变量且无重复时返回原引用，不触发重渲染。

**会破坏它的路径与防线**：

| 路径 | 为什么危险 | 防线 |
|------|-----------|------|
| 迟到帧（断连重放、缓冲回放、gap 补齐回放）直接 append | 序号小于已渲染内容的最大序号，老内容画到最下面 | 落定即归一（不靠单点 append 保证顺序） |
| 缓冲/回放帧按到达序送 | 空洞期的迟到帧与先到的后帧会交错应用 | 回放入口先按 `view_seq` 排序（无号帧保持到达序、排最后） |
| 增量 hydrate 整体追加 | 游标可能偏（缓存来自上一页生命、他端动过），带回已渲染过的帧 | 同 `seq` 的行原地替换（id 与位置不变），只追加真正新增的行 |

---

## 7. 显示规则矩阵（主会话 `subject=root`）

| 内容类型 | Web 聊天区 | Web 右侧栏（工具） | CLI attach | Matrix |
|----------|------------|-------------------|------------|--------|
| 用户气泡 | `source=web` | user_message trace | 本端 | 房间 |
| assistant 正文 chunk | `isWebSource` | — | `cli_shows_source` | `should_push_matrix` |
| 工具 ✓/✗ 行 | **聊天流内联一行**（插在正文段落之间） | `tool_*` + web source | scrollback | ✗ |
| edit diff | `source=web` | tool_complete | diff 面板 | COARA_DIFF |
| 子智能体工具行 / diff | ✗（折进 delegate 行展开区） | — | 活动树 | ✗ |
| 子智能体正文 / 最终答复 | ✗（同上） | — | 活动树 | 直推 |
| janitor / daily | silent | silent | silent | silent |
| slash 命令结果 | command_result 卡片 | — | 同 | — |

工具行与 diff 的插入顺序：工具行帧必须先于 diff 帧投递，否则 diff 会跑到 edit 行上方。

### 7.1 走 `isWebSource` 门的 WS 类型

`user_message` · `turn_start` · `turn_queued` · `chunk` · `turn_end` · 侧栏 `tool_*` · **`diff`** · **`tool`（工具行）**

### 7.2 不走此门的类型

`command_result` · `continuation_input_injected` · `chat_turn_retracted` · vault · `state` / heartbeat

### 7.3 跟话（Web 在 CLI/手机占回合时发消息）

跟话 `user_message` 与开局用户行同形：同一 `TurnStream.emit_user_message`（含
`client_msg_id` / `attachments`），经微批时间线落带并广播——不是旁路直写。

```text
has_active_turn && 非 slash
  → 找或建本会话 web TurnStream（persist=视图带）
  → emit_user_message（正式用户行）
  → EndRegistry 登记指向该流的 sender
  → submit_continuation_input(source=web)
  → set_deferred_remote_ctx（仅审批/交互门；聊天正文不走此路）
  → 后续 chunk/diff 按段 source=web 经 TurnStream
  → 前端每条 chunk = 独立 assistant 气泡（不续写旧泡）
```

实现：`web_server.py` `_handle_chat` 活跃回合分支；`TurnStream.emit_user_message`；
内核 `base.py` + `end_registry.py`。CLI attach / Matrix 同构：跟话只是入队时机不同，
用户行仍是正式输出（attach 走 TurnStream；Matrix 以房间里用户消息为正式用户行）。

---

## 8. 服务端持久化：`web_views`

**Canonical 存储说明**亦见 [架构契约 §web 端数据载体](./架构契约.md)。

| 项 | 说明 |
|----|------|
| **路径** | 主对话：`<coara_home>/workspaces/<id>/web_views/conversation.jsonl`（**一个空间一条线**，`session_id` 不参与命名；`/new` 只是线上一个分隔帧）。模块会话：`web_views/{subject}__{session_id}.jsonl`；`flow` 走 `workflow_assets_root(coara_home)/web_views/` |
| **写** | TurnStream persist 回调 → `WebViewStore.append_event`（异步批量写，失败只记日志、绝不中断回合） |
| **读** | `GET /api/session/messages` → `SessionHandlers._load_view_snapshot`（**唯一构造点**，切空间响应用同一个） |
| **对账键** | `view_seq`（快照 `latest_seq` / 端上 `hydratedSeq`） |
| **线身份** | `epoch = {workspace_dir}::{subject}[#世代]` |
| **单调** | 同一条线内 `view_seq` **永久单调、不重置**：分配时取「jsonl 尾部最大序号」与「sidecar 高水位（`<file>.meta.json`）」较大者 +1。文件被归档/清空/截断都不会让序号回退。重置序号只有 `WebViewStore.reset_line` 一条合法路径（世代 +1 → epoch 变化） |
| **与录像带** | `session_events.jsonl` 为全端冷备/审计；Web 聊天区**不读**录像带投影 |

帧类型：`turn_start` · `user_message` · `turn_queued` · `chunk` · `tool` · `diff` · `files` · `error` · `divider` · `subagent_result` · `turn_end`。其中 `subagent_chunk`（过程正文）**不落带**——只做实时投递；落了带，hydrate 会把它当正文复现出来。

**快照形状**（`_load_view_snapshot`）：

```text
{messages, total, latest_seq, workspace_dir, session_id, epoch,
 subagent_results, subagent_diffs, subagent_briefs, runtime}
```

- `limit` 默认 100、上限 500；`after_view_seq` 走增量；`workspace_dir` 走只读预取（非绝对路径直接 400）
- `runtime` 是「该空间此刻的回合态」，与消息同源回来——刷新与切空间走同一条路径、同一份权威，不靠心跳推送（推送跳过时端侧只能拿缓存猜）

---

## 9. 子智能体折叠区契约

子智能体的产出面不是主对话正文，一律钉在**发起它的那条 delegate 工具行**里。钉法：服务端给归属键，端上按归属键归集（详见 [Web设计体系.md](./Web设计体系.md) §0.3）。

| 映射 | 形状 | 归属键 | 来源帧 |
|------|------|--------|--------|
| `subagent_results` | `{tool_call_id: text}` | 子智能体任务 id（= delegate 行的 `tool_call_id`） | 落带 `kind=subagent_result` |
| `subagent_briefs` | `{父 call_id: 指令全文}` | 发起它的 delegate 行 call_id | `user_message` 帧带 `delegate_brief` 标记（老数据 `delegate_task` / 正文以 `<任务指令>` 开头） |
| `subagent_diffs` | `{父 call_id: [帧…]}` | 同上 | 带 `parent_tool_call_id` 的 `tool` / `diff` 帧 |

- **不投影成消息**：三张映射只随快照顶层下发，`build_messages` 不把它们投影成聊天行（落了气泡就是刷新后凭空多一条回复）。
- **实时侧同一套键**：WS 帧带 `parent_tool_call_id`；`subagent_chunk` 不落带、只按 `tool_call_id` 分桶微批送折叠区；快照与实时按 `view_seq` 去重后并进同一张表。任务指令帧与「无通道兜底」的最终答复帧由 delegate 直接落带（§4），与实时帧同字段，因此刷新前后归集结果一致。
- **手风琴组序**（`toolLineGroups.ts::buildToolLineGroups`）：任务指令 → 过程 → 最终结果。**过程组**把同一件事的三个来源合成一条时间线：① 落带帧（工具行 / diff，按 `view_seq` 升序）→ ② 活动树里帧还没有的行（在跑的工具，行首 `◌`）→ ③ 过程输出正文（排最后、无标签）。①② 按 call 去重、以帧为准（同一工具只显示一次）；计数提示合成一条（`N 项` + 有正文时 ` · X 字`）。最终结果独立成组并带标签。空组不出现；一组都没有则该 delegate 行不可展开。默认展开态：任务指令收起（长文本、需要时再看），其余展开。组内限高 160px、面板 240px，超出组内滚动。组内 diff 卡片用 `DiffBlock` 的紧凑变体（左右内边距归零），左缘与同组工具行的 `✓`、与过程正文一致（树行补给行保留 `depth` 缩进）。
- **隐藏的过程噪音工具行**（`toolVisibility.ts::isHiddenToolLine`，文档只登记清单）：`delegate wait`（内部同步点、不是工作，一回合能出现几十次；活动树同规则不给它建行）与 `send_file`（效果已是聊天流里的文件/图片卡，工具行是重复信息，判 `tool_name`，老帧缺字段时按 label `/^send_file\b/` 兜底）。**只过滤渲染**——行仍在视图带与 `messages` 里，照旧参与顺序不变量、去重与快照对账；折叠区条目与计数一并排除；取向「认不出来就不隐」。
- **封顶**：单次快照的折叠映射只回游标之后的帧（`view_seq > since_seq`），call_id 数封顶 30、单 call 帧数封顶 100；端上再随消息窗口回收（只留 messages 里仍有 delegate 行的 call、活动树在跑的 call，以及最近 8 个出现过 call）。

---

## 10. 模块会话与 Flow

| subject | source 标签 | hydrate API | 合并策略 |
|---------|-------------|-------------|----------|
| `root` | `web` | `/api/session/messages` | 每条 chunk 独立一行（与实时逐帧一致） |
| `flow` 等 | `web-{subject}` | `/api/module-session/messages?subject=` | 同回合合并为一条（与实时追加同一气泡一致） |

模块会话状态在 `store.moduleSessions`，与主聊天 `messages` 隔离。模块线不进主对话那条线（各按会话分文件）。

---

## 11. 已知限制与遗留

| 项 | 说明 |
|----|------|
| 快照游标 | 已同源：`latest_seq` 只覆盖**实际返回切片**的末帧（被裁帧不再被当成「已送达」，长线历史不会出现永久静默空洞），见 `web_views` 返回路径 |
| 条数常量 | 刷新取 200（前端）、切空间快照取默认 100（服务端）——同一把尺两个数（#28） |
| 无 seq 兜底锚点 | `loadHistory` 仍保留「hydrate 末条文本在尾部 8 条窗口内找锚」的旧服务端兜底分支，仅在 `hasSeqKey=false` 时生效；实时帧与快照都已带 `view_seq`，主路径不走它 |
| 纯图片跟话 | `pendingInject` 靠文本匹配，空气泡可能清不掉徽标 |
| 侧栏 hydrate | WS 已有 tool 行时 skip REST，刷新后工具列表可能不全 |
| heartbeat | `running=true` 未必带 `turn_id`，极端时 spinner 略不同步 |
| replay 上限 | 200 帧；多 completed turn 重连可能短暂闪烁 |
| 折叠区封顶 | 超 30 个 call / 单 call 超 100 帧的历史子智能体产出刷不全；老数据 delegate 指令无父 call_id 时归不到位（#33） |

跟踪清单见 [`REMAINING_ISSUES.md`](./REMAINING_ISSUES.md)。

---

## 12. 源码索引

| 环节 | 文件 |
|------|------|
| 端上状态机 / 序号对账 / 顺序归一 | `src/ui/web/src/lib/store.ts` |
| 聊天行与折叠区渲染 | `src/ui/web/src/features/chat/MessageList.tsx` · `ToolLineRow.tsx` |
| 折叠区分组判据 | `src/ui/web/src/lib/toolLineGroups.ts` |
| 活动树状态机 | `src/ui/web/src/lib/subagentTree.ts` |
| hydrate 触发 | `src/ui/web/src/views/ChatView.tsx` |
| WS 协议类型 | `src/ui/web/src/lib/ws.ts` |
| WS 服务 / sender / standby 流 / 跟话 | `src/ui/web_server.py` |
| 打开/唤起三态判定与标签存在性 | `src/ui/web_server.py`（`open_or_focus_decision`）· `src/ui/web_tab_presence.py` · `src/ui/web/src/lib/tabPresence.ts` |
| 回合流与微批 | `src/ui/turn_stream.py` |
| 视图存储 / 序号 / epoch / 折叠映射 | `src/ui/web_views.py` |
| 快照构造（唯一）与 REST | `src/ui/handlers/session.py` · `handlers/workspace.py` |
| 端路由 | `src/coara/end_registry.py` · `src/coara/base.py` |
| 来源契约 | `src/coara/turn_source.py` |
| 端上 selftest | `src/ui/web/src/lib/store.selftest.ts` |
| 帧序号 / 快照 / 折叠回归 | `tests/test_ui/test_view_seq_frames.py` · `test_web_views.py` |

---

## 13. 构建、部署与自检

**前端构建**（`src/ui/static/dist/` 不入 git）：

```bash
cd src/ui/web && npm run build
```

改 `web_server.py` / `web_views.py` 后需**重启 coara 内核**。

**自动化：**

```bash
cd src/ui/web && npx tsx src/lib/store.selftest.ts
pytest tests/test_ui/test_view_seq_frames.py tests/test_ui/test_web_views.py -q
```

**手动自检：**

1. Web 发消息 → F5 → 历史完整、无重复气泡、顺序不乱
2. 快速切两个工作空间 → 无串台，且只出现「骨架 → 目标内容」两种画面
3. Config ↔ Chat 来回 → 不闪白、不重复 hydrate
4. 断网/重连跨回合 → 正文与工具行接续、无空洞重复
5. edit → Web 聊天 diff 卡片位置在对应工具行下方；刷新后位置不变
6. delegate → 展开该工具行：任务指令 / 过程 / 最终结果三组齐全（过程组里落带帧、活动树补给行、过程正文按序同列，同一工具不重复）

---

## 14. 与 CLI / 录像带的关系

| 文档 | 关系 |
|------|------|
| [CLI_DISPLAY_FRAMEWORK.md](./CLI_DISPLAY_FRAMEWORK.md) | attach 三通道；与 Web **零镜像** |
| [SESSION_EVENT_SOURCING.md](./SESSION_EVENT_SOURCING.md) | 录像带冷备；Web 聊天不读其投影 |
| [架构契约.md](./架构契约.md) | 多端 FIFO、web_views 帧矩阵、读写分离 |
| [MATRIX_APP_LINK.md](./MATRIX_APP_LINK.md) | 手机端房间与 COARA_DIFF |

多端聊天意图硬约束见仓库 `.cursor/rules/multi-end-chat-intent.mdc`。

---

## 15. 打开 / 唤起 Web UI：三态判定

**问题**：打开/唤起原本只看进程内的活跃 WebSocket 连接。内核重启丢掉内存态、后台标签的 WS 又被浏览器节流，于是「已有标签」被误判成「没有标签」→ `webbrowser.open` 新开一个标签，而新标签立刻被 `SingleTabGuard` 判定为后来者自我关闭 —— 用户看到「开一下又关掉」。

**判定真源**：`WebServer.open_or_focus_decision()`（`src/ui/web_server.py`），三态：

| 态 | 条件 | 动作 |
|----|------|------|
| `focus-active` | 本进程有活跃 WS 连接 | 推 `focus_window` 帧 + 系统层置前浏览器窗口 |
| `focus-recent` | 无连接，但最近有标签（跨内核重启记得、含标签被冻结） | 只置前 + 推 focus，等它重连（`RECONNECT_GRACE_SECONDS`）；**绝不开新窗** |
| `open` | 确实没有标签，或同一次打开窗口内的**第二次点击** | 才 `webbrowser.open` |

**标签存在性信号**（`src/ui/web_tab_presence.py`，落盘 `<workspace_home>/runtime/web_tab_presence.json`）：

- 信号源：WS 连接建立、前端 HTTP 心跳（`src/ui/web/src/lib/tabPresence.ts`，可见 30s / 隐藏 60s，页面重新可见、窗口获得焦点、bfcache 恢复时立刻补一次）、标签关闭时的 `bye`（`navigator.sendBeacon`）
- 判定：`last_seen_at > last_bye_at` 且未超 `FRESH_TTL_SECONDS`（90s）。`bye` 只记时间不抹 `last_seen` —— 多标签场景下另一个标签的心跳仍会让它保持新鲜
- 「真的关了浏览器」的兜底：`bye` 立刻置为不新鲜；漏掉 `bye`（崩溃/强杀）时靠 TTL 过期；TTL 内再点一次（`SECOND_CLICK_WINDOW_SECONDS`，15s）直接开新窗

**唯一入口**：`GET /api/ui/open`（判定+动作都在内核侧；托盘、协议启动器、外进程一律走它）。`GET /api/ui/focus` 保留旧语义（`focused` = 是否找到并唤起已有标签，永不开窗）。`open_or_focus_web_ui()` 在内核不可达时才本地回退打开。

回归：`tests/test_ui/test_web_tab_presence.py` · `tests/test_ui/test_web_ui_open_window.py`。
