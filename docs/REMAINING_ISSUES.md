# Coara V8 剩余问题清单

> 本文是仓库唯一在册的问题台账，逐条列出**当前仍存在**的问题：现象、源码位置、影响、修复方案与复杂度（S=局部小改 / M=跨模块 / L=跨端或新功能）。
>
> 编号 #1–#N 按当前问题清单连续编排，仅记录当前仍存在的问题；已修复/已裁定的条目不再保留。
>
> 2026-08-14 全系统审计（六路并行 105 项发现）后大清理：**当日已修复 52 项**（含 P0 全部数据丢失级与 P1 安全主要项）；审计原始报告见 ref-doc/全系统审计-2026-08-14.md 存档段落。
>
> 2026-08-16 工作流内核化战役：全切 kernel（WdlRunner/wdl_legacy/迁移脚本/dsl_parser/qc_agent/approval_html 及 wdl 解析栈删除），随删出册 #11/#15/#16/#17/#19 共 5 条；同轮修复的存量 bug（YAML on 布尔陷阱、prune 时区混用、kernel 实例行缺失）见文末战役记录。
>
> 2026-08-20 修复战役（九路并行）：**P2 全部 18 条出册**、P3 五条出册、human_approval 死路径整条拆除、vault 明文过渡缓解落地、CLI 输入卡顿根治（httpx 默认构造同步加载 CA 实测 570ms）、工作空间切换两 bug 同根修复；版本号策略出册（全端 1.0.0 同源）、git 历史凭据决策挪至 manual/18 §18.9；详见文末战役记录。
>
> 2026-08-20（深夜）三项拍板落地：Matrix owner 名单已配置（`security.owner_matrix_ids: ["@phone:coara.local"]`，原 #2 出册）；i18n 判死出册（中文优先单用户产品，不做国际化框架）；vault 挑战-响应协议正式立项（见 #1）。
>
> 2026-09-09 全仓复审（三路并行扫描 + 台账逐条复核）：**出册 3 条**——原 #7 错误恢复指引（已由 `_user_facing_llm_error`/`_user_facing_turn_error` 集中映射落地）、原 #8 唤醒回合不回 Matrix 发起端（`_mirror_awakened_reply` 已闭合，见战役记录）、原 #17 matrix homeserver 探测覆盖 .env（已加占位符/迁移场景护栏）；**数字更新**——getattr 防御 32→60 处（扩散）、吞异常 54→49 处；**新增 7 条**（#7 端/通道状态载体并行、#11 计费与通知静默吞、#21 provider 包装样板、#22 前端类型漂移与死 action、#23 fire-and-forget 任务引用、#24 registry docstring 失实、#26 仓库磁盘卫生）；全仓编号重排。方法与明细见文末战役记录。
>
> 2026-09-11 web 端显示一致性改造：**新增 9 条**（#27–#35，见「本轮遗留」节）。改造把「端上显示＝服务端权威（视图带 + 此刻 runtime）」立成硬约束并落地（帧带归属、`view_seq` 对账、原子提交、折叠区契约、standby 流），但游标口径、条数常量、线重置、兜底路径与补注入判据留下若干缺口，逐条登记。
>
> 面向用户的安全已知限制汇总见 [`manual/18-安全与治理.md`](./manual/18-安全与治理.md) §18.9。

---

## P0：需先拍板的决策项（1 项）

### #1 vault 密码明文经 Matrix 传输（高，唯一 L 级；过渡缓解已落地）

- **过渡缓解（2026-08-20 已做）**：gomatrix 新增 vault 回信清扫（`internal/service/vault_sweep.go`）——启动即清历史库中的 `[COARA_VAULT_REPLY]` 明文，之后每分钟清理保留窗（5 分钟）外的回信；新明文最多留存 5 分钟（Python `vault_bridge` 60s 解锁等待窗口不受影响）。残留：投递窗口内仍落库 + SQLite 空闲页/WAL 可能有字节级残留。
- **剩余（L）**：传输层仍明文——unlock 改挑战-响应（服务端发一次性 challenge，客户端 HMAC 上送）；setup（首创，无 verifier 可校验）单独协议；旧 App 兼容期双协议并存；跨 Android/gomatrix/vault_bridge/vault service 四端。
- **推荐**：已立项（2026-08-20 拍板）——unlock 改挑战-响应（服务端发一次性 challenge，客户端 HMAC 上送）；setup（首创，无 verifier 可校验）单独协议；旧 App 兼容期双协议并存；跨 Android/gomatrix/vault_bridge/vault service 四端。
- **注**：2026-08-14 已修 vault 改密跨进程互斥；2026-08-20 已修双进程同时解锁竞态（O_EXCL 原子占位）。

