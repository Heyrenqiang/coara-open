# coara CLI 展示框架

> 本文是 CLI 终端展示的架构说明。实现入口：`src/cli/display_controller.py`（attach 版 CLI：`src/cli/attached_chat_runner.py`）。
> 08-30 内核化后：CLI 是 attach 客户端，经 `/ws/attach` WebSocket 收内核事件流（`src/cli/attach_transport.py` + `remote_event_bus.py`），不再是进程内直收 yield；展示逻辑不变。

---

## 1. 设计目标

coara 运行时产生两类用户可见信号：

| 信号类型 | 产生方 | 典型内容 |
|----------|--------|----------|
| **Turn 流** | 内核 `CoaraBase.process_message()` → `run_turn_loop` 逐块 **yield** → EndChannel 投递 | assistant 正文、`✓ tool(...)`、`✗ tool 报错: …` |
| **Trace 事件** | `_emit_trace()` → `EventBus.publish()` → 端事件流 | `tool_start/complete`、`subagent_*`、`todo_update`、`background_*` 等 |

CLI 把它们变成终端上的**滚动历史**与**输入框上方的动态区**，拆成三条并行通道（§3）。

斜杠命令与离线 CLI 的文案约定（中文优先、不展示内部 id）见 [`CONVENTIONS.md`](./CONVENTIONS.md) §用户可见文案。

---

## 2. 端到端数据流

```text
内核                              信号                CLI 展示（Layer 3）
─────────                        ─────────           ─────────────────────────────
process_message ─┐
  └ turn_orchestrator ────► yield 文本块 ──EndChannel─► 通道 A：StreamingBlock → 滚动历史
  └ executor ─────────────► tool_start/complete ─────► 通道 B：活动树 → 动态区
  └ delegate ─────────────► subagent_* ─────────────► 通道 C：事件直打 → 滚动历史

端接入：内核 EndRegistry 按 (source, session_id) 路由正文；CLI 经 attach_ws 收流，
        Web / attach 的 trace_batch 在 ``trace_broadcast`` **发送端**按 source /
        共看同空间过滤（主门），客户端 ``cli_shows_source`` 只是兜底——同一份
        EventBus 进总线，出站按端投递，显示互不影响。

EventBus 的其它订阅者（与 CLI 并行）：TraceStore（JSONL 落盘）、WebSocket 推送（聊天流式 + 工具侧栏）
```

---

## 3. 三通道模型（核心）

### 通道 A — Turn 流 → 滚动历史

| 环节 | 文件 |
|------|------|
| 收集 | `session._collect_chat_turn_streaming` |
| 缓冲 | `BackgroundSpinner.append_streaming_chunk` → `StreamingBlock` |
| 落盘 | `StreamingBlock` → `CliScrollback` |

yield 规则（`turn_orchestrator`）：

- 本轮**无 tool_calls** → yield assistant 最终正文，回合结束。
- 本轮**有 tool_calls** → **先 yield** 本轮 assistant 文字预览，再执行工具；每个工具完成后 yield `✓ <摘要>`，出错时 yield `✗ <工具名> 报错: …`。
- `delegate(background=true)` 的结果行**不** yield `✓`（后台任务经完成通知回传）。
- continuation 注入：用户跟话（CLI / Matrix / Web 任一来源）进模型队列后，**排队期间在动态区 spinner 上方逐条显示** ``→ 正文`` 行（一条一行，从上到下排队；`input_queue_display.pending_input_hint_lines`），**注入时回显一次**滚动区青色 ``你：`` + 正文（`continuation_input_injected` 事件，与普通用户输入同款正式输出），排队的该条随之消失——回显输出把终端推到底部，新提示符重新锚定。前台子智能体完成 / `report` 推送进模型队列，并在滚动区回显 ``{type}子智能体： [task_id] …``（type 从 `sa-{type}-…` 解析）。其它系统/事件注入（后台完成提醒等）不进排队提示、不回显为用户消息。

### 通道 B — Trace → 动态区（输入框上方）

