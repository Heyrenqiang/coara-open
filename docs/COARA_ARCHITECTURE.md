# coara v8 架构详解

> 本文档依据当前仓库代码逐条核实编写，描述**现状**。阅读前先了解 [`docs/CONVENTIONS.md`](CONVENTIONS.md) 的文档分层约定；文档索引见 [`docs/README.md`](README.md)。
>
> **分层铁律与四梁八柱见 [`docs/架构契约.md`](架构契约.md)（一页纸，改动前必读）**——支撑层与 `core` 严禁 import `coara`/`cli`/`agent`/`tools`，共享逻辑一律下沉为 core 原语。
>
> **重要时效注记（2026-09-08，2026-09-10 修订）**：WDL **执行层**已剥离为独立 WDL 软件（仓库顶层 `wdl/` 子项目，含引擎/持久化/调度/画布工作台）。本文中 WorkflowEngineRuntime 子进程、WorkflowBridge IPC、`src/coara/workflow_engine.py`、`src/workflow/persistence|scheduler|scanner` 相关段落描述的是剥离前架构，代码已删除或迁至 `wdl/src/wdl/`。
>
> **2026-09-10 修订**：图**内核**（`src/workflow/core/` 的 model/semantics/serde）已回到 coara 自持 —— 它是 `wdl/src/wdl/core/` 的自持副本，目的是让 coara 的内嵌 flow 能力不依赖 wdl-engine 是否安装（wdl 是独立软件，对 coara 为 `[wdl]` extra）。分叉声明见 `src/workflow/core/model.py` 文件头，漂移校验见 `scripts/dev/check_core_fork.py`。因此下文提到 `src/workflow/core/*` 的路径**重新指向 coara 内文件**；而 `kernel_runner` / `persistence` / `scheduler` / `scanner`（引擎侧持久宿主）仍归 wdl 软件，coara 不含。coara 保留的是 WDL 生产侧（orchestrator/flow 现场模式 + `src/workflow/draft_*` + 自持图内核）。进程模型、动态归属、结果投递等相关章节待按新架构重写。

### core 原语模块（共享逻辑唯一归宿）

| 模块 | 职责 |
|---|---|
| `core/tool_base.py` | `BaseTool`/`ToolResult`/`ToolInvocation`/`ToolKind` 工具契约（46+ 消费方） |
| `core/events.py` | `TraceEvent` 运行时事件契约（全进程单一事实源） |
| `core/message_tags.py` | 消息包装标签库（REMOTE/CONTINUATION/SUBAGENT/… + system_info/reminder） |
| `core/read_format.py` | 读取输出行号格式化（Cursor 风格 `LINE|content`） |
| `core/system_messages.py` | 全局系统消息 JSONL store（`/message` 历史） |
| `core/process.py` | 子进程解码、PowerShell 生成（跨平台） |
| `core/text.py` | 按行切片、绝对路径校验 |

关键落点：轮播词库 `records/loading_phrases.py`、压缩提示词 `context/compression_prompt.py`、待办循环控制 `todos/turn_control.py`。

---

## 一、系统总览（先给结论）

coara 是一个 Python 多智能体运行时：**无头内核（daemon/tray）持有 RootCoara** 面向用户处理对话，CLI/Web/Matrix 均为经 EndChannel/WS 接入的平等端；需要时在同进程内创建轻量子智能体（协程，不是子进程），或把正式的多步骤流程交给独立的 WorkflowEngine 子进程执行。

记住五条主线，后文都是对它们的展开：

1. **节点同构**：Root 和子智能体共用同一个基类 `CoaraBase`，差别只在接线（注册了哪些工具、白名单、上下文标志），不在类型。
2. **回合循环**：`CoaraBase.process_message()` → `run_turn_loop()`（`src/coara/turn_orchestrator.py`）是运行时心脏，负责上下文准备、LLM 调用、工具执行、守卫与 trace。
3. **工具分层注册**：进程级 3 个 + 每实例运行时工具 + Root 专属工具，可见性再经白名单/plan-mode/deferred 过滤。
4. **三条消息通道**：`EventBus`（发后即忘的观测事件）与 `UnifiedScheduler`（必须进入对话循环的消息）分工明确；trace 经 EventBus 汇入 TraceStore 供 Web UI 展示。
5. **进程模型**：无头内核（daemon/tray，持有 RootCoara）+ WorkflowEngineRuntime（multiprocessing 子进程，队列 IPC）+ 子智能体（进程内协程）+ bash 后台任务（真子进程）；**终端只是端**——裸 `coara` 拉起/复用常驻内核后 attach（`src/cli/main.py`）。

全系统挂载关系一图览：

```text
coara 运行时的进程与执行体
├── 内核进程（daemon/tray：`coara tray`，无头常驻，终端关闭不杀）
│   ├── RootCoara
│   │   ├── CoaraBase 回合循环（process_message → run_turn_loop）
│   │   ├── EventBus（trace 汇聚中心）+ UnifiedScheduler（入站消息队列）
│   │   ├── WorkflowBridge（工作流子进程 IPC 桥）
│   │   └── 全局服务：workspace_manager / vault / reminder / event_source
│   ├── 子智能体（同进程协程，delegate 工具创建）
│   │   ├── 前台：asyncio.create_task；结果由 LLM 调 delegate(action="wait") 显式收齐，回合出口只软提醒一次
│   │   └── 后台：BackgroundAgentManager，完成后经 EventBus 通知注入
│   ├── WorkspaceSession（每个工作空间一个对等 CoaraBase，含启动空间）
│   ├── bash 后台任务子进程（新进程组，输出写 .coara/tasks/）
└── WorkflowEngineRuntime 子进程（mp.Process，按需拉起，mp.Queue 双向 IPC）
    ├── KernelGraphRunner：执行内核投影（节点=智能体，边=拓扑）
    ├── delegate 池：节点复用 CoaraBase 实例
    └── WorkflowPersistence：SQLite 持久化，终态 30 天归档
```

### 进程与执行体一览

| 执行体 | 形态 | 创建方式 | 通信 |
|--------|------|----------|------|
| RootCoara | 内核进程内 asyncio 对象 | daemon/tray 常驻内核启动时创建（裸 `coara` 拉起后 attach） | 直接方法调用；端经 EndChannel/WS |
| 子智能体 | 同进程 asyncio 协程 | `delegate` 工具 | 协程 await / continuation_input |
| WorkflowEngineRuntime | 独立子进程 | Root 按需拉起 | `multiprocessing.Queue` 双向 |
| bash 后台任务 | 独立 OS 子进程（新进程组） | `shell` 工具后台模式 | 输出写文件，完成发 EventBus |

---

## 二、目录结构

```text
src/
├── cli/              # CLI 客户端：attach 传输/UI、attached_chat_runner、显示控制、scrollback、spinner、托盘入口
├── coara/            # 内核：RootCoara / CoaraBase / 回合循环 / 命令层 / 输出路由(segment+end_registry) / 端注册 / 工作流桥 / 事件总线 / 调度器
├── core/             # 跨层原语：tool_base / events / message_tags / process / text / read_format / system_messages（分层铁律：支撑层禁 import 上层）
├── agent/            # 工具执行器、hooks、循环检测、回合控制
├── background/       # bash 后台 runner、TaskStore
├── context/          # 上下文窗口管理与压缩（含 compression_prompt）
├── event_sources/    # 事件源（file watch / poll / webhook）
├── examples/         # 示例工作空间安装（stocks-watch）
├── llm/              # Provider 抽象（anthropic / openai / kimi / zhipu / minimax / agnes / custom）、流式、重试、profile、service
├── matrix_client/    # Matrix 机器人集成
├── prompt/           # PromptBuilder、AgentRegistry、YamlPromptLoader、环境注入
├── records/          # 本地记录统一存储（facade / migrate / store）+ 轮播词库 loading_phrases
├── reminders/        # 个人提醒服务
├── runtime/          # 工具输出 spill、用量统计
├── skills/           # 技能发现、加载、激活
├── todos/            # 会话 todo 域（含 turn_control 循环控制）
├── tools/            # 工具注册表、策略、缓存、builtin/、沙箱、内容策略
├── ui/               # Web 服务：web_server / turn_stream / attach_ws / trace_broadcast / web_views（视图存储）/ TraceStore / handlers
├── utils/            # 剪贴板、图像处理、多模态
├── vault/            # 加密宝箱
├── workspace/        # 工作空间注册表、VFS、权限、updates（动态存储）
└── workflow/         # 工作流内核（core 图模型/serde/semantics）、持久化、触发器、结果投递

tests/  skills/  android-app/  gomatrix/  deploy/  docs/  ref-doc/
```

---

## 三、节点模型：一套运行时，多种角色

先看整体分层（自顶向下，括号内是关键源码）：

```text
分层架构
├── 用户交互层
│   ├── CLI（click + prompt_toolkit + Rich）
│   ├── Web UI（aiohttp HTTP/WebSocket + React SPA）
│   └── Matrix 前端（matrix-nio 机器人）
├── 运行时入口层
│   └── RootCoara（root.py / root_lifecycle.py）
│       ├── 注册 Root 专属工具、加载技能、启动调度消费循环
│       ├── 持有 EventBus + UnifiedScheduler + WorkflowBridge
│       └── 管理 WorkspaceSession 与全局服务
├── 核心运行时层（所有节点共用）
│   ├── CoaraBase.process_message → run_turn_loop（base.py / turn_orchestrator.py）
│   ├── ToolExecutor + hooks + LoopDetector / TurnStagnationGuard（agent/）
│   ├── Provider.complete()（turn_completion.py）
│   └── ContextWindowManager（压缩 + 守卫，context/window.py）
├── 编排协调层
│   ├── UnifiedScheduler + inbound_router（入站消息进对话）
│   ├── WorkflowBridge → WorkflowEngineRuntime 子进程
│   └── BackgroundAgentManager（后台 delegate 生命周期）
├── 能力层
│   ├── 内置工具（file_io / web / delegate / workflow / shell / …）
│   ├── 挂起的内置工具（截图/打印/邮件等）+ 技能（SKILL.md）
│   ├── Prompt 系统（agents/*.yaml + *.md 配对）
│   └── Vault 宝箱
└── 基础设施层
    ├── ConfigManager / loguru 日志 / core.types
    ├── AbortController（取消原语，core/abort.py）
    └── 持久化：TraceStore / TaskStore / SubagentStore / 用量统计
```

### 3.1 结论

所有运行时节点（Root、子智能体、工作空间会话实例）都是 `CoaraBase`（`src/coara/base.py`）的实例。没有独立的「子智能体类」。一个节点能做什么，由以下五项共同决定：