---

## P1：仍存在的问题（7 条）

### #2 动态属性访问（getattr 防御）残留（高；较上次审计扩散）

- **位置**：全仓 `getattr(self, "_…", default)` 模式现有 **60 处**（2026-09-09 复测）：`src/coara/base.py` 24、`src/cli/root_shim.py` 8、`src/ui/web_server.py` 8、`src/llm/_http_provider.py` 6、`src/coara/root.py` 3、`src/ui/handlers/workspace.py` 4 等；旧单引号风格 `getattr(self, '_…'` 已清零（仅 base.py:708 残留 1 处）
- **问题**：访问未在 `__init__` 声明的属性；类型检查/IDE 补全失效，拼写错误不被捕获。数字较上次（32）上升，扩散来自新代码沿用该风格
- **方案**：`__init__` 声明全部实例属性；turn-scoped 属性用 TurnRuntime dataclass；新代码禁止再引入。M。

### #3 裸 except Exception: pass 吞异常（高；54→49，部分缓解）

- **位置**：多行模式现有 **49 处**（2026-09-09 复测）。重灾区：`src/ui/attach_ws.py` 5、`src/cli/main.py` 3；计费/通知路径点名（不可观测代价最高）：`src/runtime/usage_collector.py`、`src/runtime/usage_attribution.py`（用量/费用统计失败无日志）、`src/coara/base.py:476`（后台完成通知写入 updates store 失败被静默——与上方 446 行「不静默消失」注释直接矛盾）、LLM 流关闭 `close_exc` 捕获后不记录（`src/llm/openai.py:527`、`src/llm/responses.py:452`、`src/llm/anthropic.py:667,691,709`）、`src/coara/continuation_leftover.py:150`（matrix 房间解析失败静默降级）
- **问题**：静默吞异常不记日志，故障不可观测；计费与后台通知两处丢失即用户可感知
- **方案**：至少加 `logger.debug(..., exc_info=True)`；计费/通知两处至少 warning；预期异常用具体类型。M。

### #4 CoaraBase 职责过重（God Object）

- **位置**：`src/coara/base.py`（1500+ 行），12+ 职责（消息/LLM/工具/技能/子智能体/会话/持久化/Trace/中断/Plan/上下文/流式）
- **问题**：修改任何关注点都可能影响其他；测试需 mock 整个基类
- **方案**：提取 ForegroundDelegateTracker、SessionStateManager、ToolBootstrapMixin 等。L。

### #5 RootCoara 的 janitor 编排逻辑不应在 Root

- **位置**：`src/coara/root.py`（1270 行）`_janitor_startup_scan`/`_janitor_finalize_pending`/`_janitor_maybe_renew`/`_janitor_scan_expired`
- **方案**：提取 JanitorScheduler 组件。M。

### #6 依赖版本范围过宽

- **位置**：`pyproject.toml:12-67` 全部 `>=`（anthropic/openai/pydantic 等）；无 requirements.lock/uv.lock/poetry.lock（2026-09-09 复核仍无变化）
- **影响**：不同环境行为不一致；上游 breaking change 直接影响生产；无法可复现构建
- **方案**：pip-compile/uv pip compile 生成 lock 文件锁精确版本。M。

### #7 端/通道状态载体并行 + 端来源分类双谓词（2026-09-09 新增）