| 环节 | 文件 |
|------|------|
| 活动树 | `ActivityLiveTracker`（`src/cli/activity_live.py`），由 `SubagentSpinnerManager` 薄封装 |
| 渲染 | `BackgroundSpinner.__call__` → `get_status_lines()` |
| 刷新 | `BackgroundSpinner` 内部循环约 12.5Hz（0.08s），`_dirty` 合并 |

规则：

- `tool_start` 建节点；`tool_complete` 标记完成并从动态树清除。
- `subagent_start` 通过 `parent_tool_call_id` 挂到对应 delegate 节点下；子工具经 `coara_id` 关联。
- delegate 节点直接展示其下子工具；运行中的 delegate 单行展示为 ``delegate coaras: 描述…  (12s · 2.8k tok · cache 80%)``。
- 子智能体的 `thinking_progress` **不进动态区**（内心独白不是状态，多分身共享展示槽会互相覆盖）；Root 的思考文字走通道 A。
- 动态区总上限 **12 行**（`MAX_STATUS_TOTAL_LINES`），超出折叠为 overflow 提示；单个 delegate 子树最多 **4** 个活跃工具（`MAX_ACTIVE_TOOLS_PER_DELEGATE`），其余折叠。
- **janitor / daily**：不进动态区、不 flush ✓ 到滚动区、不弹完成摘要（`activity_live` 静默子树），仅空闲「后台」行保留。
  `aide` 是普通子智能体，正常显示——静默集真源是 `CLI_SILENT_SUBAGENT_TYPES`（`{janitor, daily}`）。
- **后台任务指示**：`TaskStore` 只读快照 → 底部工具栏「后台 N」；空闲时输入框上方一行摘要（最多 3 个任务标签）。与 turn 内排队跟话（`input_queue_display` 的 ``→ …`` 行）是两个独立槽位，互不重叠。

### 通道 C — Trace → 事件直打（滚动历史）

由 `CliDisplayController` 统一路由：

| 事件 | 展示 | 说明 |
|------|------|------|
| `tool_complete`（子智能体工具） | 立即 flush 到 `CliScrollback` | 子智能体工具的完成行不进 turn yield，靠这里补 |
| `tool_complete`（带 `display_blocks`，子智能体） | 先 flush ✓ 行，再 diff 面板 | 订阅顺序：activity → spinner flush → terminal |
| 前台 session `display_blocks` | `✓ tool(...)` yield 之后再 diff | 由 `turn_orchestrator` 在通道 A 画，避免 diff 跑到工具行前面 |
| `todo_update` | （CLI 不展示） | 仍由 executor 发出；见 [`TODO_SYSTEM.md`](./TODO_SYSTEM.md) |
| `background_agent_complete` | 滚动区摘要（跳过 janitor / daily） | 后台任务不占用 turn yield |
| `output_truncation_recovery` | 滚动区黄字通知 | 系统提醒 |
| `shell_output_matched` | （不再 CLI 显示/注入） | 已并入 `background_task_complete` 完工 `系统提醒`；Web 活动区仍可见 |
| `continuation_input_received` | （不回显；仅重绘 spinner） | 用户跟话入队时静默，等注入时由 `continuation_input_injected` 回显一次 |
| `continuation_input_injected` | 回显块：青色 ``你：`` + 用户跟话 / ``{type}子智能体：`` + 子智能体结果（本地 CLI 来源；matrix/web/event 已由 remote_sync 或其自有通道呈现 跳过避免双显） | 经 `BackgroundSpinner.write_echo_lines` 进当前流式块渲染（见 §3.5） |

### 3.5 滚动区间距模型（StreamingBlock 唯一裁判）

块型：``T`` 正文文本块 / ``✓`` 工具行 / ``D`` diff 块 / ``E`` 回显块（你：/子智能体：）。规则：

