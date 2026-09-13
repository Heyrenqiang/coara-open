# Web 设计体系（单实例）

> 状态：工程契约（2026-09-10）。
> 依据：[`空间性质模型.md`](空间性质模型.md) §1–§6、§8–§9。
> 实施范围：**单实例、单用户、可演示** —— 先把"一个人在 coara 上做空间、用空间"做到足够强。分发 / 生态（空间性质模型 §7）远期。

## 0. 目的

本文件不是"让界面更好看"，而是**让界面可被安全改写**（空间性质模型 §9.3）。因为交付后每个用户都会改，所以必须交付"能被改的东西"而不是"改好的东西"。据此定五件事的硬约束：分类、布局、令牌、组件、治理。

### 0.1 单一事实：刷新与页面切换同一把尺（硬约束）

**任何页面、任何时刻，端上显示的就是该页面当下该有的事实**——事实只有一份：服务端权威（该空间的视图带，加上它此刻的 runtime）。端侧的一切手段（本地缓存、IndexedDB 预览、增量拉取、seq 合并、滚动锚）只允许用于「更快更稳地到达这份事实」，**不得成为第二份真相，更不得先行上屏**。

- **允许慢，不允许跳。** 宁可等权威数据回来一次到位，也不用可能过期的本地副本先铺一屏、再被覆盖——那就是 A 跳 B。加载期间该空就空、该显示加载态就显示加载态。
- **判据**：任意时刻把端上显示与该页面权威数据对一遍，必须一致。不一致就是 bug，不必再分「缓存旧了」还是「合并漏了」——那只是同一错误的不同表现。
- **适用范围**：全部页面切换与刷新，不限对话页。
- **禁止补丁式回调**：用「先铺旧料 + 稍后校正」的时序技巧抹平跳动，等于把真相留在端侧再对齐；正确做法是让端侧根本不产生这份料。
- **豁免：本端动作的即时回显不算第二份真相。** 用户自己发出的消息（乐观气泡）与正在流式生成的正文可以先上屏——它们表达的是「我做了什么」，不是「服务端已经是什么」；权威数据到达后必须被**等价替换**（同一条内容不变成两行）。除此之外的一切持久内容，端侧都不许自行合成。

### 0.2 落地机制：一条入屏通道（0.1 的实现）

**事实的形状**

- 视图带（每空间一条 JSONL）是内容的唯一权威，逐帧带 `view_seq`——该线内单调递增，落盘时分配
- 快照接口返回 `{messages, latest_seq, epoch, workspace_dir, session_id, runtime}`；`epoch = "{workspace_dir}::{subject}"` 标识「哪条线」，`latest_seq` 是快照覆盖到的最后一帧

**一条通道**

- 屏幕内容只由两种输入驱动：快照与实时帧；两者都必须携带 `workspace_dir`、`session_id`、`view_seq`，由端上同一个 reducer 应用
- 端侧合成的持久内容、无归属的直推帧、第二份历史接口，一律删除
- 端上只留 UI state（滚动位、展开态、草稿）与乐观回显（自己发的气泡、流式正文）

**订阅先于快照**

- 进页面（刷新/切空间）时先在 WS 上订阅（连接即订阅，帧先入缓冲），再取快照
- 快照落地后：丢弃缓冲中 `view_seq <= latest_seq` 的帧，按序应用其余，最后一次提交上屏
- `view_seq != last + 1` 即 gap，就地按 `after_view_seq` 增量补齐

**原子提交**

- 内容与 runtime 在同一次状态提交里落定，中途只显示骨架
- 禁止「先渲染一半再补齐」，禁止「先铺旧内容再校正」

**慢而不跳**

- 骨架尺寸对齐真实内容；允许空、允许等，不允许旧内容上屏

**丝滑的正当手段**（都是「更早到达事实」，不是替代事实）