| 决定因素 | 来源 | 作用 |
|----------|------|------|
| `delegate_depth` | 构造参数（Root=0，子智能体=父级+1） | 为 0 才注册 `delegate` 工具；≥1 时执行 delegate 直接报错 |
| `is_owner_context` | 构造参数（Root=True） | 为 False 时隐藏 `owner_only` 工具 |
| 工具白名单 `_tool_whitelist` | 子智能体 YAML 的 `tools.include` | 过滤可见工具 |
| plan-mode 状态 | `PlanModeTool` 切换 | plan 模式下只暴露固定允许清单（见 §7.4） |
| 调用层审批策略 | `config.yaml security.call_policy.prompt` | 决定哪些调用必须人工确认（`src/agent/tool_policy.py`） |

### 3.2 RootCoara

`RootCoara`（`src/coara/root.py`）继承 `CoaraBase`，构造时固定 `user_facing=True`、`is_owner_context=True`、`delegate_depth=0`。它在基类之上多持有：

- `event_bus`（`EventBus`）、`scheduler`（`UnifiedScheduler`）
- `workflow_bridge`（`WorkflowBridge`，工作流子进程 IPC 层）
- `workspace_manager`、vault/reminder/event_source 等全局服务
- `_sessions`（各工作空间的 `WorkspaceSession` 缓存）与 `_foreground_session_id`（当前前台）

Root 在初始化末尾执行 `self.set_trace_sink(self.event_bus.publish)`，因此 `root.event_bus` 是全系统结构化事件的唯一汇聚中心，CLI / Web UI / Matrix 都直接订阅它。

### 3.3 内置子智能体

子智能体配置由 `src/coara/builtin_agents.py` 从 `src/coara/prompts/agents/{name}.yaml` + `{name}.md` 配对文件加载。共 4 个：`coaras`/`aide` 可由模型委派（下表），`janitor`/`daily` 系统专属（`SYSTEM_ONLY_SUBAGENT_TYPES`，由运行时派发，delegate schema 的 `subagent_type` 枚举排除它们，仅 `system_dispatch=True` 的内部调用可启动）。工具白名单逐个核实如下：

| 子智能体 | 定位 | `tools.include`（YAML 原文） | `tools.exclude` |
|----------|------|------------------------------|------------------|
| `coaras` | 并行分身（工程 / 只读摸底） | `["*"]`（继承父级可见工具集） | 无 |
| `aide` | 资料调研与头脑风暴（后台） | `read` `grep` `glob` `web_search` `web_fetch` `todo` | `write` `edit` `delete` `shell` `delegate` `skill` |

系统专属的工具白名单（与可委派者同表核对）：`janitor`（管家，三职责：概况维护 + 记录 + 消息过目）= `read` `write` `edit` `delete` `grep` `glob` `shell` `todo` `ws` `record` `local_search`（exclude `delegate` `skill`）；`daily` = `read` `glob` `grep` `record` `local_search`（exclude `write` `edit` `delegate` `skill` `shell` `delete` `ws`）。

补充规则（代码行为）：

- `coaras` 的 `include: ["*"]` 在 `builtin_agents.py` 中被视为「不设置白名单」，delegate 时取父级可见工具名作为白名单。但实例上**没有** Root 专属注册（`ws`/`reminder`/`vault`/`plan_mode`/`event_source` 只在 Root 与 WorkspaceSession 初始化时注册；工作流经 Root 激活 `workflow` 技能后的 `orchestrator` 工具），所以这些名字即使在白名单里也无工具可调。只读摸底时在 prompt 写明禁止修改（原 `explore` 已并入）。
- 任务型 `coaras`（仅前台）有主⇄子双向通道：子智能体经 `interact` 工具把中间沟通以 `<子智能体消息>` 标签注入父会话（普通文字不转发；最终交付=最后一条文本，不走工具；收到 `<途中消息>` 必须用 interact 回应）；父级可用 `delegate(action="message", task_id=…)` 途中干预（下一迭代边界以 `<途中消息>` 呈现），硬停走 `delegate(action="stop", task_id=…)`。`aide` 后台、janitor/daily 是系统派发，无通道（只等最终结果）。flow 节点交付结果与 one 路由后继走 `deliver(message=…, next=…)`（节点专用，调用即交付并结束回合，与主会话沟通无关）。
- 工作流草案/引擎操作经激活 `workflow` 技能后的 `orchestrator` 工具完成；aide/janitor **不再**挂独立 `workflow` 工具。
- `aide` 在 delegate 调用时被**强制** `background=True`（invocation 构造时改写参数）。
- `aide` 创建时深拷贝父级 `message_history` 的最近 50 条（尾部窗口，丢弃孤儿 tool_result）作为自己的初始上下文。
- `aide` 收官结果回投主会话（`background_task_complete` 事件路由）：主会话忙时走 `submit_continuation_input`（当前回合下一迭代看到）；空闲时进驻会话历史（append + `persist_session_to_disk`），**不唤醒回合**——主会话提示词已写明「知道即可 无需回应无需复述 后续自然运用」。bash 等其他后台任务空闲时仍唤醒新回合，语义不变。
- 子智能体不能再委派：`delegate_depth >= 1` 时 delegate 执行直接返回错误。
- `explore` / `research` 在 `REMOVED_SUBAGENT_TYPES` 中：只读并行改用 `coaras`；调研改用 `skill(action="activate", name="research")`。
- plan_mode 下 delegate 的子代理强制只读：子代理是白名单继承但 `is_plan_mode=False`，故 `_resolve_tool_whitelist` 在父级 plan_mode 时交集安全集并剥离 `write`/`edit`/`plan_mode`（详见 `docs/子智能体重构.md`）。
- 并发数量不设上限（前台 + 后台）；单个子智能体工具迭代硬上限 `SUBAGENT_MAX_TOOL_ITERATIONS = 1500`（`delegate.py`）。

### 3.4 工作空间会话实例（WorkspaceSession）

每个已切入的工作空间（含启动空间）都有一个对等的 `WorkspaceSession`（`src/coara/workspace_session.py`），内部是独立 `CoaraBase`（共享 Root 的 persona / provider，`is_owner_context=True`）。`RootCoara` 只做进程宿主。详见 §十二。

---

## 四、启动与关闭

### 4.1 CLI 入口与内核分派

入口是 `src/cli/main.py` 的 click group `coara`（version 0.1.0）。**终端只是端，不是内核**：裸 `coara` 检测/拉起常驻内核（`tray`/`daemon` 子命令，`_spawn_detached_daemon` 以 detached 进程拉起，终端关闭不杀）后经 `/ws/attach` attach；Web/Matrix 由内核托管，入口收拢到系统托盘（右键开 Web / 手机二维码 / 退出）。

| 命令/标志 | 含义 |
|------|------|
| 无参数 | 拉起/复用常驻内核并 attach（推荐入口） |
| `coara attach` | 显式 attach 到运行中的内核 |
| `coara tray` | 无头内核 + 系统托盘常驻图标（右键开 Web / 手机 / 退出） |
| `coara daemon` | 纯无头内核（无托盘） |
| `-p / --provider`、`-m / --model` | 指定 LLM provider / 模型 |
| `--workspace`、`--workspace-alias` | 指定工作空间目录 / 别名 |
| `-v / --verbose` | DEBUG 日志到控制台 |

启动链路（`src/cli/main.py`）：

```text
coara 裸启动链
├── config_manager.load()
├── _ensure_kernel_and_attach()       # 当前 cwd 登记为工作空间
├── _spawn_detached_daemon()          # 拉起内核（无头 daemon/tray），stdout/stderr 重定向 daemon.log
├── _wait_daemon_ready()          # active.json 出现活 pid 且 web 端口可连
├── attach（attach_client → /ws/attach）进入对话
└── 内核侧：TraceStore 订阅 EventBus（并订阅 workspace_switched 以便换工作空间时换 store）
```

离线子命令（`src/cli/main.py`，均不进入聊天循环）：

| 命令 | 用途 |
|------|------|
| `coara status` / `coara providers` / `coara llm-profiles` | 状态与 provider 查看 |
| `coara search <query>` | 搜索宝箱 `open/` 文件名（`--no-prompt` + `COARA_VAULT_PASSWORD` 用于 CI） |
| `coara ws list/add/remove/rename/default` | 工作空间登记（`default` 只写启动默认；会话切换用 `/ws switch`） |
| `coara usage summary/session` | 用量统计（summary 默认 7 天窗口） |
| `coara vault status/init/unlock/lock/list/read/write/import/passwd` | 宝箱操作 |
| `coara workflow list` | 工作流草案列表 |
| `coara examples install <name>` | 安装示例工作空间（如 `stocks-watch`） |

其他入口：`python -m src.coara`（委托 `src.cli.main:main()`）、`python -m src.matrix_client`（直接跑 Matrix 机器人）。

### 4.2 Root 初始化顺序

`RootCoara.initialize()` 的实际工作大部分在 `initialize_root_services()`（`src/coara/root_lifecycle.py`）。按代码顺序：

```text
RootCoara.initialize() 启动链
├── 配置与工作空间
│   ├── get_config(force_reload=True)
│   ├── 创建 WorkspaceManager；workspace_dir 定为启动 cwd
│   └── workflow_bridge.bind_runtime_paths()
├── 工具与服务注册
│   ├── 线程中 prune_stale_tool_outputs()
│   ├── vault_enabled → VaultService（工具挂在各 WorkspaceSession）
│   ├── RecordsStore + LocalSearchTool（records 目录，延迟）
│   ├── bootstrap_tools() / ReminderService
│   ├── register_root_scoped_tools(root)：plan_mode / workflow / skill / ws / event_source
│   ├── load_skills()
│   └── CoaraBase.initialize()：运行时工具（含 media）
├── 绑定前台 WorkspaceSession（_bind_foreground_workspace_session）
│   ├── ensure_workspace_session(启动空间)
│   ├── 设 _foreground_session_id
│   └── publish_active_runtime() → <coara_home>/runtime/active.json
├── 观测与通知接线
│   ├── set_trace_sink(event_bus.publish)
│   ├── attach_usage_collector()
│   └── 后台任务完成订阅；孤儿 running_* SubagentStore / TaskStore 恢复（遍历注册表全部活跃空间）
├── scheduler 消费循环 + idle watcher
├── EventSourceManager + workflow trigger 注册表
└── 就绪日志
```

### 4.3 关闭路径（快而不丢）