- **位置**：同一「当前端/通道」概念由 3–4 种状态载体共同表达——`src/coara/base.py:226,512,697,789`（旧 `session_origin` 字典）、`src/coara/turn_context.py:26,48,73`（`EndChannel` ContextVar）、`_active_turn_source`（base.py 多处）、`src/coara/segment.py:28`（`SegmentTracker.source/channel_id`）；另有来源分类双谓词——`src/coara/continuation_leftover.py:26,31,35`（`is_web_source/is_matrix_source/is_cli_source`）与 `src/coara/turn_source.py`（自称「单一事实源」的 `cli_shows_source/web_shows_source/should_push_matrix`）
- **问题**：谓词语义已漂移（`is_matrix_source` 仅认 `"matrix"`，`should_push_matrix` 还含 `"event"`；`cli-` 前缀处理范围不同）；某条路径只更新载体之一而漏改其余时，输出路由/可见性不一致——channel_id 端内路由上线后此风险被放大
- **方案**：判定谓词收敛进 turn_source.py 单一事实源（continuation_leftover 改为转发引用）；状态载体收敛到 Segment + EndChannel 两套并文档化各自职责。M。

### #8 无错误恢复指引的 provider 侧残留（2026-09-09 新增，收窄自原 #7）

- **现状**：用户可见层已解决——`turn_orchestrator.py:172,226` 的 `_user_facing_llm_error`/`_user_facing_turn_error` 集中映射（401/402/403/429/超时/空响应等均给中文恢复指引）；剩余为 provider 层仍抛技术性 `LLMError` 字符串（`src/llm/anthropic.py:553`、`src/llm/openai.py:535`），靠集中映射兜底
- **方案**：低优先——provider 侧维持技术串即可，无需逐 provider 写文案；仅当集中映射漏掉新错误类别时补映射分支。S。

---

## 待评估（3 条）

### #9 [P2] 事件投递机制边界模糊

- **现状**：EventBus vs UnifiedScheduler 的判定规则在 AGENTS.md 有文档，但代码中多处边界模糊
- **建议**：引入 StateChangeBus（状态变更）与 NotificationBus（纯通知）分离

### #10 [P1] 配置加载链复杂且重复

- **位置**：`src/core/config.py`
- **问题**：单次 `load()` 入口（:210）但内部多趟解析：`_iter_yaml_load_paths`（:182）先合并一遍 YAML 路径再 `_iter_config_yaml_paths(merged)` 二次枚举、`_load_env`（:369）、`_load_llm_preferences_files`（:390）、`_load_providers`（:431）/`_load_llm_profiles`（:434）独立 load。深度合并逻辑复杂
- **注**：2026-08-14 已修写入优先级（补 users/default 层）；2026-09-09 复核加载链较台账原述略降但仍未根本简化
- **建议**：
  1. 简化加载链：一次 load + 一次合并
  2. 启动时打印生效配置摘要（mask 后）
  3. `coara config show` 命令显示当前生效值

### #11 [P2] 回合终态归类口径三处跟进（2026-08-20 评审发现）

- 上下文超限（guard.should_block）/ 停滞停止 / 迭代上限三条路径都置 `CoaraStatus.FAILED` 但不记 `turn_failure`，session_log 仍记 completed——同类既有口径问题（显式标记制度建立后暴露，非回归）；这些路径无异常对象，需拍板归类语义：记 failed 还是有意算正常结束
- `GeneratorExit`/`asyncio.CancelledError`（生成器被关闭/取消，如 detach 的 pending.cancel）路径确定性记 completed；旧实现 exc_info 可靠时记 failed——建议补 `except (GeneratorExit, asyncio.CancelledError): turn_failure = ...; raise`
- switch_workspace 中断分支整轮从历史抹除（strip_ws_switch_tail），本回合已落盘的文件改动随之对模型隐形（同副作用注记缺口；new_session 分支历史整体作废无需处理）——需确认「切走即整轮未发生」是有意设计还是同样要补副作用注记

---

## P3：观察级