- 全块型两两交界**恰好一个空行**（T↔✓、T↔E、✓↔E、注入↔任何）
- 唯二例外：连续 ``✓`` 工具行紧贴无空行；``D`` 紧贴自己的 ``✓ edit/write`` 行（尾部仍一空行）
- ``E`` 上下各一空行 且与相邻块的块首空行复用不叠加（块内 已消费 标记）
- 顺序：所有块（含 ``E``）经同一个流式块按到达先后落滚动区 **禁止旁路直写**（旁路曾把回显插到排队 ✓ 前 已修）
- ``D`` 内部：变更行通栏增绿/删红 gutter 行号与 +- 染色 头部 路径 +N -N 无框无虚线；以变更行为中心 上下各取最多 3 个非空上下文行 遇空行即停 且每个变更块上下互取小值对称；整块首行前导语境与末行尾随语境再互取小值（多变更块同块时首块上方与末块下方也能对称）；变更区内部空行不显示；滚动区按屏幕行预算 head+tail 截断后，对**可见行**再跑一次同一套对称裁剪（避免上语境被省略、下语境仍露出）

### 3.6 多通道分流

- CLI：本空间前台全量 + 手机/Web 主会话对话镜像（带 ``[端]`` 标签）+ 跨空间收尾投递
- WebUI：自己发起的回合 + 注入它处回合的回投镜像
- 手机：房间时间线
- 模块会话（flow/config 等 ``web-*``）：只在 WebUI 三端互不见
| `event_turn_complete` | flush 残留子工具行 + 清 Root 状态 | 入站事件 turn 不经 `chat_runner.finish_turn`，这里兜底 |
| `session_auto_new` | 滚动区提示「N 分钟无新对话，已自动开启新会话」 | 空闲自动 /new |

**子智能体工具 flush 判定**（`src/cli/activity_types.py` 的 `should_flush_tool_to_history`）：

- `depth == 0`（Root 自己的工具）→ 不 flush（走通道 A yield）。
- `depth == 1` 且为 delegate / 非工具型活动节点 → 不 flush。
- 其余（depth ≥ 2 的子智能体工具）→ flush；**去向**见 §3.7（有 delegate 归属 → 折叠块，无 → `✓ [coaras] web_search(...)` 落滚动区）。
- 非工具型活动节点（`subagent` / `background` / `workflow` / `process`）任何 depth 都不 flush。

### 3.7 子智能体折叠块（CLI 端口径对齐 web 折叠区）

一次 delegate（子智能体）运行**默认只在滚动区留一行摘要**；任务指令 / 过程 / 最终结果 / diff 四类全部折进块里。
**Ctrl+O** 在输入区上方可重绘层展开/收起（有已展开块时优先收起；收起即从该层消失，不往滚动区刷提醒）；
**`/detail [关键词]`** 按**类型**（`coaras`/`aide`）或**任务摘要**定位某一块，把明细落进滚动区永久回看
（无参＝最近一块；多命中取最近；未命中给一行黄字提示）。实现 `src/cli/subagent_fold.py`，数据装配在
`display_controller`（`_on_fold_event` / `_on_subagent_terminal` / `ingest_tool_block` /
`ingest_subagent_frame` / `queue_diff_frame`），过程行的分流在 `spinner.flush_finished_tool_history`。

组序与 web 一致：**任务指令 → 过程（工具行 + 过程正文 + diff）→ 最终结果**，空组不出现。

| 内容 | 数据来源（既有帧/事件，不加端上帧字段） |
|------|----------------------------------------|
| 块 key / 归属 | `tool_start`(delegate spawn/resume，call_id) · `subagent_start` · `background_agent_start`（后两者登记 subagent_id / coara_id / task_id 别名） |
| 任务指令 | `user_message` 帧 `delegate_brief` + `parent_tool_call_id` |
| 过程工具行 | 活动树完成块（`delegate_owner_for_block` 在 `_finish` 时算好归属；节点树会 prune，不能事后回查） |
| 过程正文 | attach `subagent_chunk` 帧（`tool_call_id` = 发起它的 delegate 行 call_id） |
| diff | attach `diff` 帧（带 `parent_tool_call_id`，不再落滚动区） |
| 最终结果 | attach `subagent_result` 帧；缺帧时回退 `[前台子智能体已完成]` 回显行 |
| 终态 / 耗时 | `subagent_complete` / `subagent_failed` / `background_agent_complete` |