```text
shutdown_root_services() 关闭链
├── 第一段：打断与持久化
│   ├── 回合进行中 → interrupt_current_turn("shutdown")，等 _process_lock 释放（≤2s）
│   └── 持久化 `_sessions` 中全部 WorkspaceSession（阻塞 IO 放 to_thread）
├── 第二段：全并行关闭（单个 asyncio.gather，墙钟取 max 而非 sum）
│   ├── scheduler（≤2s）/ 后台任务（≤2s）
│   ├── vault / reminders / event_sources
│   ├── workflow_bridge（≤5s）
│   └── llm_service.close()
├── 第三段：收尾
│   ├── 退订 EventBus 订阅 → CoaraBase.shutdown()
│   ├── event_bus.shutdown() / 用量统计收尾
│   └── clear_active_runtime()
└── 配套措施
    ├── WorkflowEngineController.shutdown()：join(3s) → terminate → join(2s) → 强杀；
    │   阻塞 join 放 asyncio.to_thread，整体包 wait_for 超时
    ├── CLI+Web 共享 root 时 WebServer._owns_root=False，stop() 不重复关 root
    ├── CLI 退出：Matrix dispatcher 排空 3s、任务取消 gather 5s（chat_runner.py）
    ├── SIGTERM / Windows 关窗收尾（`src/cli/shutdown_signals.py`）：
    │   SIGTERM handler 只打断当前回合并向输入队列投退出哨兵，主循环照旧走正常 finally；
    │   关窗 / Ctrl+Break（CTRL_CLOSE_EVENT / CTRL_BREAK_EVENT）经 SetConsoleCtrlHandler
    │   在 OS 注入线程做最小同步收尾（session persist + usage flush + trace close，
    │   约 5s 预算，任何异常不阻塞退出），Ctrl+C 行为不变
    └── /restart 不丢 trace：os._exit 前显式关闭 root.trace_persistence
        （process_restart._flush_trace_persistence，复用 chat_runner 的 close 路径）
```

---

## 五、回合循环：process_message → run_turn_loop

### 5.1 双层结构

```text
外层：用户回合（CLI / Web / Matrix 各前端驱动）
└── 内层：一次 process_message() 里的 LLM-工具循环
```

`process_message()`（`src/coara/base.py`）关键参数：`trust_level`（`"owner"` / `"untrusted"`，沙箱只对 untrusted 生效）、`source`（发起前端：`cli`/`web`/`matrix`）、`image_blocks`、`turn_id`。

进入 `_process_lock` 后：

1. 置 `_inside_turn`、创建 `TurnRuntime`（携带 `AbortController`）
2. 记录快照点 `turn_history_start = len(self.message_history)`（打断收口与异常回滚用，见 §六）
3. 依次发 trace：`user_message` → `turn_start`（保证各前端按时间序显示）
4. 委托 `run_turn_loop()` 跑内层循环，产出的文本块一方面 yield 给调用方，一方面作为 `chat_chunk` trace 发给其他前端

### 5.2 内层每轮做什么

`run_turn_loop()`（`src/coara/turn_orchestrator.py`）每轮（iteration）：

```text
run_turn_loop 的一轮
├── ① 拾取回合外输入
│   └── drain_continuation_inputs() → USER 消息入史
│       （前台 delegate 结果、后台完成提醒都经此进对话）
├── ② 上下文准备（turn_loop/context_prep.py）
│   ├── 构建 system prompt + 消息窗口
│   ├── 超阈值 → maybe_compress_messages() 压缩历史
│   └── evaluate_guard() 仍超限 → context_blocked 终止本轮
├── ③ LLM 调用（turn_completion.py）
│   ├── provider 支持工具 → 原生 function calling（失败直接上抛，不静默降级文本协议）
│   ├── 流式中途 LLMError → 仅当零 delta（未收到任何内容分片）才回退
│   │   非流式整 prompt 重发；已出部分内容则直接报错不重发（避免重复计费），
│   │   provider 已返回的部分 usage 随异常带出，以 llm_turn_partial 入账
│   ├── 不支持 tools → 不带 tools 的纯文本完成
│   └── LLMError → llm_error trace、置 FAILED、yield 错误返回（不回滚历史），
│       并向历史追加一条失败注记（<系统消息>，含错误类型），
│       让下一轮模型知道上一轮失败过、用户消息尚未被处理
├── ④ 追加 assistant 消息（content / tool_calls / reasoning / wire blocks）
├── ⑤ 输出截断恢复（agent/output_truncation.py）
│   └── 命中 max_tokens 截断 → 按策略续写/告警，直接下一轮
└── ⑥ 分支判定（每个 continue 分支先 flush 本轮未发文本，见 §5.2.1）
    ├── 无 tool_calls（按序做退出前检查）
    │   ├── todo 未完结 → flush → turn_continue 继续
    │   ├── 有未保存工作流草案 → flush → 注入提醒继续
    │   ├── 前台 delegate 在跑 / 有 continuation_input
    │   │   └── flush → asyncio.wait(FIRST_COMPLETED) 等首个完成（可被打断）；
    │   │       结果已经 done callback 入队，下一轮 ① 拾取
    │   └── 以上皆否 → yield 剩余文本，产出最终回复，退出循环
    └── 有 tool_calls
        ├── 先 yield 本轮文本（Channel A 预览），再 ToolExecutor.execute() 执行
        ├── apply_tool_results_to_history() 结果写回历史
        └── 停滞守卫记录进展签名 → 下一轮
```

特殊批次处理：一批 tool_calls **全部**是后台 delegate 时，不结束回合，而是注入一条「以下后台子代理任务已启动」的 USER 角色系统提醒并 `turn_continue`，让父级继续推进，避免重复委托。

### 5.2.1 输出流式不变量：yield 是远程通道的唯一生命线

每个迭代产出的 assistant 文本有两条互不知情的消费通道：

| 通道 | 消费方 | 特点 |
|------|--------|------|
| `process_message` yield 的 chunk | Matrix（`stream_coara_reply_to_matrix` 逐条发房间）、Web UI、leftover 处理 | 远程用户**唯一**的消息来源 |
| trace 事件（`llm_turn_complete` 含全文、`chat_chunk` 等） | CLI activity 实时显示、TraceStore/Web dashboard | 发后即忘，仅供观测 |

关键推论：**CLI 少了 yield 仍能靠 trace 显示，远程通道少了 yield 就彻底丢消息**。任何「CLI 有输出、手机没有」的现象，第一嫌疑永远是没有 yield 的分支，不是 Matrix 通信。

文本记账（`base.py`）：

- `_streamed_assistant_chars`：本轮响应已被 CLI 流式钩子实时消费的长度（无钩子恒为 0）；每次新 LLM 响应后重置
- `_unsent_assistant_text(full)`：切出全长文本中尚未流出的部分

**不变量：每轮迭代的可见文本必须恰好 yield 一次。** yield 点共五处：

1. 工具执行前（Channel A 预览，`turn_orchestrator.py` 工具分支）
2. todo 未完结强制 `turn_continue` 前
3. 工作流草案待保存注入提醒前
4. 前台 delegate 等待 / continuation_input 续跑前
5. 轮次退出时（`_unsent_assistant_text` 剩余部分）

历史教训（2026-08，trace 实证）：当时 2–4 直接 `continue` 不 yield。前台子智能体场景中，模型在等待前写好的结论文本只入史不流出——`llm_turn_complete` 有全文、全程无 `chat_chunk`——CLI 正常显示而 Matrix 用户永远收不到，且现象飘忽（只在有子智能体/强制继续时出现），被反复误诊为通信问题。

### 5.3 守卫与硬上限

| 守卫 | 位置 | 行为 |
|------|------|------|
| `LoopDetector` | `src/agent/loop.py` | 重复工具调用检测（历史容量 `MAX_HISTORY_LENGTH = 100`） |
| `TurnStagnationGuard` | `src/agent/loop.py` | 连续无进展 / 重复错误签名 → 提前停止本轮 |
| `ShellFailureStreakGuard` + hooks | `src/agent/loop.py`（守卫类）+ `src/agent/hooks.py`（Before/Post hook） | shell 连败前/后 hook |
| `AccessPolicyHook` | `src/agent/hooks.py` | before hook，调用层访问策略 |
| 上下文守卫 | `src/context/window.py` | 警告 / 阻断（`context_blocked`） |
| `max_tool_iterations` | 构造参数 | 到达上限发 `iteration_limit` trace、置 FAILED；Root 默认 1200，子智能体固定 1500 |

### 5.4 @ 服务台（CLI / attach 专属）

外挂 CLI 入站整段 ``@台名 …`` 由 `src/coara/service_desk.py` 解析，投递到 daily / 配置等服务台会话；**不换本端 view**。Web 聊天框与手机把 `@…` 当普通用户文本交给 LLM。旧的 `@文件路径` 注入已退役。

---

## 六、打断、取消与 message_history 回滚

### 6.1 取消原语

`src/core/abort.py` 仿 Web API 实现 `AbortController` / `AbortSignal`：

- signal 支持多监听器（子智能体、工具、HTTP 请求都可挂）、记录 `reason`、支持 await 与回调两种消费方式
- `abort(reason)` 幂等；已 abort 时新监听器立即触发
- 每个回合一个 `TurnRuntime(controller=AbortController())`；LLM 等待（如 `maybe_compress`、前台 delegate 等待）经 `_await_interruptible()` 与 signal 竞速
- delegate 启动子智能体时在父 signal 上挂监听器，父级取消会调用子智能体的 `interrupt_current_turn("parent_cancelled")`

### 6.2 回滚规则（逐条对照 turn_orchestrator.py 异常分支）

快照点：`turn_history_start = len(message_history)` 在拿到 `_process_lock` 后、本轮用户消息入史**之前**记录。异常路径的整轮回滚即 `del message_history[turn_history_start:]`（`base.py::_rollback_partial_turn_history`）。

| 路径 | 是否回滚整轮 | 用户看到 |
|------|--------------|----------|
| `CoaraRunCancelledError`（Ctrl+C、`/stop`、普通 `interrupt_current_turn`） | ✗ 不回滚；执行器经 `interrupt_sink` 把批次中已有结果（已完成工具的真实结果、被取消工具的已取消结果）先如实入史——尊重 AbortSignal 的工具（`wait_for_abortable`）执行中被中断时同样记录一条「执行中被用户打断」的真实结果（`preserve_cancel_content` 标记使文案不被统一改写）；`finalize_interrupted_turn_history` 只为**未执行**的 tool_calls 补「[未执行] 回合被中断」合成结果；再以 `<系统消息>当前会话已打断。</系统消息>` 注入 USER 历史（若硬停了子智能体，正文附可 `delegate(action="resume")` 的 task_id 列表） | UI：`[系统] 当前会话已打断。` |
| `CoaraRunCancelledError(reason="new_session")`（运行中 `/new`） | ✗ 不回滚（/new 另行重置整个会话历史）；turn 内 `/new` 发 abort 后**延迟到旧回合释放 `_process_lock` 再清状态**（`_finish_new_session_after_turn_exit`，与调用方同栈不可原地 await），旧回合退出前的追加（工具结果、闭合注记）不会落进新会话空 history；延迟清理任务存活期间 `process_message` 拒绝新回合（引导稍候，防清理抹掉新回合历史），等锁超时（旧回合无视 abort 存活）则放弃清理并告警、保留旧会话现场 | 无「已打断」输出 |
| `CoaraRunCancelledError(reason="switch_workspace:<名>")`（回合中 `ws(switch)`） | ✗ 不做整轮 `turn_history_start` 回滚；`strip_ws_switch_tail` 删除「触发切换的用户输入 → ws(switch)」整段尾部 | `[系统] 已切换到工作空间 <名>` |
| Provider `LLMError` | ✗ 不回滚（已入史的 assistant/tool 保留），追加失败注记（见 §5.2 ③） | `Error: …` |
| 其他未捕获异常 | ✓ 回滚后**继续抛出**；回滚时若有已执行工具，注入一条磁盘副作用注记（本回合已执行的 write/edit/delete 及目标文件清单，见本节末段） | 由上层处理 |
| 工具审批 ESC / 取消 | 走 `user_cancelled` 分支，不是整轮回滚 | 工具结果标记取消 |