- 预取：切换动作发起时并发取目标空间快照，到位再切边界、一次提交
- 常驻 shell：切页面只换内容区，侧栏与顶栏不重挂
- 增量快照：带 `after_view_seq` 只拉增量

**验收**

- 任意时刻把端上内容与该空间视图带按 `view_seq` 前缀比对，必须一致
- 一次切换只允许出现两种画面：骨架 → 目标内容
- 刷新后与权威一致，含回合态

### 0.3 折叠区契约：子智能体的产出钉在发起它的那条工具行里（硬约束）

**归属键由服务端给，端上只按归属键归集。** 子智能体的三个产出面（任务指令 / 过程 / 最终结果）都不是主对话正文——它们属于发起它的**那条 delegate 工具行**。端上不允许自己从文本里猜归属，服务端必须让每一类产出都带得回键。

| 映射（快照顶层字段） | 形状 | 归属键 | 来源帧 |
|---|---|---|---|
| `subagent_results` | `{tool_call_id: text}` | 子智能体任务 id（= delegate 工具行的 `tool_call_id`） | 落带 `kind=subagent_result` |
| `subagent_briefs` | `{父 call_id: 指令全文}` | 发起它的 delegate 行 call_id | `user_message` 帧带 `delegate_brief`（老数据 `delegate_task` / 正文以 `<任务指令>` 开头） |
| `subagent_diffs` | `{父 call_id: [帧…]}` | 同上 | 带 `parent_tool_call_id` 的 `tool` / `diff` 帧 |

- **不投影成消息**：`build_messages` 不把这三类帧投影成聊天行。落成气泡＝刷新后凭空多一条回复，且指令气泡会假扮用户输入。
- **实时与快照同一套键**：WS 帧带 `parent_tool_call_id`，快照带上面三张映射；两端按 `view_seq` 去重后并进同一张表——刷新前后展开区内容一致。
- **过程正文不落带**：`subagent_chunk` 只做实时投递（按 `tool_call_id` 分桶微批）。过程信息落了带，hydrate 就会把它当正文复现。
- **手风琴组序固定**：任务指令 → 过程 → 最终结果（组名以此为准，代码在 `toolLineGroups.ts`，selftest 逐字断言）。**「过程」组装的是子智能体干活途中的全部痕迹**，三种来源合成一条时间线：① 落带帧（子智能体自己的工具行 / diff，按 `view_seq` 升序）→ ② 活动树里「帧还没有的行」（在跑的工具：帧还没落带，树里已是 `◌`）→ ③ 过程输出正文（`subagent_chunk` 累积的那段，排最后、**不带标签**）。①② 是同一件事的两个来源，按 call 去重、**以帧为准**（同一工具只显示一次；跑完的不会重复，在跑的仍可见）。计数提示合成一条（项数＝帧数 + 补进来的树行数，另有正文时追加 ` · X 字`，如 `7 项 · 1.2k 字`）；「最终结果」是收官答复，独立成组并带标签。空组不出现；一组都没有则该 delegate 行不可展开。默认展开态：任务指令收起，其余展开。组内限高、面板限高，超出在组内滚动。折叠区里的 diff 用顶格变体（去掉 `DiffBlock` 的左右内边距），与同组工具行的 `✓`、与过程正文左缘对齐（都是组体左缘，无额外缩进）；活动树补给行保留 `depth` 缩进（那是树的形状）；主流里的 diff 卡片外观不变。
- **封顶策略**：快照的折叠映射只回游标之后的帧，call_id 数封顶 30、单 call 帧数封顶 100；端上再随消息窗口回收（只留 messages 里仍有该 delegate 行的 call、活动树在跑的 call，以及最近 8 个出现过 call）。封顶保证长线上历史子智能体不随会话长度无界回传。
- **隐藏的过程噪音工具行**（当前两条）：`delegate wait`（内部同步点，不是工作——一回合能出现几十次，活动树也不给它建行）与 `send_file`（它的效果本身就是聊天流里那张文件/图片卡，工具行是重复信息）。**只在渲染层过滤**：行照旧写在视图带里、照旧参与顺序不变量与快照对账，只是不画；折叠区条目与计数同样排除它们。判据真源在 `src/ui/web/src/lib/toolVisibility.ts`（`isHiddenToolLine`），文档只登记清单；取向是「认不出来就不隐」。
- **实现单一真源**：分组判据在 `src/ui/web/src/lib/toolLineGroups.ts`（纯函数，可 selftest 直接断言）；行渲染在 `src/ui/web/src/features/chat/ToolLineRow.tsx`（只有一份，聊天流与折叠区共用）。