| 编号 | 条目 | 备注 |
|------|------|------|
| #12 | 全局单例过多（tool_registry/llm_service/context_window_manager/BashBackgroundRunner/BackgroundAgentManager） | 短期文档化每个单例生命周期与重置方法 |
| #13 | 测试 SimpleNamespace mock 过多（352 处/55 文件） | 定义共享 typed mock 类 |
| #14 | AGENTS.md 过长 | 拆分，架构深挖进 COARA_ARCHITECTURE.md |
| #15 | 无用户反馈闭环 | /report 已走子智能体内部通道，缺 issue tracker/稳定反馈端点 |
| #16 | `src/ui/web_file_bridge.py:74-85` resolve_file 未命中全目录 iterdir 前缀匹配 | 投递目录大时 O(n)；可建索引（2026-09-09 复核仍在） |
| #17 | `src/coara/event_bus.py:59-63` 订阅表 defaultdict 空列表残留 + 退订 O(topics) | 轻微内存驻留；topic 数有限（复核仍在） |
| #18 | `src/coara/root.py` switch_llm_global 用 manager.active_entry 判前台，后台空间回合中执行可错位 | 窗口小；可改显式前台引用（复核仍在，行号现为 :406/:516/:538） |
| #19 | `src/vault/crypto.py:28-41` DEK 中间副本（derive_key 返回 bytes、rekey 途中 dek_old/dek_new）不可变驻留至 GC | Python 固有；session 侧已用 bytearray，可对齐（复核仍在） |
| #20 | `src/event_sources/sources/file_watch.py:21,67,89` 共享 4 线程 debounce 池，`_fire` 线程内 sleep 阻塞，慢路径（网络盘 resolve）可占满 | 新事件排队延迟甚至丢失；可加超时/队列上限（复核仍在） |
| #21 | `src/prompt/agent_registry.py:35-41` _agents 类属性 + `__new__` 单例冗余，绕过单例构造共享状态 | 主要影响测试隔离（复核仍在） |
| #22 | 三 provider 重复 LLMError 包装样板（`anthropic.py:553-557,711-716`、`openai.py:530-537`、`responses.py:312,446`） | 重试已集中 retry.py，异常包装可抽公共 helper；2026-09-09 新增 |
| #23 | 前端 `ServerMessage` 联合类型（`src/ui/web/src/lib/ws.ts:34`）未覆盖 `subagent_start/complete/failed`、`background_agent_*` 等生命周期事件（后端 `trace_broadcast.py:63-67` 下发、store.ts 用裸 Set 消费） | TS 契约不健全，运行期靠 trace_batch 兜底不崩溃；`store.ts` 另有 3 个零调用死 action（`clearTraceEvents`/`clearVaultResult`/`resetModuleSession`）可删；2026-09-09 新增 |
| #24 | fire-and-forget 任务引用丢失集合：`src/coara/base.py:798`（diff ensure_future）、`src/coara/mobile_sync.py:218,308,366,405`（create_task 不存引用）、`src/ui/web_server.py:453`（browser focus） | 异常时仅「Task exception never retrieved」噪声，不影响主流程；可统一收编到持引用容器；2026-09-09 新增 |
| #25 | `src/ui/web_socket_registry.py:11` docstring 声称「single event loop, no locks needed」但实现有 `asyncio.Lock()`（:56） | 文档误导，改注释即可；2026-09-09 新增 |
| #26 | 仓库磁盘卫生（均为 gitignored，不入库）：src+tests 约 1545 个 .pyc/61+ `__pycache__`、`build/` 501 个 .py（旧 wheel 残留）、`MagicMock/` 56 文件（mock.coara_home 测试残渣）、`gomatrix/gomatrix.exe~` 18MB | 可选定期清理；git 追踪层面无冗余（`standalone/makevideo` 为 embody/screenshot 工具引擎，活跃）；2026-09-09 新增 |

---

## 本轮遗留（2026-09-11：web 端刷新/切空间同一把尺改造）

### #27 [P1] 快照游标覆盖整线，消息切片只回 limit 条（不同源）

- **位置**：`src/ui/web_views.py::WebViewStore.build_messages`（`latest_seq` 取全部帧的 `view_seq` 最大值；`messages` 在出口处 `[-limit:]` 截断）、`src/ui/handlers/session.py::_load_view_messages/_load_view_snapshot`
- **问题**：游标宣称「这条线已到 N」，端上据此丢弃 `view_seq <= N` 的实时帧；但返回的切片只有最近 `limit` 条——被丢弃的帧既不在切片里、也没在端上渲染，长线上就是「判定已覆盖、行却不在屏上」，且此后不会再补（该序号永远不会再来一次）
- **方案**：游标与切片同源——`latest_seq = 切片末条 seq`（另给 `slice_from_seq` 表达切片起点），或让端上以「快照覆盖区间」而非单点游标做裁量。M