用户打断只结束回合并收口协议，**不**撤销：磁盘副作用（write/shell 已落盘的改动）、已写入的 trace 事件。未完成的前台/后台子智能体在打断时**强制取消**（`hard_cancel_all_running_delegates`；bash 后台任务经 `background(action="stop")` 收口）。未捕获异常路径的回滚同样只删 `message_history`；若回滚前本回合已有工具成功执行，回滚后追加一条 `<系统消息>` 副作用注记（`turn_orchestrator._build_rollback_side_effects_note`：列出已执行的 write/edit/delete 及目标文件、其余工具计数），让下一轮模型知道磁盘已被改动。

回合 `finally` 块固定做的事：取消所有未完成的前台 delegate、清 `_active_turn`、把 RUNNING 状态拨回 IDLE、持久化会话（事件日志 `sync_history` 前缀对账 + `session_state.json` 索引）、最后清除 turn in-flight 标记（先落盘再清标记，保证「标记在 ⇒ 本轮历史未落盘」的恢复推断成立）。

**turn in-flight 标记（进程中断恢复）**：`run_turn_loop` 开始时向 `<coara_home>/workspaces/<workspace_id>/turn_in_flight.json` 原子写入标记（tmp+replace，每回合一写一清，仅 user_facing/owner 会话）；进程被杀（无 finally 机会）时标记残留，`recover_session` 恢复时发现标记即向历史注入一条「上次回合未完成（进程中断）」的 `<系统消息>` 注记并清除标记（只注入一次；session_id 不符的陈旧标记自愈清除、不注入）。该轮未落盘的用户消息与模型回复因此不会幽灵重现，模型也能知道上一轮未正常结束

### 6.3 后台 delegate 的历史抹除

`apply_tool_results_to_history()`（`src/coara/turn_loop/tool_results.py`）对两类结果不落 `TOOL_RESULT`：

- **成功的后台 delegate**（`metadata.mode == "background"`）：从 assistant 消息的 `tool_calls` 里剥掉对应条目（没有剩余则置空），不写 TOOL_RESULT——结果完成时会另经通知链路回来
- **控制面信号**（`metadata.control_only`，如空闲 `/ws switch`）：剥离对应 tool_call 且不写结果，避免当前 session 留下悬空 tool_result；mid-turn `ws(switch)` 走 `CoaraRunCancelledError`

---

## 七、工具系统

### 7.1 注册分层（按代码核实）

```text
工具注册分层（按注册时机）
├── 1. 进程级无状态（register_builtin_tools()，src/tools/__init__.py，每进程一次）
│   └── web_search / web_fetch / task(list/stop, bash only)
│       （即 builtin/manifest.py 的 PROCESS_STATELESS_TOOL_TYPES）
├── 2. 节点 bootstrap（CoaraBase.bootstrap_tools()）
│   ├── 挂载进程级工具
│   └── skill
├── 3. 运行时绑定（register_runtime_tools()，CoaraBase.initialize() 调用，所有节点）
│   ├── 文件：read / write / edit / delete
│   ├── 发现：glob / grep
│   ├── 系统：shell / web_search(replace) / todo
│   ├── 媒体：media
│   └── delegate（仅 delegate_depth == 0）
├── 4. Root 专属（initialize_root_services() → register_root_scoped_tools()，Root 与每个 WorkspaceSession 同款注册）
│   ├── vault（vault_enabled 时）/ local_search（records store 就绪时，延迟）
│   ├── plan_mode / workflow / skill(replace) / ws / event_source
│   └── reminder（服务在 Root，工具挂到每个 session）
├── 5. 子智能体补充（delegate.py 的 _bootstrap_subagent）
│   ├── workflow（仅 aide / janitor，parent_coara 绑父 Root）
│   └── record / local_search（仅 janitor / daily，绑 records store）
└── 6. tool 常驻网关（负责揭示 deferred 挂起工具）
```

`src/tools/builtin/manifest.py` 是这套分层的汇总清单。

### 7.2 内置工具目录分类

| 类别 | 目录 | 工具 |
|------|------|------|
| 文件 I/O | `file_io/` | `read` `write`（含 `.docx`/`.xlsx`/`.pdf`）`edit` `delete` `glob` `grep` |
| Web | `web/` | `web_search` `web_fetch` |
| 后台 | `background/` | `background`（list/stop，bash 后台任务） |
| 委派 | `delegate/` | `delegate` |
| 编排 | `flow/` + `orchestrator/` | `orchestrator`（flow CRUD + WDL 草案，挂起工具 `tool(activate)` 揭示） |
| 代码 | `code_mode/` | 代码模式工具 |
| 通信 | `communication/` | 通信类工具 |
| 运行时 | `runtime/` | `shell` |
| 规划 | `planning/` | `plan_mode`（enter/submit/exit） |
| 集成 | `integration/` | `tool` `send_file` |
| 媒体 | `media/` | `media`（image/video generate、status、cancel） |
| 调度 | `scheduling/` | `reminder` `event_source` |
| 待办 | `todo/` | `todo`（read/write） |
| 技能 | `skills/` | `skill`（search/activate） |
| 本地记录 | `records/` | `record`（janitor/daily 专用）`local_search`（延迟） |
| 批复 | `review/` | 工作空间动态处置 |
| 宝箱 | `vault/` | `vault`（open/close/status；unlock/lock 为别名） |
| 工作空间 | `ws/` | `ws`（登记 list/add/remove/rename/switch、updates_*） |
| 挂起工具 | `email/` `embody/` `screenshot/` `printer/` | `email`（收/发）`embody`（键鼠/放声）`screenshot`（截图/录制）`printer`（本机打印）——`tool(action=activate)` 装载后可用 |

### 7.3 可见性治理

`ToolManager.get_visible_tool_definitions()`（`src/coara/tool_manager.py`）按四道过滤：

1. **白名单**：设置了 `_tool_whitelist` 的节点只放行名单内工具
2. **owner_only**：`is_owner_context=False` 时隐藏声明了 `owner_only` 的工具
3. **plan-mode**：激活时只暴露 `_PLAN_MODE_ALLOWED` 共 10 个：`read` `glob` `grep` `web_search` `web_fetch` `delegate` `todo` `write` `edit` `plan_mode`
4. **deferred（延迟加载）**：`should_defer=True` 的工具（内置的 local_search/vault/reminder/workflow/event_source，及截图/打印/邮件等挂起工具）不进 LLM 工具清单，经 `tool(action="activate")` 揭示（reveal）后才出现；`tool` 本身不延迟

另外，主会话发给 LLM 的高风险工具定义（write/edit/delete/shell）会注入
`require_approval` / `approval_reason` 两个元参数（LLM 可主动请求人工确认；执行器在调用前剥掉它们）。
**子智能体不注入这些参数，其工具调用也不走审批门**（审批发生在用户是否发起委派）。

### 7.4 审批链路

声明式三层取 OR，任一命中即弹人工确认：

```text
一次调用是否需要人工确认
├── ① 工具类声明 requires_approval
│   └── bool 或 @staticmethod(args) -> bool（按参数判定）
├── ② LLM 在调用里传 require_approval: true
│   └── （元参数随主会话工具定义注入，执行器调用前剥掉）
├── ③ 配置 security.call_policy.prompt 强制列表
├── ④ security.call_policy.auto_allow 可绕过 ①②（prompt 列表优先）
└── 任一命中 → ToolExecutionPolicy.resolve() 弹确认（默认超时 5 分钟）
```

**例外（整段跳过审批门）**：`delegate_depth ≥ 1` 的子智能体，以及 `janitor` / `daily`（系统维护）。见 [RELEASE_WORKFLOW.md](../deploy/gitee/RELEASE_WORKFLOW.md) §8。

工具分离校验与执行：`create_invocation(params)` → `invocation.execute(signal)` → `ToolResult`。

### 7.5 执行管线

`ToolExecutor`（`src/agent/executor.py`）：

- 默认外层超时 `default_timeout = 60.0`；工具可用 `get_execution_timeout()` 覆盖——`delegate` / `orchestrator` 都返回 `None`（关掉通用超时，避免被 60s 截断）；`shell` 按 `timeout_ms` 声明等待上限，**超时即失败**：进程树被终止并返回明确超时错误（无并行、无分离）
- 同一批 tool_calls 按 `tool.get_write_lock(args)` 的资源键分组：**同键串行、跨键 `asyncio.gather` 全并发**
- before hooks：`AccessPolicyHook` → `LoopDetectionHook` → `ShellFailureStreakBeforeHook`；post hook：`ShellFailureStreakPostHook`（仅 `shell`）
- 批后对超预算的结果做批量 spill（`_apply_batch_spill_budget`，见 §八）

### 7.6 文件工具安全模型

- 所有文件工具要求**绝对路径**且必须在挂载工作空间内；`resolve_workspace_path()` 拒绝相对路径、UNC、Windows 扩展路径、备用数据流、工作区外路径；`glob`/`grep` 省略 `path` 时默认工作空间根
- `edit` / `write` / `delete` 执行时实时读盘，**不要求**事先 `read`；`_file_read_states` 是进程内快照缓存（内容/mtime/编码），不是 LLM 上下文
- `read` 返回原文或 Vision 图片块，`offset`/`limit` 按行分段；大输出走 spill 后的 `ref` 续读
- `delete` 仅删单个文件、不可逆；删目录用 `shell`
- `write` 按后缀分流：文本/代码原样写；`.docx`/`.xlsx`/`.pdf` 走 Office 构建（富 HTML → pandoc，纯 Markdown Word → python-docx）
- 工具 `description` 与 schema 按约定**句末不加句号**（见 `docs/CONVENTIONS.md`）

---

## 八、工具输出：三通道 + spill

一次工具执行的输出先分三个通道，Model 通道再按 spill 策略决定是否落盘：