摘要行：`▸ {类型}子智能体 · {任务摘要} · {N} 项工具 · {完成|失败|运行中} · {耗时}`（缺值退化：
`子智能体` / `（任务未记录）` / `0 项工具`；后台任务带「后台」前缀；明细缓冲溢出过则追加
`· 可回看最近 N 块`）。

边界与已知近似：

- 折叠只是不打到滚动区，**明细不丢**——本地有界环形缓冲留最近 `DETAIL_CAPACITY`（24）块，超出丢最旧并标注范围；
  更长历史靠录像带 / spill 的 `tool_outputs` 按 call_id 回读（二期，本轮不做）。
- **Ctrl+O 真展开/收起**：明细画在 prompt 上方可重绘层（与 Thinking/活动树同层）；收起重绘后消失。
  终端滚动区仍是追加式的——不要把 Ctrl+O 明细打进滚动区。永久回看用 `/detail [关键词]`
  （定位一块后落滚动区：`print_fold_detail` 只写滚动区、**不置** `expanded`，否则 overlay 与滚动区双显）。
  `/detail` 的关键词带 ↑↓ 补全（`SlashOptionPicker`，候选＝各块摘要，最近在前）。
- delegate 自己的 `✓/✗` 行不再上屏（摘要行取代它）：`DelegateLineFilter` 按形态判定挡 `delegate wait`
  与 spawn/resume 形态，`delegate message/stop` 保持可见。判定按形态而非「已知 call_id 标签」——`tool_start`
  走 100ms 微批、✓ 行走即时通道，快工具会先到。
- **子智能体 diff 的帧不带 `tool_call_id`**（`_attach_output_frame` 只透出 display_blocks / diff_lines /
  tool_name / parent_tool_call_id）→ 过程组里按「工具名 + 从后往前」回贴到对应工具行，配不上的排末尾（不丢）。
  这是刻意的近似，精确钉法待帧携带 call_id。Ctrl+O 叠层里 diff 以 `[diff]` 占位（完整 diff 看 `/detail`）。
- janitor / daily 完全静默策略不变（`CLI_SILENT_SUBAGENT_TYPES` 不建块）；`<子智能体消息>`（report 推送）
  无块锚点，保持原样回显。
- 折叠块随进程存活（不随 `/new` 或切空间清空）——它只是本地回看缓存。

---

## 4. 展示策略表

| 内容 | 滚动历史 | 动态区 | 来源 |
|------|:--------:|:------:|------|
| Root `✓ tool` | ✓ | — | 通道 A yield |
| 子智能体 `✓ tool` | — | 进行中时 ✓ | 完成后进 delegate 折叠块（§3.7）+ 通道 B |
| delegate 摘要行 | 一行（唯一默认产出） | ✓ 单行 + 子工具 | §3.7 折叠块；Ctrl+O 展开、`/detail [关键词]` 落滚动区 |
| assistant 最终回复 | ✓ | 末行 pending | 通道 A |
| assistant 文字 + 同轮 tool_calls | ✓（执行工具前 yield） | — | 通道 A |
| delegate 运行中 | — | ✓ 单行 + 子工具 | 通道 B |
| 待办清单 | — | — | CLI 不渲染；工具与落盘仍在 |
| 后台任务（bash / agent） | 完成摘要 ✓ | 空闲提示行 + 底部「后台 N」 | TaskStore 只读快照 |
| aide 子智能体 | 与普通子智能体同显示 | 同左 | CLI 静默集只有 janitor / daily，aide 不再抑制 |
| janitor 概况+记忆 | — | 仅空闲「后台」行 | 工具树/✓/完成摘要抑制 |
| daily 日常整理 | — | 仅空闲「后台」行 | 同上；digest 次日 CLI/Matrix 显示（不进上下文）；SYSTEM_ONLY |
| 手机（Matrix）发起的本空间回合 | ✓ 实时镜像 | — | `remote_sync_display`（event/background 唤醒同） |
| Web 主会话发起的本空间回合 | ✓ 实时镜像 | ✓ Thinking/活动树同前台 | 与手机端同待遇：`remote_sync_display` 镜像对话 + spinner chrome；输入在活跃回合时注入接续队列并登记回投镜像（回复流回 Web 客户端） |
| 跨空间回合（含切空间后离开的） | 收尾投最终答复全文：带空间色边框块（块体缩进两格），块内带 `[空间·端]` 标签；回合失败必显 `[空间名] 回合失败：…` | spinner 行内后台段（见 §4.1） | `remote_sync_display`；过程不上屏，防多流交织；失败只认主会话 `turn_failed`（子智能体失败不算） |
| 模块会话（flow/config 等，source=web-*） | — | — | CLI 零显示；Web 自维护自己的显示通道，与 CLI 完全独立 |