### #28 [P2] 历史条数两个常量不统一（刷新 200 / 切空间 100）

- **位置**：`src/ui/handlers/session.py::_DEFAULT_HISTORY_LIMIT = 100`（切空间响应与 `/api/session/messages` 默认）、`src/ui/web/src/views/ChatView.tsx`（`fetchSessionMessages(200, …)`）
- **问题**：同一把尺两个数——F5 首屏取 200 条、切空间快照取默认 100 条，长度与游标随路径不同；出问题时两种路径的表现不一致，难对账
- **方案**：收敛为一个跨端常量（服务端默认调 200，或前端刷新也走默认），并在 `docs/Web设计体系.md` §0.2 记明。S

### #29 [P2] `reset_line` 只换世代，序号不会真的从 1 重来

- **位置**：`src/ui/web_views.py::WebViewStore.reset_line`（世代 +1、`latest_seq = 0`）与 `_next_view_seq`（取 `max(read_latest_view_seq(path), meta.latest_seq)` 再 +1）
- **问题**：`reset_line` 不截断 jsonl，首次分配仍从文件尾部续号——docstring 宣称的「序号从 1 重新开始」在文件非空时不成立；且世代变化只体现在下一次快照的 `epoch` 上，已连端在下次快照前仍按旧 epoch 处理实时帧（本地缓存该丢的没丢）
- **方案**：`reset_line` 同时归档/截断该线文件（或把「线已重建」作为一条显式帧下发，端上立即作废本地内容与游标）。S

### #30 [P3] sidecar 元数据同步写盘可能短时占住事件循环

- **位置**：`src/ui/web_views.py::_next_view_seq → _persist_line_meta` → `src/core/json_store.py::write_text_atomic`（同目录临时文件 + 文件 fsync + 目录项 fsync，全程同步）
- **问题**：新文件首帧、以及每 `_META_WRITE_MIN_INTERVAL_S`（1s）一次，都在回合协程里同步落盘；网络盘/机械盘上单次几十毫秒，直接拖慢流式输出（帧是逐条 `append_event` 的）
- **方案**：sidecar 写盘移出事件循环（`asyncio.to_thread` 或独立线程 + 队列），失败照旧只记日志；元数据滞后一两帧本就无害。M

### #31 [P3] standby 流「LRU」名不副实（实为 FIFO）

- **位置**：`src/ui/web_server.py::_standby_stream_for`（`_MAX_STANDBY_STREAMS = 8`，超限 `pop(next(iter(self._standby_streams)))`）
- **问题**：淘汰按插入序，命中复用不刷新位置——用得最多的那条线可能先被淘汰（连它的 replay buffer 一起丢），而注释写的是「只留最近若干条」
- **方案**：命中时把键重新插到末尾实现真 LRU（或改注释明确 FIFO 语义与代价）。S

### #32 [P2] 子智能体帧投递失败只记 debug

- **位置**：`src/coara/base.py::_route_subagent_tool_line`（`except → logger.debug`）、子智能体 chunk 投递路径同款、`src/ui/web_views.py::_persist_line_meta` 失败也是 debug
- **问题**：这几条帧没有第二个来源（`subagent_chunk` 不落带、工具行只投不发第二遍），投递/落盘失败即永久缺失；而默认日志级别下 debug 不可见，排障时表现为「展开区少一段」且查不到原因
- **方案**：失败至少 warning，并带上 `source / session_id / tool_call_id` 键名（与 `EndRegistry` 的 route miss 日志同格式，一眼对上）。S

### #33 [P3] 折叠区封顶与老数据 `brief` 无法归位

- **位置**：`src/ui/web_views.py`（`_FOLD_MAX_CALLS = 30`、`_FOLD_MAX_FRAMES_PER_CALL = 100`、`_trim_fold_*`）、`_is_brief_frame` + `payload.get("parent_tool_call_id")`
- **问题**：① 超 30 个 call / 单 call 超 100 帧的历史子智能体产出被静默裁掉，刷新后展开区不完整且无任何提示；② 老数据的 delegate 指令没有 `parent_tool_call_id`（或为空串），归集键为空 → 端上找不到对应 delegate 行，指令既不进展开区也不进正文（静默消失）
- **方案**：封顶后给可见提示（「更早内容已省略」）或按需分页拉取；老数据按所在回合的 delegate 工具行反查 call_id 兜底归集。M