```text
一次工具执行的输出分流
├── Terminal 通道（ToolResult.display）
│   ├── display blocks 渲染到 CLI scrollback（diff_render.py 用 Rich 绘制）
│   └── 绝不进 message_history
├── Gate 通道（审批弹窗里的执行前预览）
│   └── 按需计算（gate_preview.py，如 edit 的紧凑 diff），不持久化
└── Model 通道（ToolResult.content → message_history）
    └── spill 判定（spill_policy.py）
        ├── 自管理工具（read/write/edit/delete/web_fetch/web_search）
        │   └── 跳过按工具 spill，仅受批预算 batch_budget_bytes 200K 兜底
        ├── 具名默认 → grep/glob 20K(头+尾)、shell 30K(尾)、delegate 32K(尾)
        └── 其余工具 → 默认阈值 25K
            超阈值：全文落盘，模型只见头/尾预览（read(ref=…) 续读全文）
```

### 8.1 三个输出通道

每次工具执行最多产生三个通道的输出（框架详见 [`docs/TOOL_OUTPUT_FRAMEWORK.md`](TOOL_OUTPUT_FRAMEWORK.md)）：

| 通道 | 载体 | 去向 |
|------|------|------|
| Model | `ToolResult.content` | 进 `message_history`，给 LLM 看 |
| Terminal | `ToolResult.display` 的 display blocks | 渲染到 CLI scrollback（`src/coara/diff_render.py` 用 Rich 绘制），**绝不**进历史 |
| Gate | 审批弹窗里的执行前预览（如 `edit` 的紧凑 diff） | 按需计算（`tool_output/gate_preview.py`），不持久化 |

`src/coara/tool_output/pipeline.py` 是路由入口；`diff.py` / `syntax.py` / `read_format.py` 负责构建显示块。

### 8.2 大输出 spill（分层预算）

配置模型 `ToolOutputStoreConfig`（`src/core/types.py`，`config.yaml runtime_enhancements.tool_output_store`）默认值：

| 键 | 默认 | 含义 |
|----|------|------|
| `enabled` | `true` | 总开关 |
| `spill_threshold_bytes` | 25 000 | 无具名覆盖时的默认阈值 |
| `batch_budget_bytes` | 200 000 | 一批 tool_calls 合计模型可见输出上限；0 = 禁用 |
| `preview_head_chars` / `preview_tail_chars` | 2 000 / 8 000 | 模型可见预览的头/尾长度 |
| `tool_thresholds` | `{}` | 按工具名覆盖阈值 |
| `retention_days` | 30 | spill 文件保留天数（Root 启动时清理过期文件） |

`src/runtime/spill_policy.py` 的判定规则：

- **自管理工具**（`read` `write` `edit` `delete` `web_fetch` `web_search`）跳过按工具 spill（它们自己在 `execute()` 里限输出），只剩批次预算兜底
- 具名默认：`grep`/`glob` 20K（头+尾）、`shell` 30K（留尾）、`delegate` 32K（留尾）
- 头+尾模式的预览带显式标记 `[PREVIEW TRUNCATED — use read(ref=…) for full body]`，完整内容落在 coara home 下的工具输出存储（`src/runtime/tool_output_store.py`），模型经 `read(ref=…)` 续读

---

## 九、四个执行层：todo / delegate / flow / workflow

Root 在一个回合里可以走四条路径推进任务，边界如下：

```text
todo / delegate / flow / workflow 的执行边界
├── Root 当前回合
│   ├── 简单任务 → 直接做
│   ├── 多步任务 → todo 自我推进
│   ├── 需独立上下文 → delegate 子智能体
│   ├── 需 agentic 流程 → orchestrator(spawn, flow=…) 编排内存态 flow 图
│   └── 需正式流程 → 用 orchestrator 织图并提交内核投影（save/run）给引擎
├── todo
│   ├── 按 session 持久化，todo(action=read|update) 维护（整表覆盖）
│   ├── 未完结 → TurnController 不允许回合退出
│   └── 有待办 → 消息层注入提醒（不进 system prompt）
├── delegate
│   ├── 独立上下文 / 独立 todo；工具白名单按子智能体 YAML
│   ├── 前台：create_task 异步启动；结果由 LLM 调 delegate(action="wait") 显式收齐（出口软提醒一次后可放行，迟到结果忙则注入、闲则驻历史）
│   ├── 后台：BackgroundAgentManager 执行，完成经 EventBus 注入后续回合
│   ├── 并发数量不设上限（前台+后台）；单个子智能体 ≤ 1500 轮工具迭代
│   ├── delegate_depth ≥ 1 不能再委派
│   └── 返回父级的是最终交付结果，不是过程日志
├── flow（agentic 工作流）
│   ├── tool(activate) 揭示 orchestrator 工具
│   ├── 节点是 coaras 子智能体，FlowCoordinator 薄协调
│   ├── spawn 默认停车，run 点火后 fan-in 依赖级联推进
│   ├── routes_mode=one 时 report(next=…) 动态选后继
│   ├── save(flow=…) 从图自动投影为 WDL 并落盘（无需单独 export）
│   └── schedule 仅记录（内部不生效），投影到 WDL 后由外部引擎承载
└── workflow（系统级子系统，存储/配置锚定 config home，与工作空间无关）
    ├── 内核投影（nodes + edges）经 orchestrator 存取/提交
    ├── 草案是唯一活模型：编排写穿 <系统home>/users/default/workflows/drafts/，编辑器实时同步
    └── 执行在 WorkflowEngineRuntime 子进程（实例库/日志在系统目录），结果经 bridge 回注对话
```

### 9.1 todo（自我推进清单）

- 统一工具 `todo(action=read|update)`（`src/tools/builtin/todo/todo.py`），按 session 持久化；`update` 整表覆盖
- 回合退出判定时 `TurnController` 读 todo 状态：还有 pending/in_progress 就不允许结束本轮
- 有待办时向消息层注入 todo 提醒（不进 system prompt）

### 9.2 delegate（委派子智能体）

参数：`action`（`spawn` 委派新任务 / `message` 向运行中子智能体发途中消息）、`description`（一句话任务名）、`prompt`（spawn 为完整任务指令、message 为消息正文）、`subagent_type`、`background`、`workspace`（可选，锁定登记的工作空间）、`task_id`（message 必填）。

**前台模式**（`background=false`，默认）：

```text
前台 delegate 的生命周期
├── asyncio.create_task 启动子智能体，立即返回占位 ToolResult（"前台子智能体已启动"）
├── 父级不停下，继续自己的工具循环
├── （任务型 coaras）子智能体经 interact 工具把途中消息以 <子智能体消息> 标签注入父会话；
│  父级可 delegate(action="message", task_id) 途中干预，delegate(action="stop") 硬停
├── LLM 无 tool_calls 时，退出守卫 asyncio.wait 等首个前台 delegate 完成
└── done callback 已把结果 submit_continuation_input 入队；
    下一轮 drain 进历史，LLM 带着结果继续推理
```

**后台模式**（`background=True`）：

```text
后台 delegate 的生命周期
├── BackgroundAgentManager.start() 起独立 asyncio.Task，立即返回 task_id
├── 本轮可正常结束
└── 完成后经 EventBus 通知链路注入后续回合（见 §十）
```

共同规则：

- 并发数量不设上限（前台+后台），子智能体 1500 轮工具迭代上限（§3.3）
- `workspace` 参数把子智能体锁到登记工作空间：解析后给实例挂专属 `VfsResolver`
- `aide` 强制后台；完成通知经 EventBus 回投主会话（忙时接续输入、空闲进驻历史不唤醒，见 §3.3）
- 任务型 `coaras`（仅前台）有双向通道（`interact` 工具中间沟通 + 主→子 `delegate(action="message")`）；`aide` 后台、janitor/daily 系统派发无通道，只等最终结果
- 子智能体状态持久化：`SubagentStore`（`<coara_home>/workspaces/<workspace_id>/subagents/{agent_id}.json`）记录 running→终态与结果预览；运行中每个工具批次结束还会增量重写断点（`SubagentCheckpointer`，5s 节流），进程被杀也保住中间进展；启动对账（`recover_stale_running`）把全部已登记空间的 running_* 记录收敛为 failed，使 `delegate(action="resume")` 可受理；后台任务同时写 `TaskStore` ；agent 硬停用 `delegate(action=stop)`，视频取消用 `media(action=cancel)`（LLM 侧已无 task 工具）
- 子智能体看不到父级对话，`prompt` 必须自洽（工具描述里内置「如何写 prompt」六要素：目标/范围/方法/规则/验收/回报格式）

### 9.3 flow（agentic 工作流编排）

- 激活 `workflow` 技能后注册 `orchestrator` 工具，flow 图级参数 `flow`/`node_id`/`prompt`/`depends_on`/`routes_to`/`routes_mode`/`input`/`auto_run`/`schedule` 与 action（spawn/run/wait/status/save/load/update/edge/remove/…）
- `FlowCoordinator`（`src/coara/flow_coordinator.py`）进程级单例，持有内存态 flow 图：
  - **spawn**：创建 `coaras` 子智能体（停车，不跑 `process_message`），登记节点与依赖/路由
  - **run**：启动所有就绪节点（pending 且依赖满足）；完成后按 `routes_to` 路由结果给下游，触发 `_maybe_start` 级联推进
  - **wait**：等整个 flow 收尾（所有节点 done/failed）
  - **status**：各节点状态与结果摘要
  - **save(flow=…)**：从会话内图自动投影为内核 WDL 并校验落盘（也可 `definition=` 直接存文本）；无单独 export action
  - **load**：按 flow 名或 draft_id 从草案载回内存图
  - **update / edge / remove**：运行中调整节点任务/路由、加删边、删未运行节点
- `routes_mode=all`（默认）：完成后结果广播给全部 `routes_to` 下游
- `routes_mode=one`：节点运行期用 `deliver(message=…, next=node_id)` 动态选一个后继（调用即交付并结束回合）
- `schedule` 仅记录意图（内存态无定时器），`save(flow=…)` 投影时写入 WDL 顶层 `schedule`
- **护栏**：`max_hops`（默认 100）防死循环；死锁/无效依赖检测标 failed 收尾；`_validate_identifier` 校验 flow/node_id 防路径逃逸
- **WebUI 实时视图**：`/flow-live/:flowName` 路由，React Flow 只读画布，全量快照（`flow_snapshot` 请求）+ 增量 trace 事件（`flow_graph_changed` / `subagent_start` / `subagent_complete` / `subagent_failed`）更新
 - 蓝图与设计意图（流程原语、内外工作流一致）已归档 `ref-doc/archive/内部编排与自演示规划.md`；当前 flow 模型以此处为准

### 9.4 workflow（正式工作流）