### 4.1 多工作空间显示框架（唯一 spinner 行）

多空间并发时，CLI 不给每个空间单独成行——后台空间的运转状态并入**唯一 spinner 行**（前台 Thinking 行）：

```text
⠼ 文案 前台计时 [nx] 等待模型 2m58s [gora] 正在运行 shell 40s
```

| 方面 | 规则 | 代码 |
|------|------|------|
| 数据源 | `WorkspaceActivityRegistry`：事件驱动归集各空间活跃状态 + 渲染时 reconcile 兜底校正 | `src/cli/workspace_activity.py` |
| 事件双路 | 同一批活动事件：活动树只吃**前台**空间（`_event_in_foreground_workspace` 按 payload `workspace_dir` 判别），登记表**全量**归集；另订阅 `llm_request_start` / `completed` / `turn_failed` / `turn_interrupted` 喂登记表 | `display_controller.py` `_ACTIVITY_TREE_TOPICS` / `_WORKSPACE_REGISTRY_TOPICS` |
| 行结构 | 后台空间段（`[空间名] 动作摘要 计时`）空格相连跟在「⠼ 文案 前台计时」之后，绝不换行；整行青蓝 thinking 样式，`[空间名]` 标签保留空间色做辨识 | `spinner.py` `_background_ws_segments` |
| 前台空间 | 不建行——Thinking 行本身就是前台 spinner；单空间（无后台段）时保持原样 | `spinner.py` `__call__` |
| 前台空闲 | 前台无回合但后台在跑时，该行只显示后台段 | 同上 |
| 空间色 | 按 workspace key（resolve 后目录路径）md5 哈希取 8 组亮暗对色板，跨进程稳定（内置 `hash()` 对 str 按进程随机化，不可用）；前台用亮色，后台段用暗色 | `workspace_activity.py` `workspace_color` |
| 动作摘要 | `tool_start` →「正在运行 \<工具\>」、`llm_request_start` →「等待模型」、子智能体 →「子智能体 ×N」；摘要截 14 宽，只在行内滚动更新，**不进滚动区** | `workspace_activity.py` `ingest` |
| reconcile | 每次渲染从 `root._sessions` + `has_active_turn()` 校正（防事件丢失后行不消失/不出现），2s 宽限避免与事件流互相打架造成行闪烁；后台行按最近事件时间倒序 | `workspace_activity.py` `reconcile` / `_STALE_GRACE_S` |
| 主会话门控 | 只有工作空间**主会话**的回合结束事件（`completed` / `turn_failed` / `turn_interrupted`）才收行（子智能体回合也发 `completed`，不能误收）；主会话身份靠 `root._sessions` 确认，确认不了留给 reconcile 兜底 | `workspace_activity.py` `_is_main_session_event` |
| 静默子智能体 | `CLI_SILENT_SUBAGENT_TYPES`（janitor / daily）的活动不建行——系统管家不占状态位 | `workspace_activity.py` `ingest`；`builtin_agents.py` |

