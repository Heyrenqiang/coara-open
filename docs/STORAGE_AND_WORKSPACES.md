# coara 存储布局与工作空间目录

> **Canonical 存储参考**，与 `src/core/coara_home.py` 对齐。
> **工作空间概念总览**（四种目录、cwd、name、切换）→ [工作空间与目录.md](./工作空间与目录.md)。
> 多工作空间编排、事件源 → [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md)。
> 动态存储细节 → [工作空间动态模型.md](./工作空间动态模型.md)。
> 配置项 → [CONFIGURATION.md](./CONFIGURATION.md)。

---

## 目录

1. [两类目录](#1-两类目录)
2. [coara Home 完整布局](#2-coara-home-完整布局)
3. [workspaces/{workspace_id}/ 槽位内容](#3-workspacesworkspace_id-槽位内容)
4. [Trace / Log / 错误日志 / 用量](#4-trace--log--错误日志--用量)
5. [未配置全局 Home 时的退化布局](#5-未配置全局-home-时的退化布局)
6. [启动清理与遗留回退](#6-启动清理与遗留回退)
7. [资源上限](#7-资源上限)
8. [常用路径与命令](#8-常用路径与命令)
9. [常见误解](#9-常见误解)
10. [源码索引](#10-源码索引)

---

## 1. 两类目录

coara 运行时涉及**两种物理目录**，不要混为一谈：

| 类型 | 谁拥有 | 典型路径 | 放什么 |
|------|--------|----------|--------|
| **工作空间目录** | 用户 | `D:\code_ws\v8` | 源码、`AGENTS.md`、业务文件 |
| **coara Home** | coara 运行时 | `D:\coara`（或 `<工作空间>/.coara`） | 登记表、trace、log、宝箱、动态、事项、子代理持久化等 |

```text
用户工作空间目录（源码）              coara Home（运行时数据）
D:\code_ws\v8\test-coara    ←→   D:\coara\workspaces\test-coara-2b4bd8d172\
  src/ AGENTS.md ...                 traces/ logs/ subagents/ ...
                                   users/default/assets/vault/（全局宝箱）
```

**原则**：工作空间目录只放用户产物；coara 产生的 trace、log、宝箱等一律进 coara Home，不污染 git 工作空间。唯一的例外是**错误日志**（§4）和 **LLM 调用镜像**（§5），它们始终写在工作空间目录的 `.coara/` 下。

coara Home 的解析顺序：合并配置的 `coara_home` 字段 → `COARA_HOME` 环境变量（配置链 `resolve_config_home()` / `resolve_bootstrap_coara_home()` 还会读 Windows 注册表里的用户/机器级持久值）→ 否则 `<cwd>/.coara`（workspace 侧 `resolve_coara_home()`）。

---

## 2. coara Home 完整布局

配置 `coara_home`（如 `D:\coara`）后，canonical 布局为：

```text
{coara_home}/
  registry/
    workspaces.yaml             # 工作空间登记（键 = workspace_id）
    permissions/                # 可选的按工作空间 ACL
  system/                       # 机器级配置与日志
    config.yaml                 # 全局运行时配置
    providers.yaml              # LLM provider 配置
    .env                        # API key 等
    logs/                       # dashboard、隧道等系统日志
  users/default/                # 用户级资产
    config.yaml                 # 用户级覆盖（优先级高于 system/config.yaml）
    llm_preferences.yaml        # /model --global 写入的全局默认
    skills/                     # 用户技能（enabled.json、custom/）
    records/
      agent/                    # agent 笔记、digests、curator_state
      user/                     # 用户收藏（entries/ + files/）
    workflows/
      drafts/                   # WDL 草案
      triggers.json             # 工作流触发器注册表
      instances.db              # 工作流实例 SQLite
    matters/                    # 事件源状态（定义在空间自治布局下位于各空间 `.coara/matters/`）
      definitions/*.yaml        # 无 registry（工作空间功能未启用）时的回退位；常规布局在 `<ws>/.coara/matters/definitions/`
      .state/                   # 事件去重、冷却、轮询水位（保持集中）
    assets/
      vault/                    # 宝箱（加密）：vault.meta.json + sealed/ + open/
    works/                      # 用户产出（PPT、PDF、文档等）
    inbox/{name}/               # 工作空间动态（updates）——空间自治布局下已迁入各空间 `.coara/inbox/`，此处为旧集中布局兼容位
    inbox/{name}.read_marker.json  # 动态的已读水位线（旧布局；新布局在 `<ws>/.coara/inbox/read_marker.json`）
    logs/                       # 用户级日志
    sessions/                   # 会话数据
  runtime/
    active.json                 # 本机正在运行的 CLI 绑定（手机 Matrix 路由用）
  .coara/
    tasks/                      # 后台任务统一存储（bash + agent，TaskStore JSON）
  workspaces/
    {workspace_id}/             # 每个工作空间的运行时槽位（非源码），见 §3
```

要点：

- **workspace_id 算法**：`{目录名 slug}-{path SHA1 前 10 位}`（`workspace_id_for()`），同一绝对路径永远映射到同一 id。
- 启动时 `ensure_workspace_layout()` / `ensure_user_layout()` 幂等创建上述目录骨架。
- 后台 bash 任务的**输出日志**始终写在工作空间目录本地：`{workspace}/.coara/tasks/{task_id}/output.log`；任务状态记录（TaskStore JSON）则统一放 `{coara_home}/.coara/tasks/`。
- 宝箱磁盘形态：锁定 = `sealed/<uuid>.cvault` + `vault.meta.json`；解锁 = 明文 `open/`。详见 `src/vault/`。
- 配置文件读取顺序：`system/providers.yaml`、`system/config.yaml` → `users/default/config.yaml`（后者覆盖前者）。

---

## 3. workspaces/{workspace_id}/ 槽位内容

每个运行过的工作空间在 `workspaces/{workspace_id}/` 下有一个槽位：

| 子目录/文件 | 内容 |
|-------------|------|
| `traces/trace_events.jsonl` | 结构化 trace 事件流（可观测、侧栏补给、turn timing） |
| `traces/trace_details/` | trace 详情 JSON（5000 个软上限，超出裁最旧 20%） |
| `logs/coara.log` | loguru 文本运行日志 |
| `subagents/` | 子代理运行状态落盘（后台任务查询；500 条上限） |
| `tool_outputs/` | 工具输出 spill 持久化（超大工具结果全文） |
| `usage/events.jsonl` | token/工具用量事件（`coara usage` 离线聚合） |
| `session_state.json` | 最近 `session_id` + `last_updated`（会话恢复锚点） |
| `session_events.jsonl` | 会话事件日志（**全端冷备/审计**，append-only，永久保存）：完整 `message_history`（含 tool_calls / tool_results / 接续输入）+ turn 边界 + `session/meta`（usage 快照，供恢复后 CLI 工具栏直接显示 context）。恢复 = 事件投影重放，见 SESSION_EVENT_SOURCING.md。注：**web 聊天区不读它**——web 唯一数据源是 `web_views/` 视图存储 |
| `session_projection.checkpoint.json` | **投影检查点**（启动提速，治本）：会话持久化时缓存"最近 session 的投影 + seq 水位"；启动恢复直接读检查点 + 只增量投影水位之后的纯追加事件。增量含 `history/shadow`（压缩/截断影子重写）或检查点缺失/损坏/session 不匹配时回退全量投影（仍经倒序快读只读尾部目标段）。恢复结果与全量投影逐条一致，磁带一字节不动。见 `src/session_log/checkpoint.py` |
| `web_views/` | **web 端会话视图存储**（08-31 起 web 聊天区唯一数据源）：每会话一个 `{subject}__{session_id}.jsonl`，回合帧（正文 chunk、user 消息、diff、files、错误）经 TurnStream 逐帧落盘；实时显示=刷新恢复读同一份。见 docs/架构契约.md「web 端数据载体」 |

---

## 4. Trace / Log / 错误日志 / 用量

四类记录用途不同，路径也不同：

| 类型 | 路径 | 格式 | 用途 |
|------|------|------|------|
| **Trace** | `workspaces/{id}/traces/trace_events.jsonl` | JSONL 事件流 | 可观测、Web 工具侧栏补给、turn timing |
| **Log** | `workspaces/{id}/logs/coara.log` | loguru 文本 | 人类可读运行日志、排错 |
| **错误日志** | `{workspace}/.coara/logs/errors.jsonl` | JSONL | 工具失败、拦截、agent/LLM 错误（仅错误） |
| **用量** | `workspaces/{id}/usage/events.jsonl` | JSONL | token/工具用量（`coara usage summary`、Web `/usage`） |

```text
用户发消息 → TraceEvent      → trace_events.jsonl   （结构化、可分析）
           → loguru          → coara.log            （文本、grep 友好）
工具/Agent 出错 → error_log  → errors.jsonl         （始终在工作空间本地 .coara/ 下）
Token/工具用量 → usage       → usage/events.jsonl   （离线 / Web 用量查询）
```

**已停用**：`workspaces/{id}/activity/`（曾与 Trace 双写）。`ensure_workspace_layout` / TraceStore 启动时会删除遗留 `activity/` 目录。

注意：**错误日志不写进 coara Home**——即使配置了全局 Home，`errors.jsonl` 也始终落在**工作空间目录**的 `.coara/logs/` 下（`resolve_workspace_errors_log_path()`）。

分析 turn 耗时：[`scripts/dev/README.md`](../scripts/dev/README.md) § analyze_turn_timing（默认跟 live `active.json` 的工作空间）。

---

## 5. 未配置全局 Home 时的退化布局

未设置 `coara_home` / `COARA_HOME` 时，运行时数据落在**当前工作空间目录**下：

```text
{workspace}/.coara/
  data/              # trace（等同全局布局的 workspaces/.../traces/）
  logs/coara.log     # 文本日志（errors.jsonl 也在 logs/ 下）
  skills/            # 工作空间级技能覆盖
  tasks/             # bash 后台任务输出
```

适合单工作空间试用；**多工作空间长期使用请配置全局 Home**（如 `D:\coara`）。

注意：`{workspace}/.coara/logs/errors.jsonl`（错误日志）**与是否配置全局 Home 无关**，始终写在工作空间目录本地。LLM 调用镜像按工作空间 × 智能体落盘 `.coara/llm/llm-calls.jsonl`（每实例一行全文），供独立开发者工具 `coara-devtools` 查看最后一轮调用。

---

## 6. 启动清理与遗留回退

与旧布局相关的启动行为（`src/core/coara_home.py`、
`src/core/error_log.py`、`src/coara/base.py`）：

1. **临时槽位清理**：`workspaces/` 下 pytest 遗留的 `tmp*` 槽位每次启动删除（`ensure_workspace_layout` → `_prune_temp_workspace_slots`，仅全局 Home 布局下执行）。
2. **废弃的 `logs/sessions/` 审计目录**：启动时由 `_purge_legacy_audit_logs` → `purge_session_audit_logs` 幂等删除（原工具调用审计目录，已无写入方），覆盖本工作空间与 `workspaces/` 全部槽位。
3. **遗留 `activity/` 目录**：启动时幂等删除（曾与 Trace 双写，已停用）。
4. **遗留 `matters/store.json` 等日程卡文件**：启动时幂等删除 `users/default/matters/store.json`、`store.json.corrupt`、`runs.jsonl`（定时已改走 cron 事件源；不触碰 `definitions/` / `.state/`）。
5. **遗留 `data/`、`logs/toolcalls/` 等旧布局目录**：启动时不再自动清理（除上述 `activity/` 与第 4 项遗留文件）。
6. **遗留 `config/` 目录已废弃**：不再作为配置回退读取；配置读取只看 `system/` 与 `users/default/config.yaml`，新写入一律进 `system/`。

---

## 7. 资源上限

| 对象 | 上限 | 超出行为 |
|------|------|----------|
| trace 详情 JSON | 5000 个 | 裁掉最旧 20% |
| trace JSONL 单文件 | 50 MB | 轮转 |
| 工作空间动态 | 每工作空间 500 条 | 先删已归档，再删最旧的已读；**永不删未读** |
| 子代理持久化记录 | 500 条 | 先驱逐空闲记录，再驱逐最旧的非空闲记录 |
| bash 后台任务记录 | 500 条终态 | 裁剪 |
| 事件源去重键 | 每源 500 个 | 淘汰最旧 |

---

## 8. 常用路径与命令

### 8.1 查看当前绑定

```powershell
type D:\coara\runtime\active.json
coara ws list
```

### 8.2 CLI 启动 banner

`coara` 启动时会打印当前工作空间与关键路径（工作空间、workspace_id、coara_home、trace/log/errors 路径）。内核化（08-30）后 banner 由内核/attach 启动链各自打印，无独立 `banner_lines` 方法：

```text
workspace:     D:\code_ws\v8\test-coara
workspace_id:  test-coara-2b4bd8d172
coara_home:    D:\coara
trace:         D:\coara\workspaces\test-coara-2b4bd8d172\traces
log:           D:\coara\workspaces\test-coara-2b4bd8d172\logs\coara.log
errors:        D:\code_ws\v8\test-coara\.coara\logs\errors.jsonl
```

### 8.3 与其它文档的分工

| 层 | 路径 | 文档 |
|----|------|------|
| 登记 / 事件源 / 事项 | `registry/`、`users/default/matters/` | [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md)、[工作空间与事项制度.md](./工作空间与事项制度.md) |
| 工作空间动态 | `<workspace>/.coara/inbox/`（空间自治；旧集中布局 `users/default/inbox/` 启动时迁移） | [工作空间动态模型.md](./工作空间动态模型.md) |
| 每工作空间运行时数据 | `workspaces/{id}/` | **本文档** |
| 工作空间生命周期操作 | `coara ws add/remove` | [工作空间与目录.md](./工作空间与目录.md)、`skills/工作空间管理/SKILL.md` |

---

## 9. 常见误解

| 误解 | 实际 |
|------|------|
| `workspaces/` 里是源码 | 是 **coara 运行时数据**；源码在 registry `path` 指向的目录 |
| trace 和 log 是同一个文件 | **trace** = JSONL 事件流；**log** = 文本 `coara.log` |
| 错误日志也在 coara Home | 不是。`errors.jsonl` 始终在工作空间目录 `.coara/logs/` 下 |
| 在 v8 根目录开 CLI 的操作会记到 test-coara | **cwd 决定 workspace_id**；各目录各自独立槽位 |
| 手机 Matrix 是第三个工作空间 | 绑定 `runtime/active.json` 里的 live 进程工作空间 |
| `logs/sessions/` 里有工具调用审计 | 该目录已废弃，启动时会被清理 |

---

## 10. 源码索引

| 模块 | 路径 |
|------|------|
| 路径解析、布局创建、启动清理 | `src/core/coara_home.py` |
| CLI 绑定 active.json | `src/coara/workspace_runtime.py` |
| Root 发布/清除 runtime、初始化 | `src/coara/root_lifecycle.py` |
| Trace 持久化 | `src/ui/trace_store.py` |
| 活动投影（CLI 内存，不落盘） | `src/ui/activity_store.py` |
| 文本日志 | `src/core/logger.py` |
| 错误日志 | `src/core/error_log.py` |
| 用量 | `src/runtime/usage_store.py`、`usage_collector.py` |
| 子代理持久化 | `src/coara/subagent_store.py` |
| 工具输出 spill | `src/runtime/tool_output_store.py` |
| 会话状态 | `src/coara/workspace_state.py` |
| 宝箱 | `src/vault/`、`src/tools/builtin/vault/vault.py` |
| 工作空间登记 | `src/workspace/registry.py`、`manager.py` |
| 事件源存储 | `src/event_sources/state.py`、`src/event_sources/types.py` |
| 工作流资产 | `src/workflow/draft_store.py`（生产侧）；执行层 `wdl/src/wdl/persistence.py` |