### #34 [P2] 上下文补注入每回合全量拼接历史文本，前缀子串判定可能误判

- **位置**：`src/coara/turn_loop/user_turn_injectors.py::inject_environment_seed`（每回合把整段 history 转文本）、`src/coara/injections/context_modules.py::build_missing_prefix_messages`（`joined = "\n".join(history_texts)` 后 `marker in joined`）
- **问题**：① 每回合 O(历史长度) 的转换与拼接，长会话每轮都做一次；② 判据是「前缀出现在整段拼接文本里」——压缩摘要若复述了模块标题（如 `AGENTS.md：`），该模块被判「已存在」而永不补注入，上下文静默缺失（与 `is_context_module_seed` 的 `startswith` 严格判据不一致）
- **方案**：改用显式标记（消息 metadata / 注入世代号）判断「该模块是否已在历史里」；至少把判据改为只在历史头部若干条里按 `startswith` 认领（与 `is_context_module_seed` 同源）。M

### #35 [P2] `delegate(action=message)` 对运行中的前台子智能体不可用

- **位置**：`src/tools/builtin/delegate/delegate.py::_execute_message` → `lookup_running_subagent`（只查 `_RUNNING_SUBAGENTS`，即带 channel 的前台 coaras）
- **问题**：运行中的前台 aide（或任何未登记进该表的活实例）查不到 → `_execute_message` 直接落回 `_execute_resume`：用户以为发的是途中消息，实际动作被静默替换成「追加任务 / 断点续跑」，报错文案也随之误导
- **方案**：查不到活实例时按 `coara_id` / `BackgroundAgentManager` 兜底找活实例再投递；确无活实例则给明确错误（「该任务不在运行中，如要追加任务请用 resume」），不做静默动作替换。S

---

## 附：2026-09-09 全仓复审记录

**方法**：三路并行扫描（运行时核心 / UI·CLI·前端 / 外围子系统+台账复核）+ 仓库卫生核查。另：当日早间完成未提交改动集（89 文件，+3421/−1095）两轮 review，提交前修掉 2 处（MatrixApiService.kt `nextSyncRetryDelay` when 合并行格式损坏；delegate.py auto-resume 兜底 `ToolResult.error` 补 metadata 使回滚注记可取 task_id）；新增测试 10 文件全绿（61+8 passed）。

**出册（3 条）**：
- 原 #7 无错误恢复指引：`_user_facing_llm_error`/`_user_facing_turn_error` 集中映射已覆盖 API key/402/403/429/超时/空响应/上下文异常等全部已知类别并给中文恢复指引；provider 层残留降级为新 #8 观察项
- 原 #8 后台完成唤醒回合不回 Matrix 发起端：`root.py:1457` 起 `origin_source in ("matrix","event")` 时 `_mirror_awakened_reply`（:1464-1486）从历史取最终正文经 `matrix_notify.send_to_user` 送回；web 侧 `stream_awakened_turn` 已带 `workspace_dir` 落盘锚定。残留：`TaskRecord.origin_source` 仍只存 source 字符串，回投经 `matrix_notify` 单例兜底而非按 room_id 定向（多房间场景待观察）
- 原 #17（P3）matrix_connect 探测失败覆盖 .env：现仅占位符/迁移场景才 `persist_matrix_homeserver`，占位符密码直接报错不写回

**数字更新**：getattr 防御 32→60（扩散，见 #2）；`except Exception: pass` 54→49（部分缓解，见 #3）。

**新增 7 条**：#7（端/通道状态载体并行 + 双谓词漂移）、#8（provider 技术串残留）、#22（provider 包装样板）、#23（前端类型漂移 + 死 action）、#24（fire-and-forget 任务引用）、#25（registry docstring 失实）、#26（磁盘卫生）。