**切空间与 detach**：回合中途切走时，`iter_while_foreground` 以 anext + 0.2s 轮询检查前台状态（`_DETACH_POLL_INTERVAL`）——静默回合（长工具/等模型首包）也在 0.2s 内 detach，主循环不再被旧回合流钉死；detach 后原空间回合并入上述 spinner 行后台段，收尾由 `remote_sync_display` 色框块投递。取消路径补发 `on_drain_complete`；切回重挂经 `ensure_streaming` 自愈（`src/coara/turn_detach.py`）。

---

## 5. 模块职责

| 模块 | 职责 | 不应负责 |
|------|------|----------|
| `turn_orchestrator` | ReAct 循环、Root 工具 yield | 子智能体实时 UI |
| `executor` | 执行工具、发 `tool_start/complete/todo_update` | 终端渲染 |
| `delegate` | 子智能体生命周期 trace | 子智能体 yield 到父 CLI |
| `event_bus` | 多订阅分发 | 展示策略 |
| `ActivityLiveTracker` | trace → 活动树（通道 B/C 的语义源） | 渲染 |
| `SubagentSpinnerManager` | `ActivityLiveTracker` 的薄封装 | 滚动历史 |
| `WorkspaceActivityRegistry` | 多空间活跃状态登记（事件驱动 + reconcile）→ spinner 行内后台段 | 渲染 |
| `BackgroundSpinner` | 动态 prompt、流式块、工具栏、flush 执行 | EventBus 订阅 |
| **`CliDisplayController`** | **订阅 EventBus、turn 生命周期、策略路由** | turn 逻辑 |
| `chat_runner` | 会话主循环（调用 begin/finish_turn） | 事件 handler |
| `matrix/response_stream` | 消费 yield 发房间消息 | spinner |

### 5.1 工具栏与 chrome 同步

CLI 的底部工具栏 / 动态区遵循两条规则：

| 规则 | 做法 |
|------|------|
| **Live-read** | 工具栏的 provider / model / 工作空间名 / 计划模式在每次绘制时读 `root.foreground_coara` |
| **事件失效** | `llm_switched` → 重绘工具栏；`workspace_switched` / `session_started` → `reset_after_runtime_change`（清暂停 / 流式块 / 事件回合标等残留 chrome + `sync_foreground_chrome` 重绑活动树会话、清后台任务缓存） |

底部工具栏三栏（左 provider·model / 中工作空间 / 右 context+cache%）由 `src/cli/terminal_width.py` 按**终端显示列宽**排布（CJK/emoji 常占 2 列，不能用 `len()`）。空间不够时：先截中间工作空间名，再压缩右侧 context 细节，**始终保留** `cache N%`，避免窄终端把 `cache 93%` 裁成 `cache 9`。cache 命中率来自上一轮 provider usage（`LlmUsageSnapshot.cache_hit_ratio` → `prompt_cache_hit_ratio`）。

`/thinking` `/sandbox` 这类不在工具栏上的开关不走 chrome 同步。

---

## 6. Turn 生命周期（CLI 视角）

```text
用户输入
  → display.begin_turn()                # 开始通道 A 流式
  → async for chunk in process_message
       → append_streaming_chunk(chunk)  # 通道 A
  → display.finish_turn()               # 停止流式 + flush 子工具残留行 + 清 Root 状态
  → （若打断）同样 finish_turn；打断不回滚本轮历史（未完成 tool_calls 收口为「已取消」+ 注入「当前会话已打断」），整轮回滚仅限未捕获异常，见 COARA_ARCHITECTURE.md §六
```

**前台 delegate 时间线**（前台 delegate 是异步的，`execute()` 只负责启动并立即返回占位符）：

```text
Root iteration N: tool_calls = [delegate(coaras)]
  execute(): asyncio.create_task 启动 coaras → 立即返回占位符
  通道 A: yield ✓ delegate(...)（占位符也是正常工具结果）
  通道 B: delegate 行 (耗时·token) + coaras 子工具轮转
  通道 C: coaras 每个子工具 complete → ✓ [coaras] … 滚入历史
  coaras 完成 → 结果注入 continuation 队列（进模型）+ 滚动区 ``coaras子智能体： [task_id] …``
  report() 途中消息同路径：模型 + ``{type}子智能体：`` 回显（普通中间文字不转发）
LLM 无 tool_calls 但前台 delegate 未结束 → 轮次不退出，
  asyncio.wait(FIRST_COMPLETED) 等首个结果回来 → drain → LLM 继续推理
```

