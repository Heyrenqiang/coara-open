# Web 设计体系（单实例）

> 状态：工程契约（2026-09-10）。
> 依据：[`空间性质模型.md`](空间性质模型.md) §1–§6、§8–§9。
> 实施范围：**单实例、单用户、可演示** —— 先把"一个人在 coara 上做空间、用空间"做到足够强。分发 / 生态（空间性质模型 §7）远期。

## 0. 目的

本文件不是"让界面更好看"，而是**让界面可被安全改写**（空间性质模型 §9.3）。因为交付后每个用户都会改，所以必须交付"能被改的东西"而不是"改好的东西"。据此定五件事的硬约束：分类、布局、令牌、组件、治理。

### 0.1 单一事实与渲染硬约束（已并入契约）

单一事实原则、一条入屏通道、原子提交、折叠区契约、顺序不变量（I1–I10）的具体条文，**以 [消息渲染契约.md](./消息渲染契约.md) 为唯一权威**。本节不再重复表述，避免两处口径漂移。

### 0.2 落地机制（已并入契约）

落地机制（视图带与快照的形状、订阅先于快照、帧裁量＝行存在性、慢而不跳、增量快照）**以 [消息渲染契约.md](./消息渲染契约.md) 为准**。

### 0.3 折叠区契约（已并入契约）

子智能体产出的归属键、三张映射、手风琴组序、隐藏行清单与封顶策略**以 [消息渲染契约.md](./消息渲染契约.md) §五为准**。现行口径：子智能体的任务指令 / 过程正文 / 最终结果**一律钉在发起它的 delegate 工具行里**，不投影成主会话消息；过程正文只落带、读端归集，缺父标识的帧三端一律丢弃。

### 0.4 顺序不变量（已并入契约）

「序号给归属、落定给顺序」的三条机制与三类危险路径**以 [消息渲染契约.md](./消息渲染契约.md) §四为准**。

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
| 输入框上方（⑥） | 子智能体、转圈 | 当下正在发生 / 需要我现在决定 |

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