### 0.4 顺序不变量：序号给归属，落定给顺序（硬约束）

**机制（三条，缺一条就会乱序）**

1. **内容行带序号**：落带的每一帧都由服务端分配 `view_seq`（同线永久单调），端上把序号挂在发生它的那一行上。
2. **落定即归一**：任何一次状态落定都过一次顺序归一——落带段按 `view_seq` 升序、未落带实时尾部（无序号）按到达序排在落带段之后，同 `view_seq` 的后来者丢弃（同一帧被实时/回放/快照三路各送一次时的幂等）。
3. **回放先排序**：缓冲回放（边界未定期的帧）入口先按 `view_seq` 排序，无序号帧保持到达序、排最后。空洞期的迟到帧与先到的后帧因此不会交错应用。

**会破坏它的路径**（这三条都是「老内容画到最下面」的现成因，不得靠调用方自觉规避，只能靠上面的机制兜住）

| 路径 | 为什么危险 |
|---|---|
| 迟到帧直接 append | 断连重放 / 缓冲回放 / gap 补齐回的帧，序号可能小于已渲染内容的最大序号 |
| 回放按到达序直接送 | 空洞期先到的后帧与迟到的前帧交错 |
| 增量 hydrate 整体追加 | 游标可能偏（他端动过、上一页生命的残留），会带回一段已渲染过的帧 |

**判据**：任意时刻把端上顺序与该空间视图带按 `view_seq` 比对，必须一致；不一致就是 bug，不必再分「缓存旧了」还是「合并漏了」。

## 1. 分类与归位

- **顶层两层**：用户壳（个人 / 全局记录 / 最近动态）+ 空间（各自主页）
- **空间的完整画像** = `(数据来源, 可变性边界, 默认落点)`（空间性质模型 §6）
- **导航由注册表派生，禁止硬编码**。现存 `AppLayout.tsx` 的 `NAV_PREFIXES` 与 `isConversationSpace()` 二元分组待移除
- 门面三形态（仓库 / 展示 / 营业）只作**缺省模板与命名**，**不作分类轴**

## 2. 布局：PageShell 五槽位

| 槽位 | 职责 | 必选 |
|---|---|---|
| 导航 | 空间切换器（用户壳 + 空间） | 是 |
| ① 空间身份头部 | 名称 / 描述 / 状态 / 主操作 | 是 |
| ② 工具条 | 筛选 / 视图切换 / 动作 | 否 |
| ③ 内容区 | 查看器（由内容类型决定） | 是 |
| ④ 检查器 | 右侧辅助面板 | 否 |
| ⑤ 常驻对话入口 | 全空间统一入口 | 是 |

**约束**：页面不得自造头部 / 滚动 / 内边距；一切页面经 `PageShell` 组合。

**滚动与内边距契约**（2026-09-10 收口）——此前每个视图各自复写 `flex:1 + overflow:auto + padding`：

| 参数 | 取值 | 用于 |
|---|---|---|
| `scroll` | `"auto"`（默认） | 内容超高即滚 —— 列表 / 详情 / 表格页（`UsageView` `ReviewView` `PersonalView` `LoginView`） |
| | `"hidden"` | 内容自带滚动子区 —— 左右分栏、目录树、查看器（`RecordsView` `FileView` `ChatView`） |
| `padded` | `true`（默认） | PageShell 提供 `16px 20px 28px` |
| | `false` | 内容自带 padding（`UsageView` `ReviewView` 的居中窄栏） |
| `surface` | `"surface"`（默认） | 白底内容（表格、详情） |
| | `"subtle"` | 灰底内容页（用量、消息、登录） |