机制细节（并发上限、取消传播、后台模式）见 [`子智能体重构.md`](./子智能体重构.md) §前台/后台异步委托。

---

## 7. 与 TraceStore / Web 的关系

- **TraceStore**：全量落盘 `traces/trace_events.jsonl`；CLI 订阅同一 EventBus。
- **Web**：无 Trace 复盘页；右侧工具活动侧栏吃 `trace_batch` + `/api/trace/events?kinds=tool`。
- CLI `ActivityLiveTracker` 用 `workspace_trace_event` 做内存投影（不写 `activity/`）。
- 调试单个 LLM 请求：独立开发者工具 `coara-devtools`（`python -m src.devtools`，默认 8090），读各工作空间 `.coara/llm/llm-calls.jsonl` 磁盘镜像（每实例一行全文，含 system prompt / 工具 Schema / 对话 / 响应），按工作空间 × 智能体看最后一轮。delegate 行的 tok 显示是 context 口径（最后一轮 prompt，与主会话侧栏一致），不是跨轮累计。

---

## 8. Matrix 模式差异

| 项 | 本地 CLI | Matrix |
|----|----------|--------|
| 输入 | `prompt_toolkit` | 房间消息 |
| yield 消费 | StreamingBlock + spinner | `stream_coara_reply_to_matrix` |
| `✓ tool` 行 | 通道 A + C | 默认不发房间（CLI 宿主机可经 `echo_tool_summary_local` 本地回显） |
| spinner | 有 | CLI 宿主机仍显示 |

---

## 9. 扩展新展示行为的流程

1. **确定信号来源**：yield 还是 trace？若仅 trace，Dashboard 与 CLI 是否都要？
2. **查 §4 策略表**：应进滚动历史、动态区，还是静默？
3. **在 `display_controller.py` 加 handler**（或扩展其订阅表），不要在 `chat_runner` 里加裸 `console.print`。
4. **子智能体长任务**：走通道 C flush 或通道 B 心跳，不能假设通道 A 会 yield。
5. **加测试**：`tests/test_cli/` 下的 spinner / display_controller 单测。

---

## 10. 文件索引

```text
src/cli/
  display_controller.py        # 展示路由中枢（EventBus 订阅 + turn 生命周期）
  activity_live.py             # ActivityLiveTracker（通道 B/C 活动树）
  activity_types.py            # ToolCallBlock + flush 规则 + 动态区行数常量
  scrollback.py                # CliScrollback 统一滚动输出（通道 A + C）
  spinner.py                   # SubagentSpinnerManager + BackgroundSpinner
  workspace_activity.py        # WorkspaceActivityRegistry（多空间活跃登记 + 空间色）
  remote_sync_display.py       # 跨端/跨空间镜像与收尾投递（色框块、turn_failed 必显）
  background_tasks_display.py  # TaskStore 只读 → 后台任务工具栏 / 空闲提示
  input_queue_display.py       # turn 内排队消息提示
  streaming.py                 # StreamingBlock（通道 A 缓冲）
  session.py                   # turn 收集 / SIGINT
  chat_runner.py               # 会话主循环
  interactive_prompt.py        # 弹窗问答

src/coara/
  turn_orchestrator.py         # yield 规则
  base.py                      # process_message 入口
  event_bus.py                 # 分发

src/agent/executor.py                  # tool_start/complete/todo_update
src/tools/builtin/delegate/delegate.py # subagent_* 生命周期

src/ui/
  activity_store.py            # CLI 活动投影（内存；不落盘）
  trace_store.py             # 持久化
```

**对照：** Web 浏览器聊天区的 hydrate/replay 与多端隔离见 [`WEB_DISPLAY.md`](./WEB_DISPLAY.md)。