**阴性结论（核查过、确认无问题）**：`_remote_mirror`/`CLI_BACKGROUND_RESULT`/Ctrl+B 退后台全仓零残留；TODO/FIXME/HACK 标记全仓为零（技术债全部由本台账跟踪）；前端 22 组件 + 16 lib 工具 + 11 handler mixin 无死代码；`src/llm/_debug.py`、`orphan_repair.py` 均被引用非死代码；`web_server.py` deferred-remote-ctx 的 `send_text` 现供 vault/collect/diff/remote_channel 桥接（approval_bridge.py:123 等 6 处消费 get_turn_send_text），非死参数——web 端正文单一走 EndRegistry+TurnStream，与它端一致。

---

## 附：2026-08-20 修复战役记录

九路并行批次，**P2 全部 18 条出册 + P3 五条出册 + 两项拍板落地 + 两个线上 bug 根治**：

- **回合与调度**：#15/#20 回合失败误记 completed（显式 turn_failure 变量替代 sys.exc_info()）；#16 压缩快照串空间任务清单（按 coara 解析 TaskStore）；#17 工具中断漏登磁盘副作用（中断注记含已执行清单）；#18 flow 悬空边到达残留（wait() 同步 drop_arrivals）；#19 flow reset 竞态（epoch 世代守卫，连 _maybe_start 重试回调一并封死）；#14 引擎死亡终态事件丢失（reader 排干残量）
- **存储并发**：#21 session_log seq 缓存推进+单进程假设文档化；#22 todos store 与 lock 同键共生逐出；#23 records 写路径实例锁串行化；#24 registry 热重载清空挂载快照；#25 vault 双进程解锁 O_EXCL 原子占位；#26 task_store 进程级实例缓存
- **UI 与通道**：#27 JSONL 截断全量重读改锁内读文本+锁外解析；#28 会话增量缓存实例锁；#29 retract 全程持 io_lock；#30 WS 断连后向死连接发送（发送前查 closed+顺手清引用）；#31 上下文预算等长替换漏检（system+tools 内容指纹）；#32 审批 send 30s 限时
- **human_approval 死路径整条拆除**（内核化后无源）：web approve 端点/引擎 approve_step/bridge 事件枚举/前端审批按钮/相关测试，grep 零残留
- **P3 出册**：aiohttp ClientSession 每请求新建（新增 http_session 共享单例；实锤 `_homeserver_link_watchdog` 原每 60s 新建一次——与 httpx 同类卡顿源）；手机 ACK done_callback 不取异常；by_hash/by_url 单值覆盖改 list；search touch 写放大批量化；测试临时文件泄漏复核（3c02f4c6 已修，后继测试已走 tmp_path）
- **vault 明文过渡缓解**：gomatrix vault 回信清扫（sync 读库回放架构决定不能「不落盘」，选定期清理：启动清存量+每分钟清 5 分钟保留窗外）
- **CLI 输入卡顿根治**（用户实测实锤）：`httpx.AsyncClient()` 默认构造同步加载 Windows CA 证书库 570ms——gomatrix 看门狗每 10s 新建一次致每 10s 冻结事件循环半秒；loopback 探测三处改 `verify=False, trust_env=False`（顺带消除代理环境变量劫持 127.0.0.1 的隐患），遥测 https 客户端线程内构造；工具栏命中率 8MB 日志尾全量 JSON 解析（80ms GIL）加 session_id 字节预过滤降至 17ms
- **工作空间切换两 bug 同根修复**：主循环被旧回合流钉死——`iter_while_foreground` 只在下一 chunk 到达时检查前台状态，静默期（长工具/等首包）主循环永久卡死，新前台空间的 /model 等输入堆队列不执行、切回后输出不上屏（注册表 mtime 取证实锤 /model 未执行）；修：detach 改 0.2s 轮询 + 重挂自愈（ensure_streaming 按 block 实际状态重开，reset 竞争不再丢 chunk）
- **出册/挪移**：版本号策略（全端 1.0.0 同源已事实统一）；git 历史凭据决策记录挪至 manual/18 §18.9

验证：见文末测试基线（全量 pytest + ruff + WebUI build + go test）。

---

## 附：2026-08-16 内核化战役记录

**当日完成**（全量 1246 passed / ruff 全绿 / Web UI build 绿，时点 2026-08-16 下午）：