**槽位约定**：`PageShell` 的 ④ 检查器是**固定宽**右栏；需要拖拽的（`ChatView` 状态栏、`FileView` 目录树）暂由页面自管，不进 `inspector` 槽。

**头部统一（2026-09-10 定案）**：全站**页面级**头部采用「**紧凑工具头**」= `PageHeader`（`Title level={5}` 16px + meta + subline + 1px 分隔线），不再使用 `level={4}` 大字标题。`PageHeader` 提供 `divider={false}`，供「头部 + 自有分隔线的 Tab 工具条」共用一条线（`RecordsView` 即此用法）。
（注：**内容级**标题不受此约束 —— 记录详情页的记录标题、登录卡标题等仍是 `level={4}`，属文档内层级，不是页面身份。）

**子视图**：`RecentFilesList`（原 `RecentFilesView`）不是路由页，是 `RecordsView`「最近」页签的子视图 —— 由父页 `PageShell` 承载，自身不套壳。判定：**有路由则套壳，无路由则组件**；且子视图**不得保留**「独立页 / 嵌入」双形态开关（自 §7 第 8 步清理）。
**迁移进度**：见 §7（1–6 全部完成）。

## 3. 令牌：单一真源

- **唯一真源**：`src/ui/web/src/theme/tokens.ts`
- **两条派生**：`coaraAntdTheme`（antd ConfigProvider）+ `cssVars`（`:root` 运行时注入，`main.tsx` 调用 `applyTokens()`）
- **硬规则**：组件内**禁止裸色值**（`#hex` / `rgb()` / `rgba()`）；样式表统一 `var(--coara-*)`
- **新增令牌**：先在 tokens.ts 登记，再到组件使用；不允许"先在组件写死"
- **已修正**：`--coara-brand` 曾被 `index.css` 引用但从未定义（靠 fallback 兜着），已在 tokens.ts 补齐
- **已清零**：存量裸 hex 151 → 0、rgba 26 → 0（2026-09-10，脚本 `scripts/clear-bare-colors.mjs`；按文件覆盖差异，如 `#cf222e` 在语法高亮与 diff 下语义不同）
- **待合并**：`textMuted(#6b6b6b)` 与 `textSecondary(#6b7280)` 同角色

## 4. 组件分层

| 层 | 内容 | 规则 |
|---|---|---|
| **L0** antd 原语 | Button / Table / Card / Modal … | 不直接改色改间距 |
| **L1** coara 包装 | `components/layout/{PageShell,PageHeader,SectionCard}` + `components/states/States`（`StateBlock` / `LoadingState` / `ErrorState` / `EmptyState`） | 只引语义令牌 |
| **L2** 业务复合 | MessageList / SubagentTree / DiffBlocksView / RecentFilesView … | 只组合 L0/L1，不重造布局与样式 |

**四态**：每个页面必须处理 空 / 加载 / 错误 / 无权限。

四态组件带 `fill` 开关（2026-09-10 收口）：
- `fill`（默认 `true`）：`flex:1` 撑满内容区居中 —— 整页加载 / 整页错误。
- `fill={false}`：内联块，纵向留白 `48px`、宽度 100% —— 列表内的小块加载、分栏内的空态。

此前 `textAlign:center + <Spin/>`、`<Empty description=... />` 的手写写法已全部替换为四态组件；`antd` 的 `Spin` 仅在需要 `spinning` **遮罩包裹**时直接使用（`UsageView` 的刷新遮罩），不属四态。

## 5. 治理

