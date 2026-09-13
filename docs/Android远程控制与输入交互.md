# Android 远程控制与输入交互

> **Canonical**：手机端远程操控 coara（slash 命令面板）、Matrix 命令链路、Windows 重启实现、Android 输入区手势与 UI 过滤规则。
> App ↔ PC 通信架构与侧信道协议完整定义见 [`MATRIX_APP_LINK.md`](./MATRIX_APP_LINK.md)；Android 客户端概览见 [`../android-app/README.md`](../android-app/README.md)；GoMatrix 部署见 [`../gomatrix/README.md`](../gomatrix/README.md)。

## 目录

- [目标与能力一览](#目标与能力一览)
- [Matrix 命令链路](#matrix-命令链路)
- [Windows 重启](#windows-重启)
- [侧信道速览](#侧信道速览)
- [后台任务完成 → 手机](#后台任务完成--手机)
- [Android 输入区](#android-输入区)
- [语音输入（SenseVoice）](#语音输入sensevoice)
- [Android 顶栏](#android-顶栏)
- [聊天 UI 消息过滤](#聊天-ui-消息过滤)
- [消息长按与文本选择](#消息长按与文本选择)
- [统一错误日志](#统一错误日志)
- [测试覆盖](#测试覆盖)
- [部署与使用](#部署与使用)

---

## 目标与能力一览

手机上能远程操控 coara 的哪些功能、入口在哪、等价于 PC 端什么操作。

| 能力 | Android 入口 | 等价 PC 操作 | Matrix 命令 |
|------|--------------|--------------|-------------|
| 新会话 | 输入框 **长按** → 命令面板 `/new` | CLI `/new` | `/new` |
| 空闲自动新会话 | 本地镜像时钟看似超时 → 静默查询 `[COARA_STATUS]`（**不**自行发 `/new`）；Root 自动 `/new`（janitor 续期）后强推 status（`session_event=new_session`）画「新会话」分隔线 | Root 距上次**用户消息** ≥ **2 小时** → 自动新会话并强推 status | — |
| 中断回合 | **长按顶栏 typing `...`**（运行中才有意义） | 运行中 **Ctrl+C** 或 `/stop` | `/stop` |
| 重启 coara | 命令面板 `/restart` | CLI `/restart` | `/restart` |
| 切换模型 | 命令面板 `/model` → 二级列表点选 | CLI `/model` | `/model <provider/model>` |
| 切换工作空间 | 左上角工作空间浏览器 → 点名称切换 | CLI `/ws switch` | `/ws switch <名称>` |
| 思考模式 | 命令面板「思考」行：开/关 + 低/中/高（档位仅支持的模型可点） | `/thinking` `/thinking on\|off\|low\|medium\|high` | 同左 |
| 用量/状态 | 不在面板（手机端低频）；需要时 PC 端 CLI 查询 | — | `[COARA_USAGE]` / `[COARA_STATUS]` query（协议仍支持） |
| 附件 | 左侧 **+**（打开时变为 ×；点消息列表可收起） | — | 普通消息 |
| 设置 | **双击标题** | — | — |

**空闲自动新会话（会话间隔）**：默认 **2 小时**，**仅 Root 发起**。

- **标准**：距上次任一前端用户活动的时间间隔（不是 App 退后台时长）。权威时钟在 Root：`_last_user_activity_at`（`session.idle_timeout_seconds`，默认 `7200`）。
- **CLI/Root**：空闲超时后 `start_new_session`；并通过 `[COARA_STATUS]` 推送 `session_id` / `last_user_activity_at` / `idle_timeout_seconds`（活动节流约 60s；新会话强制推）。
- **Android**：`SessionInactivityTracker` 是 Root 时钟的本地镜像（房间消息、本机发送、或 status 推送都会前进）。前台每 60s 若本地看似超时，只静默查询 status，**不**发 `/new`。收到 `session_event=new_session` 推送（手动 `/new` 与 janitor 空闲自动续期都会强推）且 `session_id` 变化时插入「新会话」分隔线（与手动 `/new` 防抖 5s）。
- **Web UI**：无独立自动 `/new`，走 Root。

分隔线语义统一：新会话画「新会话」，工作空间切换（任意来源：手机面板 / CLI / Web / LLM）画「已切换到工作空间 X」——Root 在 `workspace_switched` 事件时强推 status（`session_event=workspace_switch`），App 隐藏切换回执文本与 `[系统] 已切换到工作空间` 行。

新会话分隔线画在边界后首条消息**上方**；slash 正文不入聊天气泡（见「聊天 UI 消息过滤」）。

**顶栏在线点**：绿 = `Connected`；灰 = 未连接 / 连接中 / 错误。不再用 sync 新鲜度做中间色（切进 App 时 `lastSyncSuccessAt` 未刷新会误显「黑/黄」）。sync 单次请求超时仍收紧为服务端等待时长+10s（隧道约 40s），死连接尽快失败重连，不继承全局 300s 读超时。

**设计原则**

1. **slash 命令集中**在输入区长按的命令面板，且只留高频项（新会话/模型/思考/重启 + `/report` 报告置底）；中断走顶栏 typing `...` 长按，用量/状态/帮助不上手机；**系统设置**放双击标题——职责分离
2. 控制消息走 Matrix **标准文本**，CLI / Matrix / Android 三端共用 `/new`、`/stop`、`/restart` 等命令，**不经 LLM**
3. Matrix **prefilter** 与 **command handler** 分层——`/stop` 可即时中断，不进 LLM 回合
4. 重启先 **ack 再重启**——`request_coara_restart` 延迟 0.6s，保证「正在重启考拉…」发出
5. 跨平台重启用 **`python -m src.cli.main`**——不依赖 Windows `Scripts\coara` 路径
6. 命令**不在聊天回显**（slash 正文对用户隐藏）；后端结果正常入房展示；`/new` 写本地会话分界线
7. 默认启动 `coara`（无前端 flag）即启用 CLI + Web + Matrix；连上 GoMatrix 后即可与 App 通信

---

## Matrix 命令链路

手机上发一条 `/new` 或 `/stop`，PC 端代码按什么顺序处理。

### 端到端流程

1. Android `sendMessage("/new"|"/stop"|…")` → Matrix 房间。
2. coara `/sync` 收到 → `route_inbound_text_event`（跳过自己 echo + mention 路由）。
3. `prefilter_matrix_text_event` 按序消费（`src/matrix_client/ingress_helpers.py`）：
   - approval / vault 回执 → 恢复等待中的 future，consume
   - `[COARA_UPDATES]` 入站 → 丢弃，consume
   - mobile sync query（`type:"query"`）→ 回 payload，consume
   - `/stop` → `interrupt_current_turn("user_stop")`，**consume**（即时中断，不进回合）
   - `/new`、`/restart` → 若有回合则先 `interrupt_current_turn`，**不 consume**（继续走命令处理出 ack）
4. `stream_coara_reply_to_matrix` → `chat_commands.try_handle_matrix_chat_command`：
   - `normalize_remote_command_body` 先剥掉来源标签包装（`<手机消息>`/`<web消息>`，`src/core/message_tags.py`）再匹配命令
   - 委托共享命令层 `src.coara.commands.execute_command`（与 CLI / Web 同一实现）
   - `/model` `/ws` `/thinking` `/usage` `/status` `/new` 执行后追加 push 对应隐藏 payload（见 `mobile_sync.py`）
5. 非命令文本 → `RootCoara.process_message` 正常回合。

### 命令语义（摘要）

| 命令 | Matrix 回复 | 副作用 |
|------|-------------|--------|
| `/new` | `` 已开始新会话：`<session_id>` `` | 中断当前回合；新 session 边界 |
| `/stop` | `已中断当前回合` / `当前没有运行中的回合` | 等同 Ctrl+C；整轮 history 回滚 |
| `/restart` | `正在重启考拉…` | ack 后 0.6s shutdown + respawn |
| `/model <key>` | `已切换 → provider/model` | 持久化偏好；push `[COARA_MODELS]` |
| `/ws switch <名称>` | 聊天内不展示文本；改画「已切换到工作空间 X」分隔线 | 切换活跃工作空间；push `[COARA_WORKSPACES]` + status（`session_event=workspace_switch`） |
| `/thinking on\|off` | `思考模式：开/关（本会话）` | 本会话覆盖；push `[COARA_THINKING]` |
| `/thinking low\|medium\|high` | `思考模式：开（本会话）· 档位：低/中/高`（不支持档位的模型附提示） | 同上 |

**`/new` 确认行的格式是协议**：`` 已开始新会话：`<session_id>` ``——反引号包 session id，因为 Android 用正则 `^(已开始|已启动)新会话：`[^`]+`$`（`ApprovalMessage.kt` 的 `SESSION_BOUNDARY_ACK_REGEX`）识别并隐藏这条 ack。PC 端在 `src/coara/commands/session.py` 里有对应注释，改文案必须两端同步。

### Runtime 源码

| 文件 | 职责 |
|------|------|
| `src/matrix_client/chat_commands.py` | Matrix slash 解析与执行（`normalize_remote_command_body`） |
| `src/matrix_client/ingress_helpers.py` | prefilter；`/stop` 与 mobile sync query |
| `src/coara/commands/session.py` | `/new` `/stop` `/restart` 实现（ack 文案） |
| `src/coara/mobile_sync.py` | models / workspaces / thinking / usage / status 面板 payload |
| `src/coara/inbound_router.py` | 工作流结果分派 |
| `src/runtime/restart.py` | 重启 argv 构建 + 0.6s 延迟 + 子进程拉起 |

---

## Windows 重启

**问题**：`os.execv(python, [python, *sys.argv])` 会把 `...\Scripts\coara` 当作 `.py` 执行而失败。

**实现**（`src/runtime/restart.py`）：

- `build_restart_command`：识别 `coara` / `coara.exe` → `[python, -m, src.cli.main, *原参数]`；`python -m src.cli.main` / `python xxx.py` 形式原样保留
- `request_coara_restart`：先返回 ack（`正在重启考拉…`），0.6s 后再 `restart_coara_now`——shutdown → `Popen` 新进程 → `os._exit(0)`
- 延迟任务持有强引用，避免被 GC 提前回收

测试：无（原 `tests/test_coara/test_process_restart.py` 已随重构移除）。

---

## 侧信道速览

除 slash 命令外，PC 与 App 之间还有一套隐藏协议消息（同样在房间里同步，但聊天 UI 默认隐藏）。**协议格式与完整负载的 canonical 在 [`MATRIX_APP_LINK.md`](./MATRIX_APP_LINK.md) §5**，这里只列总表：

| 能力 | PC → App 协议 | App → PC 协议 | UI 表现 | 源码 |
|------|---------------|---------------|---------|------|
| 工具审批 | `m.coara.approval` 自定义 msgtype（非 `[COARA_*]` 文本信封；协议见 MATRIX_APP_LINK §5.1） | `m.coara.approval_reply`（自定义 msgtype） | 内联审批卡片 | `approval_bridge.py` · `ApprovalMessage.kt` |
| 宝箱解锁 | `[COARA_VAULT] …` | `[COARA_VAULT_REPLY] …` | `VaultPromptCard` | `vault_bridge.py` · `VaultMessage.kt` |
| 编辑 diff | `[COARA_DIFF] {JSON}` | — | diff 卡片 | `diff_bridge.py` · `DiffMessage.kt` |
| 工作空间动态 | `[COARA_UPDATES] {JSON}`（含 `pending` 待批复列表） | — | 工作空间菜单红点 + 抽屉待批复区 | `updates_matrix_sync.py` · `UpdatesSyncParser.kt` |
| 批复指令 | `[COARA_UPDATE_ACK] {JSON}` | `[COARA_UPDATE_CMD] {action:read/archive/mark_read/review,…}` | 抽屉卡片操作（知道了/忽略/批复/切入） | `update_cmd_bridge.py` · `UpdatesSyncParser.kt` |
| 模型列表 | `[COARA_MODELS] {type,current,choices}` | 同信封 `{type:"query"}` | `/model` 二级列表 + 行右侧当前模型 | `mobile_sync.py` · `MobileSyncParser.kt` |
| 工作空间列表 | `[COARA_WORKSPACES] {type,active_id,workspaces}` | 同信封 `{type:"query"}` | 左侧工作空间浏览器 | 同上 |
| 思考状态 | `[COARA_THINKING] {type,enabled,level,supports_levels,…}` | 同信封 `{type:"query"}` | 「思考」行开/关 + 低/中/高 | 同上 |
| 用量 | `[COARA_USAGE] {type,summary,input_tokens,…}` | 同信封 `{type:"query"}` | 不在面板展示（PC CLI 查询；协议仍支持） | 同上 |
| 状态 | `[COARA_STATUS] {type,summary,workspace}` | 同信封 `{type:"query"}` | 不在面板展示（驱动空闲时钟与分隔线；协议仍支持） | 同上 |

**安全要点**：宝箱密码通过侧信道传输，**永远不会进入 LLM 对话上下文**。

### 面板数据同步（`src/coara/mobile_sync.py`）

| 时机 | 行为 |
|------|------|
| coara 连上 Matrix 通知房间 | 主动 push：models / workspaces / thinking / usage / status |
| App 打开 slash 面板 | 静默 query models、thinking；有缓存先展示 |
| App 展开 `/model` | 再 query models |
| App 打开工作空间浏览器 | **总是** query workspaces（避免 prefs 里陈旧 `active` 与 status 分隔线不一致） |
| 聊天执行 `/model` `/ws` `/thinking` `/usage` `/status` `/new` | 命令可见输出（若有）后再 push 对应 payload |

- 静默查询由 ingress prefilter 消费，经**与回合无关**的 `configure_mobile_sync_sender` 回传，不打断进行中的回合
- App 将最新 payload 持久化 SharedPreferences；模型选中态用绿色圆点（与顶栏在线点同色）
- 面板数据**不是**监听 `providers.yaml` 热更新：改完 provider 配置需重启 coara 后再展开面板
- 点选模型发 `/model provider/model`（不用序号）；聊天里「已切换 → …」回执**可见**，命令本身隐藏
- 「思考」行：开/关发 `/thinking on|off`；低/中/高发 `/thinking low|medium|high`；面板保持展开，由 `[COARA_THINKING]` 回包刷新（`supports_levels=false` 时档位灰显）
- 「用量」「状态」已从面板移除（低频，需要时 PC 端 CLI 查询）；仅 `/model` 行右侧显示当前模型
- 状态摘要形如 `v8 · 对话 5 条`（当前工作空间 + 本会话上下文条数）；`[COARA_USAGE]` / `[COARA_STATUS]` query 协议仍支持，App 侧保留解析
- **「当前」双源对齐**：分隔线「已切换到工作空间 X」读 `[COARA_STATUS].workspace`；浏览器「当前」读 `[COARA_WORKSPACES].workspaces[].active`。App 在 `applyStatusSync` 里用 status.workspace 校准 active（`MobileSyncParser.reconcileActiveWorkspace`），避免只收到 status、workspaces 推送丢失/缓存陈旧时两边对不上
- **历史不同步面板**：`hydrate` / 上拉更早消息里的 `[COARA_*]` / 动态推送只吞掉气泡，**不**回调面板状态；只有 live sync 才画分隔线或改「当前」。侧栏点切换时本地先乐观画「已切换到工作空间 X」，避免时间线里旧的 `workspace_switch`（例如曾切到 gora）盖住刚点的 nx

---

## 后台任务完成 → 手机

PC 上跑完的后台任务（bash / 子 agent），结果怎么推给手机。

消息内容体系下，后台任务完成**不再**自动跑 `background_auto_run` 回合（`src/coara/root.py` `_on_background_task_complete`）：完成通知按**发起 session** 路由（`_resolve_session_coara_for_event`）——

| 目标 session 状态 | 行为 | 手机可见性 |
|-------------------|------|-----------|
| 忙（回合进行中） | `submit_continuation_input` 注入当前回合，下一 ReAct 迭代拾取 | 推送随当前回合通道（Matrix 发起的回合照常推回房间） |
| 空闲 | 完成内容 park 进驻会话历史并落盘，**不自动跑回合**；不再落工作空间动态收件箱 | 不单独推 Matrix；用户下次发话时 LLM 自然看到 |

要点：

- 任务发起端仍记录在 `TaskRecord.origin_source`（`src/coara/turn_source.py`），忙时注入的回合按发起端通道推送
- 工具摘要行（`✓/✗`）不推送给用户通道
- 后台任务（bash / agent）完成内容经完成通知链注入发起会话历史（不落工作空间动态收件箱）

---

## Android 输入区

底部输入栏的单击/长按/上滑分别做什么，以及面板如何开关。

### 手势分工

```text
输入框
├── 单击 → 聚焦 + 弹出键盘
├── 长按 → 震动 + slash 命令面板（上方弹出，不弹键盘；输入区禁止选区/复制）
└── 上滑（超过 80dp）→ 发送

左侧 +
└── 点击 → 附件面板（打开时图标变为 ×；点消息列表可收起）

消息区
└── 按下 → 输入框失去焦点；附件/命令面板打开时可点列表收起

面板打开时
├── 输入框被透明遮罩盖住：点输入区会收起面板而不是弹键盘
├── 清除焦点、隐藏 IME
└── 不使用 imePadding（面板不被键盘顶起）
```

### 面板状态机

`ChatInputBar.kt` 中 `InputBarOverlay`：`None` | `Attach` | `QuickCommands`

| 面板 | 位置/高度 | 内容 |
|------|------|------|
| `SlashCommandPanel` | 输入栏**上方**，高度随内容自适应（上限约 8 行，行间淡分割线） | 顺序：`/new` → `/model` → `/thinking` → `/restart` → `/report`（报告置底）。`/model` 可展开二级列表；「思考」行内嵌开/关与档位；仅 `/model` 行右侧显示当前模型 |
| `AttachMenuPanel` | 输入栏下方，全宽 dock | 拍照、相册、文件 |
| 模块导航抽屉 | 左侧推出（约占屏宽 5/6） | 顶部当前空间下拉（展开空间列表点选切换），下挂模块项：对话 / 批复（带待办角标）/ 工作流 / 记录 / 用量 / 配置；批复、用量为全页原生页，其余占位待二期 |

### 关键实现

| 文件 | 职责 |
|------|------|
| `ui/ChatInputTextField.kt` | 单击 vs 长按；禁选区（AndroidView 封装 EditText） |
| `ui/ChatInputBar.kt` | overlay 状态机、附件/命令面板、80dp 上滑发送、语音按钮 |
| `ui/InputBarPanels.kt` | `SlashCommandPanel` + `AttachMenuPanel` + 二级列表 |
| `matrix/ChatViewModel.kt` | `sendSlashCommand` / 面板 sync 状态（models / usage / status / thinking / workspaces） / `currentScreen` 模块页状态机 / `coaraApi` REST 客户端 |
| `ui/ChatScreen.kt` | ViewModel 接线、发送后滚到底、点列表收起面板；chatContent 顶层按 `currentScreen` 切换模块页 |
| `ui/AppScreen.kt` + `ui/ModuleScreens.kt` | 六模块 sealed 状态机 + 模块页骨架（返回箭头 + 标题 + 动作区）与占位页 |
| `ui/ReviewScreen.kt` | 全页批复中心（复用 `PendingUpdateCard`，数据走 Matrix 信道不变） |
| `ui/UsageScreen.kt` | 用量页（经 `/coara-api` REST 拉 `/api/usage/dashboard`） |
| `coara/CoaraApiClient.kt` | REST 客户端：`{homeserver}/coara-api` + Matrix Bearer token；错误分 Success / Unauthorized / PcUnreachable / Error |

模块页的 REST 数据通道：gomatrix 反向代理 `/coara-api/*` → PC coara web server `/api/*`（注入 dashboard token，App 只用 Matrix 令牌），见 [MATRIX_APP_LINK](./MATRIX_APP_LINK.md) §6.5。

**注意**：输入框勿用 `fillMaxHeight()`（会撑满全屏）。

---

## 语音输入（SenseVoice）

按住说话转文字的实现方式（`speech/SenseVoiceRecognizer.kt`）。

- **端侧识别**：SenseVoice-Small INT8 模型（`models/sense_voice/model.int8.onnx` + `tokens.txt`）+ ONNX Runtime，全离线，不出手机。模型文件不入 git，需手动放入 `app/src/main/assets/models/`；首次使用时从 assets 拷贝到 `filesDir`。
- **点按开始、点按停止**：点麦克风开始录音（图标变为停止键），再点一次结束；结束后对整段 PCM 缓冲**一次性推理**，无实时中间结果。
- **光标处插入**：识别结果插入输入框当前光标位置，不清空已有文字，可多次连续录音拼接；输入框从未聚焦过时从末尾接续。
- **加载时机**：进聊天界面即初始化识别器（`ChatScreen` 的 `LaunchedEffect`）；相对 `Application.onCreate` 是懒加载——App 启动时不占内存。

---

## Android 顶栏

聊天页顶部一栏每个区域的行为。

| 区域 | 行为 |
|------|------|
| 左侧两线图标 | 打开工作空间动态菜单（红点 + 未读数 + 最新摘要；点开某工作空间本地清零，已读不回写 PC） |
| 标题 / Logo | 双击 → 设置 |
| `...` 动画 | `isAgentTyping`：本地等待 **或** Matrix `m.typing`；**turn 结束**（typing 停）才关掉，不因第一条助手气泡提前停；90s 本地兜底超时防丢 clear 卡死，sync 失败恢复不复位 |
| 连接状态点 | 绿=已连接，灰=断开 |
| agent 选择 | 点击标题区展开 agent 菜单 |

> 设置改由双击标题进入；中断统一走命令面板 `/stop`。

---

## 聊天 UI 消息过滤

哪些房间消息在手机上不显示，避免刷屏。

控制类消息在 Matrix 房间仍会同步，手机 UI 默认**不展示**。规则集中在 `ui/ApprovalMessage.kt` 的 `shouldHideInChatUi`：

| 方向 | 隐藏内容 |
|------|----------|
| 发出 | 审批/宝箱回执、动态 sync、mobile sync、所有 slash 命令正文；Root 会话边界后 **120s 内**隐藏裸 `/new` 回声（只留分隔线） |
| 收到 | `[COARA_UPDATES]`、新会话 ack（`` 已开始新会话：`…` ``）、stop ack（`已中断当前回合` / `当前没有运行中的回合`）、restart ack（`正在重启考拉…`）、宝箱 ack（`[vault] …`）、工具摘要行（`✓/✗`）、工作空间切换 ack（行首 `已切换到工作空间 …`）与 `[系统] 已切换到工作空间 …` 行（均由分隔线承载） |

手动 `/new` `/stop` `/restart` 发出不在聊天展示；结果/分界线按各自规则展示。`/model …` 命令隐藏，「已切换 → …」**可见**。

---

## 消息长按与文本选择

聊天气泡上的长按菜单与文本选择手势。

| 内容类型 | 长按行为 |
|----------|----------|
| Markdown 正文 / 公式 | 文本选择（选择工具栏与长按菜单合并为单一工具栏；单击空白或工具栏外退出选择） |
| 代码块 / Diff 卡片 | 上下文菜单（复制等） |
| 文件附件 | 打开 / 保存到 `Download/coara` |
| 任意消息 | 复制 / 引用 / 收藏（Matrix `[COARA_COLLECT]` → PC `records/user/`；取消同步删 PC 条目） / 多选 / 本地删除（持久化已删 id 到 `deleted_message_ids.json`，防 sync 回灌；非 Matrix redact） |

带引用发送时，正文为 `<引用内容>…</引用内容>`（多条则多个引用块）+ 用户新输入；引用图片额外附 `[COARA_QUOTE_MXC]` 标记（协议见 [`MATRIX_APP_LINK.md`](./MATRIX_APP_LINK.md) §5.7）。

策略见 `ChatLongPressGestures.kt`、`ChatMessageInteraction.kt`、`ChatTextSelection.kt`、`ChatQuoteUtil.kt`、`MessageCache.kt`。

---

## 统一错误日志

排障时去哪看错误。

运行时错误写入 **`<workspace>/.coara/logs/errors.jsonl`**。

| 入口 | 说明 |
|------|------|
| `src/core/error_log.py` | 写入与分类 |
| `/new` | `CommandResult.data` 可带 `errors_log_path`（**不**写入用户主文案）；`/status` 亦不展示该路径 |

排障直接打开 `<工作空间>/.coara/logs/errors.jsonl`。

---

## 测试覆盖

| 文件 | 覆盖 |
|------|------|
| 模块已移除（原 `tests/test_coara/test_process_restart.py`） | Windows 重启 argv |
| `tests/test_matrix_client/test_mobile_sync.py` | models / thinking / usage / status payload |
| `tests/test_matrix_client/test_mobile_sync_query.py` | 静默 query（含 usage / status） |
| `android-app/.../ui/ApprovalMessageTest.kt` | 审批解析、隐藏规则 |
| `android-app/.../coara/MobileSyncParserTest.kt` | 面板信封解析（含 query 忽略） |
| `android-app/.../coara/UpdatesSyncParserTest.kt` | 动态信封解析 |

---

## 部署与使用

1. **PC**：`pip install -e .`，直接 `coara`（默认三端；gomatrix 由 coara 托管自动拉起）
2. **Android**：编译安装 Debug APK；扫码配对见 [`MATRIX_APP_LINK.md`](./MATRIX_APP_LINK.md) §3.2
3. **设计预览**（可选）：`android-app/design-preview` 下的纯 HTML 预览——输入栏 `chat-ui.html`、字体对比 `font-compare.html`（浏览器直接打开即可）
4. **重启期间**：Matrix 短暂断连，新进程起来后自动恢复

### 日常使用速查

| 我想… | 操作 |
|-------|------|
| 打字 | 点输入框 |
| 语音输入 | 点麦克风开始，再点一次停止出字 |
| 新会话 / 切模型 / 切工作空间 / 开思考 | 长按输入框 → 对应项 |
| 重启 PC coara | 长按 → 重启 |
| 发图/文件 | 点 **+** |
| 中断当前回合 | 长按顶栏 typing `...` → 中断 |
| 进设置 | 双击标题 |
| PC 本地中断 / 重启 | Ctrl+C 或 `/stop`；`/restart` |
| 看错误 | `<工作空间>/.coara/logs/errors.jsonl` |