- 工作流是系统级子系统：存储锚定 config home（`users/default/workflows/`），与工作空间/启动目录无关；启动时幂等迁移旧的按工作空间散落数据（`src/workflow/migration.py`）
- 草案是唯一活模型：编排动作（spawn/update/edge/remove）自动写穿绑定草案 + WS 推送，WebUI 编辑器实时显示；`save` 为幂等确认（复用绑定 draft_id 覆盖）
- UI 编辑反向感知：编辑器保存后绑定 flow 标 stale，会话下次 run/wait 前从草案重建；`load` 载回即重建绑定
- 会话全生命周期：`orchestrator(action="spawn|run|wait|status|save|load|result|update|edge|remove|rerun|resume|cancel|delete", …)`；`save`/`run` 先 `validate_projection` 再 `normalize_projection`
- 节点 LLM 独立：`llm_profiles.workflow.node`（WebUI 工作流页可改），主会话 `/model` 切换不影响；会话内编排节点与引擎节点同走 `resolve_workflow_node_llm`
- `run`（draft_id/definition）把内核投影交给 WorkflowEngine 子进程异步执行；实例库/日志均在系统目录
- WDL 写法由 `orchestrator` 工具描述独占（挂起工具，`tool(action="activate")` 揭示后即见完整指南）

---

## 十、消息通道：EventBus vs UnifiedScheduler

### 10.1 判定规则

| | EventBus（`src/coara/event_bus.py`） | UnifiedScheduler（`src/coara/scheduler.py`） |
|---|---|---|
| 语义 | 发后即忘的发布/订阅 | 必须进 Root 对话循环的消息队列 |
| 典型内容 | trace 事件、UI 更新、后台完成通知 | 用户输入、工作流结果、入站事件、后台 auto-run |
| 错过代价 | 只是观测缺失 | 会破坏对话状态或丢 LLM 响应 |

经验法则：消息缺失会破坏对话或需要 LLM 响应 → scheduler；只服务 dashboard/CLI spinner/trace → EventBus。

实现要点：

- `EventBus`：topic 过滤订阅；每订阅者回调跑成 task，待处理 task 上限 `_MAX_PENDING_TASKS = 256`，过半即清理已完成项，满了记 warning
- `UnifiedScheduler`：`urgent` / `normal` 两条 `asyncio.Queue`（各 `maxsize=512`），消费时 urgent 永远优先；队列满直接丢弃并 warning

### 10.2 入站路由

`src/coara/inbound_router.py` 负责 scheduler 消息分派。消息内容体系下，事件源与提醒到点只落工作空间动态收件箱、后台完成只进驻发起会话历史，均**不再**入队叫醒主会话；剩余：

- 工作流结果：经 `WorkflowBridge` 把子进程事件转成 scheduler 消息；默认不注入主会话（落动态 / Web UI / 后台完成通道）

### 10.3 后台任务完成通知链路

```text
后台任务完成通知链路
├── bash 任务完成（BashBackgroundRunner._publish_completion()）
│   └── EventBus.publish(background_task_complete, kind=bash)
├── agent 后台任务完成（BackgroundAgentManager 包装协程的 finally）
│   ├── trace: background_agent_complete（始终发）
│   ├── 同工作空间判定（启动时记录的 launch_workspace_dir vs 父级当前 workspace_dir）
│   │   ├── 一致 且 非 aide/janitor/daily → EventBus.publish(background_task_complete, kind=agent)
│   │   └── 不一致（用户已切工作空间）→ 跳过注入，仅留 trace + TaskStore（跨工作空间防污染）
│   └── TaskStore 终态更新写回启动时的工作空间（用启动时捕获的路径，避免写错空间）
└── Root._on_background_task_complete()
    ├── 目标 session 忙（回合进行中）→ submit_continuation_input()，下一轮迭代 drain 拾取
    └── 目标 session 空闲 → 完成内容 park 进驻会话历史并落盘（不自动跑回合；不再落工作空间动态收件箱）
```

不能在这里 `async with _process_lock` 再 append：该锁整个回合都被持有，await 会阻塞到回合结束，而那时已没有新迭代处理注入的消息。

---

## 十一、上下文窗口与压缩

`ContextWindowManager`（`src/context/window.py`）已完整接入主循环（`context_prep.py` 每轮调用）：

- **触发**：估算用量 ≥ 上下文窗口的 80%（`CompressionConfig.TOKEN_THRESHOLD = 0.80`）；上下文窗口取自压缩 profile 的 provider
- **策略**：旧历史经 LLM 压缩为 `<state_snapshot>`，默认保留最近 2 条（`PRESERVE_LAST_N = 2`，优先于比例）；比例 `PRESERVE_RATIO = 0.30`（保留约 30%，按字符数计）为显式关闭条数策略后的备选
- **门槛**：可压缩部分占比低于 `MIN_COMPRESSIBLE_FRACTION = 0.05` 时放弃压缩
- **失败兜底**：LLM 压缩失败降级为截断；仍超限则 `evaluate_guard()` 阻断（`context_blocked`）

**切分点安全**（`find_compression_split_point`）：从尾部向前遍历，维护 `pending_tool_results` 集合——遇到 TOOL 消息把其 `tool_call_id` 加入集合，遇到 assistant 消息把它声明的 tool_call 移出。**只有当集合为空时，USER 消息才是合法切分点**，否则压缩掉前缀会在保留尾部里留下孤儿 tool result（provider 会拒收）。另有 `_is_tool_safe_split` 对切分结果做正交校验。

配套的两项运行时增强（`config.yaml runtime_enhancements`）：

- **Rules Glob**（`src/coara/rules_glob.py`）：从 `.coara/rules/*.mdc` 按当前文件路径 glob 匹配注入规则；`rules_glob.enabled`（默认 true）、`max_total_chars` 8000、`max_rule_chars` 2000
- **Shell Notify**：shell 输出哨兵匹配唤醒；`shell_notify.enabled`（默认 true）、`default_debounce_ms` 5000、`default_max_notifications` 3

注入子系统（`src/coara/injections/`）：`environment_injector`（首轮环境种子，伪造 USER 消息保持 system prompt 静态）、`background_injector`、`workspace_message_injector`（工作空间动态）、`snapshot_injector`（压缩后快照）、`tags.py`（`<系统消息>` 等标签）、`tool_result_wrapper.py`。

---

## 十二、多工作空间与会话隔离

### 12.1 模型

- **RootCoara**：进程宿主（EventBus、scheduler、workflow、vault/reminder/event_source 等全局服务）
- **WorkspaceSession**：每个工作空间一份，持有独立 `CoaraBase`（`message_history`、`session_id`、工具、技能、`_process_lock`）
- **`foreground_coara`**：当前前台对话 agent；始终指向某个 session 的 `CoaraBase`。初始化时必须绑定，否则失败

权威叙述见 [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md) §3。

### 12.2 切换流程（`switch_workspace()`，`root.py`）

1. 经 `workspace_manager` 按名称/id 解析登记项
2. `ensure_workspace_session()` 创建或复用该空间的 `WorkspaceSession`，按端设 view（`set_view_workspace(end, name)`；CLI/Web/Matrix 各持独立 view，无全局前台）
3. 重新 `publish_active_runtime()`（active.json 换 session id）
4. 发 `workspace_switched` 事件——各端各自据此重建 TraceStore/视图

session 创建内容：共享 Root 的 persona/provider/`workspace_manager`；进程级无状态工具 + `register_runtime_tools()` + root-scoped 工具（`register_root_scoped_tools`：plan_mode / workflow / skill / ws / event_source，其中 `WsTool` 的 `parent_coara=root`）+ `tool` 网关 + `local_search`（延迟）/ vault / reminder；trace sink 指向 Root EventBus；`load_skills()`；从磁盘恢复近期会话。

### 12.3 持久化与恢复

`src/coara/workspace_state.py`：

- 索引：`<coara_home>/workspaces/<workspace_id>/session_state.json`（`session_id` + `last_updated`）
- 会话记忆：`session_events.jsonl`（**全端冷备/审计**；完整 `message_history` 含工具环与接续输入，恢复走投影重放）
- 回合 in-flight 标记：`turn_in_flight.json`（回合开始写、正常结束清；残留即进程中断，恢复时注入注记，见 §6.2）
- **web 聊天区数据源**：`ui/web_views.py` 服务端会话视图存储（08-31 起，每会话 `web_views/{subject}__{session_id}.jsonl`，view_seq 自增对账；实时显示=刷新恢复读同一份；孤儿回合标 `recovered`）。录像带退冷备，不参与 web 聊天区
- 超过 `session.idle_timeout_seconds`（默认 7200，2 小时）视为陈旧，下次切入开新会话（磁盘恢复与进程内缓存会话同规则）
- 关闭时持久化 `_sessions` 中全部 session（阻塞 IO 走 `to_thread`）

### 12.4 回合中切换

人类 / API 切换（`/ws`、Web、Matrix）**不**中断离开空间的进行中回合：该 `WorkspaceSession` 继续跑完当前 `process_message`；前台焦点转到目标空间。工具绑定各 session 的 `workspace_dir`（shell 默认 cwd 亦然），不依赖进程 `chdir`。切走后离开空间的输出照常显示并带空间名标记；Matrix/Web 入队即绑定发送时 session；后台完成通知按发起 session 路由——细则见 [`多工作空间与工作空间动态.md`](./多工作空间与工作空间动态.md) §3.4。

回合进行中由 LLM 调用 `ws(switch)`：检测到 `_inside_turn`，抛 `CoaraRunCancelledError("switch_workspace:<名>")`。编排器捕获后对**源**会话调用 `strip_ws_switch_tail`（从 `ws(switch)` 上溯到触发切换的用户输入，删除该用户输入及其后的切换尝试尾部，含 `ws(list)` 链），再输出 `[系统] 已切换到工作空间 <名>` 结束回合——**不**做整轮 `turn_history_start` 回滚。目标工作空间历史不受影响；离开的工作空间其余历史留在自己的 `WorkspaceSession` 里，切回即恢复。

### 12.5 活动运行时文件

本地唯一活动进程发布 `<coara_home>/runtime/active.json`（`src/coara/workspace_runtime.py`）：工作空间路径、别名、PID、session id、Matrix 房间绑定。Matrix/移动入站靠它路由；读取时忽略 PID 已死的陈旧条目；优雅关闭时按 PID 匹配清除。

### 12.6 全局服务不随会话切换

`event_bus`、`scheduler`、`workspace_manager`、`workflow_bridge`、vault/reminder/event_source 服务都留在 RootCoara 上跨会话共享；隔离的只有按会话状态（对话、工具、技能、进程锁）。

---

## 十三、工作流系统

### 13.1 对外契约：内核投影（WDL）

2026-08-16 起工作流唯一核心是**编排图**（`src/workflow/core`）：节点是智能体（只有一种），边是唯一拓扑，控制流全部塌缩为图的形状。WDL 文本不再是独立语言，只是图的 **canonical 序列化投影**（`src/workflow/core/serde.py`），Agent、引擎、UI 编辑器三方共用：