- **工作流全切 kernel**：WorkflowScheduler 只建 KernelGraphRunner；export→save→run 断链闭环（集成测试 + 实跑验证）；WdlRunner、wdl_legacy、迁移脚本、dsl_parser、qc_agent（QC gatekeeping 随内核化无入口）、approval_html 及 wdl 解析栈（parse/validate/emit/document/graph/normalize）全部删除；wdl 包仅剩 schedule 工具；Web 编辑器画布对齐内核图（前端 17 文件重写）
- **shell 前后台语义**：前台超时即失败（无并行、无分离），进程树被终止并返回明确超时错误，模型据此决策重试或改用 `timeout_ms=0` 后台；get_execution_timeout 全仓统一 (default_timeout, args) 签名
- **ptc 加固**：import/open/exec/eval 静态预检（提交时拦截带行号）；未捕获 ToolCallError 附修复建议；内置 pathlib/datetime
- **修复存量 bug**：YAML 1.1 裸词 `on` 解析为布尔使 error 边静默降级（serde 兼容还原）；prune 时区混用（本地 now vs sqlite UTC，东八区误归档刚完成实例）；KernelGraphRunner 不建实例行致终态假写；kernel resume 未认领致 fencing 终态被拒；aiosqlite 连接线程不 close 拖住测试进程退出（AGENTS.md 记录错误模式）
- **随删出册**：原 #11（WDL 控制流 resume 断链）、#15（QC 门禁解析缺省）、#16（for 回写不一致）、#17（dsl 条件除零）、#19（scanner WAITING 双路竞争）——载体已删或场景消失

---

## 附：2026-08-14 审计-修复战役记录

**当日已修 52 项**（出册），要点：

- **P0 数据丢失级**：vault 改密跨进程互斥（service.py 检查 open 会话 marker）；updates sweep 时区混用 TypeError（aware/naive 统一，janitor 自动过目恢复）；配置写入优先级补 users/default 层（WebUI 保存不再被遮蔽）；OpenAI 流式 usage chunk 丢弃（choices=[] 带 usage 的最终 chunk 不再被 continue 跳过——zhipu/minimax/deepseek-chat 全系费用与命中率统计的数据源洞）
- **P1 安全**：/attachments 静态目录 token 鉴权；providers 接口 mask_secrets 脱敏 + 保存回填闭环；Referrer-Policy: no-referrer 全响应 + meta 兜底；Matrix 信任收敛 owner 名单（空名单回退+告警，2026-08-20 已配置出册）；prompt extends/INCLUDE 根目录约束（拒绝对路径与 .. 逃逸）；@-mention 目录越界与超限全量读
- **运行时**：MCP 三连（启动失败泄漏子进程、stop 误取消等待者→McpError 异常路径、read_loop EOF 唤醒 pending）+ start 并发锁；flow run() 节点引用防 GC；UnifiedScheduler 空闲路径不丢已出队消息；/new 失败锁死解除；disabled 工具段缓存失效；工作流完成事件带 session 戳路由回原会话；分离工具跨会话注入拦截；shell 超时交接泄漏与临时脚本残留；grep 取消终止/reap
- **数据域**：todos 损坏隔离+拒写护栏（.corrupt 保留）；agent 记忆去重 hash 口径统一（compose_body 单一真相）；session_log shadow 倒挂与 seq 缓存过期；AGENTS.md 字节口径；一次性提醒 delivering 先行持久化；背景注入状态文案
- **工具与体验**：缓存命中返回拷贝（metadata 不再被污染）；delegate depends_on 字符串规范化；run_before hook 单点异常不炸批；upload Windows 保留名；.env 原子写；图片读盘出事件循环；Retry-After 封顶 60s；abort 重发；anthropic parsed-stream 泄漏；POSIX 进程组自杀守卫；mask_secrets 词边界
- **架构增强**：CLI toolbar 命中率与 WebUI 同源（events.jsonl 单事实源，session_cache_hit_ratio）；delegate resume 支持已完成任务追加指令（审计→修复合一，消除重复劳动）；子智能体白名单显式排除 tool 装载器与未揭示挂起工具（spawn 时工具面一次定死）

验证：全量 pytest 1233 passed / 19 skipped（基线 1202，+31 新增回归用例），ruff 回归基线（仅剩 3 处既有长文档串 E501）。

修复明细逐项见 ref-doc/全系统审计-2026-08-14.md。