- **lint**：`src/` 内除 `theme/tokens.ts` 外禁止裸 hex / `rgba()`（脚本 `scripts/check-tokens.mjs`；已接入 `prebuild` 为**硬门禁** —— 写死颜色会让构建失败）
- **变更外观 = 只改 tokens.ts**
- **底座升级不得静默覆盖用户定制**：投影定义版本化 + 迁移，变更时提示受影响项（空间性质模型 §9.4）

## 6. 视觉基调与摆放

### 6.1 基调：单色灰阶 + 单一强调色

与 Android coara app 对齐。**问题从来不是配色不对，而是色值散乱** —— 现状已收敛到 `tokens.ts`。

- **层级靠字重 + 间距 + 1px 边框建立**，不靠大字号、不靠色块、不靠阴影
- **主色（accent）只用于**：可交互聚焦态、选中态、链接、主操作按钮。**不得作装饰**
- **圆角三档**：控件 8 / 容器 12 / 气泡 8 —— 按此映射，不新增档位
- **阴影只在浮层用**（弹窗、浮起气泡、拖拽块）；内容平面不加阴影
- **状态色语义固定**：进行中 = 青蓝 `progress`，成功 = `success`，警示 = `warning`，危险 = `danger`

### 6.2 分层摆放原则

**全局事实上顶栏 · 对话态入状态栏 · 待决贴输入。**

| 位置 | 放什么 | 判据 |
|---|---|---|
| 顶栏（①+②） | 空间身份、模型、新会话、连接态 | 跨页面成立的事实 |
| 状态栏（④） | 运行状态、上下文占用、最近工具调用 | 只随对话变化 |
| 输入框上方（⑥） | 保险柜提示、子智能体、转圈 | 当下正在发生 / 需要我现在决定 |

**已落地**（2026-09-10，`ChatView` / `StatusSidebar`）：

- `ChatView` 顶栏改为 `PageHeader`：左侧 ① 空间身份（`FolderOutlined` + `activeName`），右侧 ② 连接态（`Badge` 点 + 文案，断开时可点重连）+ 模型选择 + 新会话；原来那块 `<div style={{flex:1}}/>` 空白已消除。
- `StatusSidebar` 收窄为纯对话态：只留「运行时（Provider / Model）/ 上下文 / 缓存命中率 / 最近工具调用」；删掉了「连接」整块与「工作空间」行，`connected` prop 也随之移除（连接态改由顶栏自带）。

### 6.3 文件系统页

文件不是"一个页面"，而是**一个查看器**：

- 目录树 → 状态栏位（④）
- 文件内容 → 内容区（③）
- 路径与操作 → 工具条（②）
- 所属空间身份 → 顶栏（①）

即 `FileView` 从"整页"降级为由 `PageShell` 承载的查看器。

## 7. 迁移顺序

每步可独立交付；优先零行为变更。