- **节点只有一种**：智能体（`id` / `task` / `input` / `routes` / `max_activations`）；没有 if/for/while/parallel/wait/merge 节点
- **边**：`from` / `to` / `on`（`success` 缺省 / `error` 失败兜底路由）；没有独立 data 边，依赖与路由都是边集派生的只读视图
- **数据依赖**：完全经节点 `input` 模板表达（`{{steps.y.text}}` 引用上游结果，或字面量种子），模板引用即隐式依赖（`src/workflow/core/semantics.py` 校验时推导）；**无图级 inputs**（输入归节点）
- **控制流即拓扑**：并行=扇出、汇聚=扇入、分支=`routes: one`、循环=回边、重试=`on: error` 边；循环终止由 `max_activations` 激活上限兜底（全局默认 100，节点可覆写）

每个节点运行时都是一个完整的 `CoaraBase` 智能体（工具循环、技能、防护、trace），经引擎 `_delegate_pool` 复用。详见 [`docs/节点即智能体.md`](节点即智能体.md)。

### 13.2 提交链路：Root → 引擎

```text
提交链路的调用层级（引擎侧模块已随执行层剥离至独立 WDL 软件 wdl/；内核只做投影校验与文件宿主）
├── orchestrator(action="run", draft_id=…|wdl=…) → 校验内核投影后交独立 WDL 软件执行
│     （save 先 validate_projection → normalize_projection，src/workflow/draft_service.py）
├── WorkflowBridge.submit_workflow()（src/coara/workflow_bridge.py）——已随执行层剥离
└── WorkflowEngineController（src/workflow/engine_controller.py）——已随执行层剥离
    ├── mp.Queue 发 {"type": "start_workflow", task_id, wdl}
    └── 按需拉起 mp.Process(target=workflow_engine_main)
        └── WorkflowEngineRuntime（src/coara/workflow_engine.py）——已随执行层剥离
            ├── 命令循环：start / cancel / resume / inject
            ├── KernelGraphRunner（wdl/src/wdl/core/kernel_runner.py）执行内核投影
            ├── WorkflowPersistence（wdl/src/wdl/persistence.py，SQLite/aiosqlite）持久化实例状态
            └── mp.Queue 回送事件 → Bridge 转成 EventBus trace + scheduler 消息
```

`WorkflowBridge` 同时维护活动实例集合与终态动态标题文案（完成/失败/已取消），并同步到工作空间动态。

### 13.3 引擎内部

- **delegate 池**：`_delegate_pool` 按池键（workspace | persona | meta_task | provider | model）复用 `CoaraBase` 实例——acquire 弹空闲实例（无则新建）跑一个节点后 release 放回，避免重复初始化；并行分支各取不同实例；引擎关闭时统一 shutdown 池
- **节点执行**：`_kernel_node_executor` 把节点 id 作 persona 名、task 作 meta_task 构造 `WorkflowNodeAgent`，跑完整子智能体回合后把最后一条 assistant 消息作为 `steps.<节点名>.text` 交付
- **激活语义**：`ActivationEngine`（`src/workflow/core/semantics.py`）做 wait/kick 边分类、就绪判定、激活上限裁决；两宿主共用
- **持久化**：`busy_timeout = 5000`ms；终态实例超过 30 天先归档到 `workflow_instances_archive` 再删除（`prune_completed_instances(keep_days=30)`）；关闭时显式 `persistence.close()`（aiosqlite 连接是非守护线程，不关会拖住进程退出）
- **父活检测**：引擎是非 daemon 子进程，主循环每次队列空转 tick（1s）计数，每 5 tick 经 psutil 探一次父进程（ppid + create_time 防 pid 复用）；父进程已死则先 `recover_orphaned_running` 再有秩序退出（`run()` 末尾 `os._exit(0)` 兜底，防泄漏线程拖住退出）

### 13.4 挂起与恢复

- **中断恢复**：引擎异常退出后重启把 running 实例标记为 `interrupted`，经 Web UI `/resume` 或引擎 resume 恢复；`KernelGraphRunner.resume` 认领实例（写 RUNNING，fenced 下 owner 换当前令牌）
- **唤醒扫描**：`WorkflowWakeScanner`（`src/workflow/scanner.py`）每 5s 扫时间唤醒与事件超时（间隔可用 `COARA_WAKE_SCAN_INTERVAL` 配置）
- **外部事件**：`RootCoara.inject_workflow_event(instance_id, event_type, payload)` 经 bridge 注入子进程

---

## 十四、Prompt 系统

- **Agent 配置**：`src/coara/prompts/agents/{name}.yaml`（配置）+ `{name}.md`（系统 prompt 正文）配对。当前有 `root`、`coaras`、`aide`、`config-assistant`、`daily`、`flow-root` 六组；`builtin_agents.py` 加载子智能体，`AgentRegistry` 加载 root。janitor 不是 agent（无独立 persona），提示词在 `prompts/injections/`（与 compression.md 同性质，不经 agents 注册表）
- **YamlPromptLoader**（`src/prompt/yaml_loader.py`）：解析 `extends`、`system_prompt_args`、`${VAR}` 替换、`{{INCLUDE:path}}` 内联包含；模块路径先相对 YAML 所在目录、再相对 prompt 根（`src/coara/prompts/`）
- **PromptBuilder**（`src/prompt/builder.py`）：注入运行时变量（`${COARA_WORK_DIR}` 及 YAML `system_prompt_args` 声明的键）
- **运行时模板**：`src/coara/prompts/injections/compression.md`（压缩快照）
- **工具描述**：内联在各工具类上（类级 `description`），动态片段注册时 `.replace()` 注入（如 delegate 的子智能体列表 `${SUBAGENT_LIST}`）
- **动态提醒**走消息层注入（todo 状态、后台完成、压缩快照），不烘焙进静态 system prompt——首轮环境种子也伪装成 USER 消息，提高 provider 缓存命中（详见 [`docs/PROMPT_CACHE_POLICY.md`](PROMPT_CACHE_POLICY.md)）

---

## 十五、安全与治理

### 15.1 两层防线

1. **调用层**（`config.yaml security.call_policy` + `src/agent/tool_policy.py`）：交互式确认弹窗；`call_policy.prompt` 强制确认列表（§7.4）
2. **执行沙箱**（`src/tools/sandbox.py`）：仅对 `trust_level="untrusted"` 的调用方生效（`process_message(trust_level=…)`，如 Matrix 陌生消息）；按基名+危险标志拦命令、按 glob 拦路径、按主机/IP 段拦 URL、净化环境变量

### 15.2 Prompt 注入检测

`src/tools/security.py` 的 `detect_suspicious()` 扫描常见注入模式（中英文），外部内容用 `wrap_external_content()` 包装标明信任边界。

---

## 十六、可观测性：trace、日志与 Web UI

### 16.1 数据流

```text
trace 数据流
├── 产生：CoaraBase / RootCoara / WorkflowEngine 发 TraceEvent
├── 汇聚：EventBus.publish()
├── 落盘：TraceStore（{coara_home}/workspaces/{id}/traces/；无 coara_home 时 .coara/data/）
└── 展示：Web Chat + 右侧工具活动侧栏（`trace_batch` WS；侧栏可经 `/api/trace/events?kinds=tool` hydrate）；无独立 Trace 复盘页。CLI 动态区订阅同一 EventBus（内存投影，不写 `activity/`）
```

主要事件类型（均在代码中可检索）：`user_message` `turn_start` `chat_chunk` `llm_turn_complete` `llm_error` `llm_switched` `turn_continue` `turn_interrupted` `turn_failed` `turn_timing` `context_blocked` `context_compressed` `output_truncation_recovery` `iteration_limit` `continuation_input_injected` `workflow_draft_continue` `subagent_start` `subagent_complete` `subagent_failed` `subagent_message` `background_agent_start` `background_agent_complete` `background_task_complete` `workspace_switched` `initialized` `conversation_message` `final_response` `completed`。

### 16.2 TraceStore 上限

`src/ui/trace_store.py`：详情 JSON 软上限 `_MAX_DETAIL_FILES = 5000`（超出裁最旧 20%，每 20 次写入检查一次）；JSONL 单文件 50MB 轮转（保留一个 `.1` 备份，每 10 次写入 stat 一次）。`append_event` 去重集合 `_written_event_keys` 的 key 中 content 以 sha256 摘要驻留（去重语义不变），长会话不再驻留完整正文字符串。

### 16.3 Web UI 性能架构（逐条核实）

`src/ui/web_server.py` 的七项设计：

1. **trace 批处理**：事件收集进 `_trace_batch`，`_trace_flush_loop` 每 100ms 作为单条 `trace_batch` WS 消息冲刷——避免流式期间每事件一个 `create_task` 洪泛事件循环
2. **轻量心跳**：`_heartbeat_loop` 每 5s 只发小 runtime 字典（`session_id`/`status`/`provider`/`model`/`running`/`alive`），不读 JSONL；`_last_runtime_snapshot` 变更检测，无变化跳过推送；完整状态快照只在 WS 首连发一次
3. **工作空间切换一致性**：订阅 `workspace_switched` 事件 → `_refresh_trace_store` 重建 store 并同步 handlers 引用——任何来源（CLI `/ws switch`、LLM `ws` 工具、Web 端点）发起的切换都生效
4. **非阻塞子进程关闭**：engine 的 `process.join()` 走 `asyncio.to_thread` + `wait_for` 超时 + 强杀兜底（§4.3）
5. **全并行 root 关闭**：`shutdown_root_services` 单 `gather` 并行（§4.3）
6. **共享 root 不重复关闭**：CLI+Web 模式 `_owns_root = False`，`WebServer.stop()` 不调 `root.shutdown()`
7. **紧凑 CLI 退出**：Matrix dispatcher 排空 3s、任务取消 gather 5s（`chat_runner.py`）

其他：WS 连接 `max_msg_size = 256KB`、心跳 30s；REST/WS 受 token 保护（`src/ui/dashboard_auth.py`，query `?token=` 或 `X-Coara-Token` 头）；文件上传上限 50MB。

回合排队（`_handle_chat`）：活跃回合期间纯文本进 continuation 队列注入当前回合；带图消息（continuation 队列只承载 str）经 per-session 回合链（`_turn_tails`）排在当前回合及其 leftover 之后，按到达顺序作为下一完整回合执行——不再直接抢 `_process_lock` 插到先到的接续输入之前；输入框占位文案对带图消息相应提示「带图消息将排队到下一回合」。

### 16.4 日志