1. ✓ **令牌单一真源**（2026-09-10 完成：tokens.ts + 主题/变量双派生 + index.css 去重 + 修 `--coara-brand`）
2. ✓ **存量裸 hex / rgba 清零**（2026-09-10 完成：151 → 0，脚本 `scripts/clear-bare-colors.mjs`；`check:tokens --fail` 已接入 `prebuild` 成为硬门禁）
3. ✓ **L1 骨架组件 + `FileView` 迁移验证**（2026-09-10 完成：新建 `components/layout/{PageShell,PageHeader,SectionCard}.tsx`、`components/states/States.tsx`；`FileView` 与 `ToolView` 的重复头部/外壳/分区标签全部收敛到组件，删掉 `ToolDetailSection`；`tsc -b` + `vite build` + 令牌门禁全绿）
4. ✓ **迁 `RecordsView` / `UsageView` / `ReviewView` / `ConfigView`**（2026-09-10 完成：四页全走 `PageShell`+`PageHeader`；头部统一为「紧凑工具头」—— 经用户拍板，全站不再用 `Title level={4}` 大字标题。`ConfigPanel` 自身的「配置管理」头部上移到 `ConfigView`，`ConfigPanel` 只留内容）
5. ✓ **导航注册表化**（2026-09-10 完成：新建 `src/lib/navRegistry.ts`，`SYSTEM_VIEWS` 单点登记 path/label/icon/load；`AppLayout` 的 `NAV_PREFIXES`、`ROUTE_PREFETCH`、`systemSpaceIcon` switch 三份散落知识全部改为消费注册表。`isConversationSpace` 保留但改为注册表反查 —— 内核给的 `home_view` 指不回注册表即按对话空间处理，未知主页不再导航到死路由）
6. ✓ **清债**（2026-09-10 完成）
   - `src/ui/web/README.md`：删掉 `/trace` `/workflow` `/tools` 三个已不存在的路由段落，补齐 `/records` `/review` `/usage` `/config` `/file?view=tool`；SPA 结构树同步更正。
   - `vite.config.ts`：删除 `vendor-workflow` 手动分包规则（`@xyflow` 已无引用，该 chunk 早已不产出）。
   - `package.json`：删除死依赖 `@xyflow/react`、`react-rnd`（全库零 import）与死脚本 `test:edges`（目标文件 `src/features/workflow/editor/…` 已不存在）。
   - 清理 vite 临时文件 `vite.config.ts.timestamp-*.mjs`。
   - △ 更正：`views/RecentFilesView.tsx` **不是**死代码 —— `RecordsView` 的「最近」页签在用它（`<RecentFilesView embedded />`）。
7. ✓ **收口：滚动契约 + 四态统一 + 页面全覆盖**（2026-09-10 完成）
   - **滚动与内边距下沉**：`PageShell` 新增 `scroll` / `padded` / `surface` 三参数，代管内容区滚动与内边距。删掉各视图复写的 `flex:1 + overflow:auto + padding` 外壳（`UsageView` / `ReviewView` / `ConfigView` / `PersonalView` / `LoginView` 直接受益；`RecordsView` / `FileView` / `ChatView` 因内容自带滚动子区而传 `scroll="hidden"`）。
   - **四态统一**：四态组件加 `fill` 开关（撑满 vs 内联块）。`RecordsView`（3 处）、`ReviewView`（2 处）、`RecentFilesView`（2 处）的手写 `textAlign:center + <Spin/>` 与 `<Empty description=.../>` 全部替换为 `LoadingState` / `EmptyState` / `ErrorState`，并清掉随之失效的 `Spin` / `Empty` import。
   - **页面全覆盖**：`PersonalView`、`LoginView` 补上 `PageShell` + `PageHeader`（此前是裸 `100vh / height:100%` 外壳，游离于体系之外）。
   - **核对结论**：9 个路由视图 100% 走 `PageShell`；`RecentFilesView` 为子视图（由父页承载），判定规则「有路由则套壳，无路由则组件」记入 §2。
8. ✓ **清理「历史路由残留开关」**（2026-09-10 完成）
   - `views/RecentFilesView.tsx` → `views/RecentFilesList.tsx`：删掉 `embedded` prop 及非 embedded 分支（含「最近文件」页头），唯一调用方 `RecordsView` 改传无参。判定规则：**从路由降级为子视图后，原用于「独立页 vs 嵌入」的开关即失效**。
   - `features/chat/ModuleChatPanel`：删掉 `embedded` prop 与 `title` prop —— 标题栏由外层 `ModuleChatFloat` 渲染，面板内从未显示过（恒 `embedded=true`）。连带删 `LoadingOutlined` import 与 CSS `.flow-chat-panel-head` 死样式。
   - `components/ExperimentalBadge`：删掉 `compact` prop（两处调用方皆不传，恒 `false`）。
   - 核查：`module-chat.css` 其余 `flow-chat-*` 类均有使用者。

## 8. 待决

- 投影 DSL 形态（结构化内核 + 自然语言表层？）—— 决定 ③ 内容区的接口
- 是否引入暗色（现状 `defaultAlgorithm` 写死、令牌全为浅色）