`setup_logger`（`src/core/logger.py`）：文件日志在 coara home 的 `logs/coara.log`（本地模式 `<workspace>/.coara/logs/coara.log`），10MB 轮转、保留 7 天；工作空间错误事件另写 `<workspace>/.coara/logs/errors.jsonl`（`src/core/error_log.py`）。token/工具用量经 `usage/events.jsonl` 单独聚合（`coara usage summary` 离线查询）。

---

## 十七、后台任务、事项、提醒与动态

### 17.1 bash 后台任务

`BashBackgroundRunner`（`src/background/bash_runner.py`，单例）：

- 每个任务一个真子进程，Windows 下 `CREATE_NEW_PROCESS_GROUP` 隔离信号
- 输出写 `<workspace>/.coara/tasks/<task_id>/output.log`
- 状态机：`running` / `completed` / `failed` / `killed` / `timed_out`，持久化到 `TaskStore`（与 agent 后台任务统一；agent 硬停用 `delegate(action=stop)`；LLM 侧已无 task 工具）
- 完成经 `_publish_completion()` 发 `background_task_complete`（kind=bash）进通知链路（§10.3）

### 17.2 个人提醒（reminder）

`ReminderService`（`src/reminders/service.py`）：`reminder` 工具增删查个人提醒；时区默认 `Asia/Shanghai`、tick 默认 1s（`config.yaml reminders:`）；到点经 `_enqueue_to_scheduler` 进对话循环。

### 17.3 事件源

`EventSourceManager`（`src/event_sources/manager.py`）：文件监听 / 轮询 / webhook（默认 `127.0.0.1:8765`）/ cron 四种 kind；事件定义在 `<workspace>/.coara/matters/definitions/*.yaml`；事件内容**无条件**落所属空间动态收件箱，`handle: janitor` 时叫醒管家 janitor 过目（`dispatch_janitor_message_review`，`src/coara/workspace_protocol.py`）；工作流 trigger 注册表命中即直启 WDL。

### 17.4 工作空间动态（updates）

按工作空间名称接收的入站事件动态流。`WorkspaceUpdatesStore`（`src/workspace/updates/store.py`）持久化在 `<workspace>/.coara/inbox/`（空间自治布局；旧集中布局 `users/default/inbox/{name}/` 启动时迁移），每工作空间最多 500 条（超出裁最旧）。每条带处置轨迹（`disposition` + `reviewed_by/at/note` + `expires_at`）；janitor watcher 每拍纯规则过目（过期 / low 超龄自动 dismissed，`store.sweep`）。查看：janitor `review` 工具、用户聊天 `/ws updates` 与 Web UI。

---

## 十八、技能系统

技能是**面向 LLM 的运行时指令**（不是代码插件）：每个技能一个目录，含 `SKILL.md`（YAML frontmatter + Markdown 正文）。增删技能只在磁盘上放/删目录。

- **发现顺序**（`SkillManager.discover()`，后者覆盖前者）：内置 `skills/` → 用户级 `<coara_home>/users/default/skills/` → 工作区级 `.coara/skills/`（需 `is_trusted`）→ 额外路径
- **实例隔离**：每个 CoaraBase 持有自己的 `SkillManager`（`self.skill_manager`，不再有模块级全局单例），发现池与 mtime 缓存互不影响——多工作空间并存时后 discover 者不再覆盖前者；Web 设置页的技能列表走 `control_plane` 的独立只读实例
- **运行时使用**（仅 Root 的 `skill` 工具）：`skill(action=search)` 在全部技能（含挂起）中按关键词查候选（query 留空返回全量）；`skill(action=activate)` 把完整 SKILL.md 加载进 `message_history`。system prompt 经 `${COARA_SKILL_LIST}` 占位符注入启动时发现的技能名（会话内静态；`listed` 标记的技能给名+描述，挂起的只给裸名并指引用 search 查描述）
- **会话状态**：`SkillSessionState.activated` 跟踪已激活技能；`/new` 清空
- `config.yaml skills.default_include` 只在列表输出标 `[default]`
- 当前内置：`research`、`skill-creator`、`工作空间管理`、`event-source`（`workflow` 技能已退役为挂起工具 orchestrator；`PPT`/`frontend-design`/`long-document` 已于 2026-08-19 裁撤；原 `agnes` 技能已并入内置 `media` 工具）

权威文档：[`docs/技能系统.md`](技能系统.md)。

---

## 十九、宝箱（vault）

用户面称「宝箱」，代码/配置用 `vault`。密码开门 → 明文 `open/`；关门封存为 `sealed/*.cvault`。

- Agent 侧只有 `vault(action=open|close|status)`（`unlock`/`lock` 是 open/close 的别名）；开门后对 `open/` 用普通文件工具
- 锁定时其他工具硬拒已封存路径（`src/vault/guard.py`）
- 密码走侧信道绝不进 LLM 上下文：CLI getpass、Web `vault_reply`、Matrix `[COARA_VAULT]`；`COARA_VAULT_PASSWORD` 可自动解锁
- PC CLI：`coara vault init/status/unlock/lock/list/read/write/import/passwd`

安全模型与已知限制见 `docs/manual/18-安全与治理.md`。

---

## 二十、资源安全上限速查

| 位置 | 上限 |
|------|------|
| `EventBus` | 256 个待处理订阅者任务（过半即清理） |
| `UnifiedScheduler` | urgent / normal 各 512 条，满则丢弃 |
| `TraceStore` | 5000 个详情 JSON（超裁最旧 20%）；JSONL 50MB 轮转 |
| `SubagentStore` | 500 条记录（超 50 余量才裁剪，先逐空闲记录） |
| `TaskStore` / `BashBackgroundRunner` | 各 500 条终态记录 |
| `WorkspaceUpdatesStore` | 每工作空间 500 条动态 |
| `WorkflowPersistence` | 终态实例 30 天后归档再删 |
| `LoopDetector` | 签名历史 100 条 |
| delegate | 并发默认 10（可配）；子智能体 1500 轮工具迭代 |
| 图片处理 | 最长边 2000px、base64 ≤ 5MB（`src/utils/image_processor.py`） |
| AGENTS.md / 用户规则注入 | 无字节上限（`src/coara/injections/context_modules.py` 全文注入） |
| Web UI | WS 消息 256KB；上传 50MB |
| 会话陈旧 | 7200s 无更新开新会话 |

---

## 二十一、LLM 抽象层

```text
src/llm/
├── provider.py      # LLMProvider / LLMResponse / ToolCall / StreamChunk 接口
├── registry.py      # provider 注册表（初始化、默认 provider）
├── service.py       # llm_service：profile 解析（如 Profile.CONTEXT_COMPRESSION）
├── anthropic.py / openai.py   # 具体实现（消息/工具定义转换、complete()）
├── stream.py        # StreamAggregator / collect_stream
├── retry.py         # 重试
└── endpoints.py / usage.py / model_catalog.py …
```

- provider 声明 `supports_tools(model)`；原生调用失败直接上抛（已移除静默 ReAct `<invoke>` 降级）
- `LLMProvider.close()` 释放 HTTP 客户端；关闭时经 `llm_service.close()` 统一回收
- 主循环默认走 `provider.complete()`；CLI 流式输出由 stream 设置与 `_assistant_stream_hook` 控制
- 流式调用中途 `LLMError`：只有零 delta（未收到任何内容分片）才允许回退非流式整 prompt 重发；已出部分内容直接报错不重发，失败 attempt 已计量的部分 usage 以 `llm_turn_partial` 事件按 `kind=llm_turn` + `status=partial` 计入用量收集，账面与 provider 扣费可对账

---

## 二十二、测试布局

- 默认 `pytest tests/ -q` 收集 **1603** 例（核心冒烟）
- `pytest tests/ -m extended -q` 增加 **374** 例边界/集成测试（路径清单在 `tests/conftest.py::_EXTENDED_TEST_PATHS`，自动打标）
- 完整套件 `pytest tests/ --override-ini="addopts=" -q` 共 **1978** 例（含 real_env；PowerShell 下勿用 `-m ""`）
- 标记：`extended`（详细边界）、`e2e`（Playwright 浏览器）、`real_env`（真实 provider/网络）
- 布局：`tests/test_{agent,background,cli,coara,collection,context,core,event_sources,llm,matrix_client,memory,prompt,records,reminders,runtime,skills,todos,tools,ui,vault,workflow,workspace}/` + 共享 `helpers.py`（`FakeProvider`、`make_test_coara`）、`real_env_helpers.py`（`managed_initialized_root`）

详见 [`tests/README.md`](../tests/README.md)。

---

## 附录：源码索引

### A.1 入口与交互

- `src/cli/main.py`（click 入口、前端分派、离线子命令）
- `src/cli/chat_runner.py`（CLI 会话管理、退出超时）
- `src/ui/web_server.py` / `src/ui/dashboard_handlers.py` / `src/ui/trace_store.py` / `src/ui/dashboard_auth.py`
- `src/coara/service_desk.py`（CLI `@服务台` 投递）

### A.2 运行时核心

```text
src/coara/
├── base.py                # CoaraBase：process_message、bootstrap_tools、initialize
├── root.py                # RootCoara：switch_workspace、后台通知订阅、submit_workflow
├── root_lifecycle.py      # initialize_root_services / shutdown_root_services
├── turn_orchestrator.py   # run_turn_loop：内层工具循环与异常分支
├── turn_loop/             # context_prep / tool_results / turn_input / user_turn_injectors
├── turn_completion.py     # provider 调用、_await_interruptible
├── workspace_session.py / workspace_state.py / workspace_runtime.py
├── workflow_bridge.py     # 工作流子进程 IPC 桥
├── event_bus.py / scheduler.py / inbound_router.py
├── background_agent.py    # BackgroundAgentManager
├── subagent_store.py / builtin_agents.py
├── tool_manager.py        # 白名单 / plan-mode / deferred 可见性
├── runtime_tools.py       # 运行时工具注册
├── injections/            # 环境、后台、动态、shell、快照注入
├── tool_output/           # 三通道：pipeline / diff / syntax / read_format / gate_preview
└── prompts/agents/        # {root,coaras,aide,janitor,daily}.yaml + .md
```

### A.3 能力子系统

```text
src/agent/    # executor / hooks / loop / react / turn_control / output_truncation / tool_policy
src/tools/    # base / registry / cache / sandbox / security / builtin/**
src/context/window.py        # 压缩与守卫
src/runtime/  # tool_output_store / spill_policy / spill_read / usage_collector
src/workflow/ # core/（model/serde/semantics/kernel_runner）/ draft_service / draft_store / editor_access / engine_controller / inputs_schema / management / node_llm / paths / persistence / result / scanner / scheduler / trigger_registry
src/skills/   # manager / loader
src/vault/    # service / store / crypto / guard
src/prompt/   # builder / agent_registry / yaml_loader / environment
src/llm/      # provider / registry / service / anthropic / openai / stream / retry
src/core/     # types / config / logger / errors / abort / coara_home / error_log
```
