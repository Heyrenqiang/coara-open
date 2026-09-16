# coara v8 文档索引

> **原则**：同一主题一份 Canonical（权威文档），其它文件只留链接；维护约定见 [`CONVENTIONS.md`](./CONVENTIONS.md)。
> AI 编码代理入口：[`AGENTS.md`](../AGENTS.md)。

---

## 按角色阅读

| 角色 | 路径 |
|------|------|
| **部署 / 发版** | [RELEASE_WORKFLOW](../deploy/gitee/RELEASE_WORKFLOW.md) · [DEV_VS_USER](./DEV_VS_USER.md) · [deploy/README](../deploy/README.md) |
| 新开发者 | [`../README.md`](../README.md) → [DEV_VS_USER](./DEV_VS_USER.md) → [工作空间与目录](./工作空间与目录.md) → [CONFIGURATION](./CONFIGURATION.md) |
| **通读系统（推荐）** | [`manual/`](./manual/README.md) 用户手册 20 章 + 附录 |
| 改代码的 AI | [`AGENTS.md`](../AGENTS.md) · [架构契约](./架构契约.md)（改动前必读） · [COARA_ARCHITECTURE](./COARA_ARCHITECTURE.md) · [技能系统](./技能系统.md) |
| 工作空间 / 多工作空间 | [工作空间与目录](./工作空间与目录.md) → [工作空间概况](./工作空间概况.md) → [多工作空间与工作空间动态](./多工作空间与工作空间动态.md) → [工作空间动态模型](./工作空间动态模型.md) |
| Workflow / 事件源 | [工作空间与事项制度](./工作空间与事项制度.md) · [`./WORKFLOW_SPEC.md`](./WORKFLOW_SPEC.md)（格式权威） · [节点即智能体](./节点即智能体.md) · [wdl 执行引擎](../wdl/README.md) |
| 事件源 | [WORKSPACE_EVENTS_IMPLEMENTATION](./WORKSPACE_EVENTS_IMPLEMENTATION.md)（运维） · [`../skills/event-source/EVENT_SOURCE_SPEC.md`](../skills/event-source/EVENT_SOURCE_SPEC.md)（智能体配置权威） · 技能 `event-source` |
| 事件 / webhook 运维 | [WORKSPACE_EVENTS_IMPLEMENTATION](./WORKSPACE_EVENTS_IMPLEMENTATION.md) · [TUNNELS_SETUP](./TUNNELS_SETUP.md) |
| Android / Matrix 链路 | [MATRIX_APP_LINK](./MATRIX_APP_LINK.md)（通信 Canonical）· [Android远程控制与输入交互](./Android远程控制与输入交互.md) · [`../android-app/README.md`](../android-app/README.md) |
| 改配置 | [CONFIGURATION](./CONFIGURATION.md)（逐键速查） |
| 已知问题 | [REMAINING_ISSUES](./REMAINING_ISSUES.md) · [manual/18 §18.9](./manual/18-安全与治理.md)（面向用户的安全限制） |

---

## Canonical 文档

### 领域模型（工作空间 / 动态 / 事项）

| 文档 | 内容 |
|------|------|
| [工作空间与目录.md](./工作空间与目录.md) | **概念总览**：四种目录、name/path/workspace_id、cwd、切换 |
| [空间切换设计.md](./空间切换设计.md) | **切换 Canonical**：WorkspaceSession 会话切换、边界与状态迁移 |
| [空间性质模型.md](./空间性质模型.md) | **空间 Canonical**：空间＝数据+投影+写入契约、四正交维度 |
| [空间模型与内容注册表.md](./archive/空间模型与内容注册表.md) | **已归档概念备忘**（2026-09-08 讨论共识，未落地；§3 已被空间性质模型修订）：一切皆是空间=内容+展示；内容类型注册表、default_content、程序不属于空间、开源布局推论 |
| [工作空间概况.md](./工作空间概况.md) | **概况 Canonical**：`ws.md` 内容/组织/时机；§8 与代码对照（注入、janitor、idle watcher） |
| [工作空间概况维护规范.md](./工作空间概况维护规范.md) | janitor 写作规范（骨架、硬规则、首次生成 / 会话边界流程） |
| [STORAGE_AND_WORKSPACES.md](./STORAGE_AND_WORKSPACES.md) | **存储 Canonical**：coara Home 布局、trace/log/errors/usage 路径、资源上限 |
| [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md) | **编排 Canonical**：WorkspaceSession（§3）、Root/janitor 分工、事件源 `salience` / `handle` |
| [工作空间动态模型.md](./工作空间动态模型.md) | **动态 Canonical**：updates 数据模型、已读水位线、红点、去重裁剪、Matrix 同步 |
| [工作空间与事项制度.md](./工作空间与事项制度.md) | Workspace + Workflow + Janitor 管家 + 事件源如何配合 |

### 工作流

| 文档 | 内容 |
|------|------|
| [`./WORKFLOW_SPEC.md`](./WORKFLOW_SPEC.md) | **WDL 格式单一权威源**（字段语义、节点类型、校验规则） |
| [`../skills/event-source/EVENT_SOURCE_SPEC.md`](../skills/event-source/EVENT_SOURCE_SPEC.md) | **事件源配置权威源**（kind、字段、salience/handle、模板；运维见 WORKSPACE_EVENTS） |
| [`../wdl/README.md`](../wdl/README.md) | wdl 执行引擎（独立软件）：图内核、引擎、持久化、画布工作台 |
| [节点即智能体.md](./节点即智能体.md) | 工作流节点模型：每个 `run` 节点是同构 CoaraBase 智能体 |

### 配置 · 架构 · 约定

| 文档 | 内容 |
|------|------|
| [CONFIGURATION.md](./CONFIGURATION.md) | `.env`、`config.yaml`、`providers.yaml` 逐键速查、LLM profiles |
| [DEV_VS_USER.md](./DEV_VS_USER.md) | 开发机 vs 用户机：公用运行时、COARA_HOME、用户默认 templates、发布隔离 |
| [CONVENTIONS.md](./CONVENTIONS.md) | 文档分层、标点、用户可见中文文案、文件 I/O 基线、delegate 工作流命名 |
| [架构契约.md](./架构契约.md) | **四梁八柱一页纸**：分层铁律、core 原语、内核不变量、端接入规矩 |
| [COARA_ARCHITECTURE.md](./COARA_ARCHITECTURE.md) | 代码对齐的深度架构（22 章 + 源码索引） |
| [重启机制设计.md](./重启机制设计.md) | **重启 Canonical**：安全交棒协议（落意图 → 让位 → 同模式拉起 → 端口就绪 → 交棒）、`/restart` 静默命令 |

### 机制专题

| 文档 | 内容 |
|------|------|
| [TOOL_OUTPUT_FRAMEWORK.md](./TOOL_OUTPUT_FRAMEWORK.md) | 工具输出三通道：Model / Terminal / Gate，及 spill 阈值 |
| [CLI_DISPLAY_FRAMEWORK.md](./CLI_DISPLAY_FRAMEWORK.md) | CLI 展示：scrollback / 动态区 / 回合产出 |
| [WEB_DISPLAY.md](./WEB_DISPLAY.md) | **Web 展示 Canonical**：聊天 hydrate/replay、多端隔离、`web_views`、跟话分段 |
| [PROMPT_CACHE_POLICY.md](./PROMPT_CACHE_POLICY.md) | 提示词缓存策略、按 provider 差异、上下文消息注入 |
| [TOKEN_COST_MODEL.md](./TOKEN_COST_MODEL.md) | 词元成本模型：费用与轮次/上下文/输出/缓存命中率的定量关系 |
| [USAGE_COST_DASHBOARD.md](./USAGE_COST_DASHBOARD.md) | 用量费用统计：价格配置、费用口径、WebUI 看板（PC 已实施；手机二期） |
| [SESSION_EVENT_SOURCING.md](./SESSION_EVENT_SOURCING.md) | 会话事件溯源（已实施）：`session_events.jsonl` 全端冷备/审计、分段轮转、派生投影；web 聊天区数据源见架构契约「web 端数据载体」 |
| [TODO_SYSTEM.md](./TODO_SYSTEM.md) | 会话 todo：数据模型、持久化、与 Turn 循环的交互 |
| [技能系统.md](./技能系统.md) | 技能加载优先级、`search`/`activate`、磁盘增删 |
| [子智能体重构.md](./子智能体重构.md) | 子智能体职责划分：并行优先（coaras / aide）与系统管家（janitor / daily） |
| [本地记录.md](./本地记录.md) | **Canonical**：record / local_search、`records/` 存储、origin、人侧入口 |
| 内部编排与自演示规划（蓝图，已归档 `../ref-doc/archive/`） | 流程控制原语进 harness、内外工作流一致、自演示技能；当前 flow 见 COARA_ARCHITECTURE §9.3，自演示见[具身工具架构](./具身工具架构.md) |
| [具身工具架构.md](./具身工具架构.md) | 具身器官总纲：coara 有手有嘴（embody），自拍手机旁观捕捉真实行为旅程 |
| [task_led_自导自演.md](./task_led_自导自演.md) | task-led 自导自演实施层：自己开启录制、真实干活、讲解、收尾成片 |
| [中文加载词库.md](./中文加载词库.md) | 加载词库事实源：CLI/Web spinner 轮换文案（`records/loading_phrases.json` 由其生成） |
| [系统术语表.md](./系统术语表.md) | 全系统概念统一词典：按端/域定位，锚点 `文件:符号`，改名前先对号入座 |

### 远程链路

| 文档 | 内容 |
|------|------|
| [MATRIX_APP_LINK.md](./MATRIX_APP_LINK.md) | **App ↔ PC 通信 Canonical**：架构、消息流、侧信道协议、可靠性语义 |
| [Android远程控制与输入交互.md](./Android远程控制与输入交互.md) | 手机 slash 命令、输入区手势、Matrix 命令链、Windows 重启 |

### 运维

| 文档 | 内容 |
|------|------|
| [WORKSPACE_EVENTS_IMPLEMENTATION.md](./WORKSPACE_EVENTS_IMPLEMENTATION.md) | 事件源实现与 webhook 反馈闭环运维 |
| [TUNNELS_SETUP.md](./TUNNELS_SETUP.md) | webhook 公网隧道搭建（cloudflared） |
| [`../scripts/README.md`](../scripts/README.md) | 仓库脚本总览（dev / examples / soft-copyright / tunnels） |
| [`../tests/README.md`](../tests/README.md) | pytest 布局与标记（默认 1630 / extended 376 / 全量 2006） |
| [`../gomatrix/README.md`](../gomatrix/README.md) | GoMatrix homeserver：构建、部署、配置、侧信道 |

### 工程台账

| 文档 | 内容 |
|------|------|
| [REMAINING_ISSUES.md](./REMAINING_ISSUES.md) | 剩余问题清单（唯一在册台账）：P0 决策项 1 + P1 问题 7 + 待评估 3 + P3 观察 12（P2 已于 08-20 全部出册） |
| [PERFORMANCE_REVIEW.md](./archive/PERFORMANCE_REVIEW.md) | **已归档调研快照**（2026-08，行号已漂移）：热路径优化点分级清单 |
| [templates/](./templates/) | coara Home `system/.gitignore` 模板；**用户默认配置**见 [`../deploy/gitee/templates/`](../deploy/gitee/templates/) |

---

## 快速结论（多工作空间 / 事件）

1. **两类目录**：用户工作空间目录（源码） vs coara Home（运行时数据）。
2. **CLI cwd** = 当前工作空间；**name** = registry 身份。
3. 外部事件无条件写入工作空间动态（存储在 `<workspace>/.coara/inbox/`，空间自治布局），默认 `handle: park` 挂住等用户。
4. 手机 Matrix 绑定 `{coara_home}/runtime/active.json`。
5. 定时执行走 **cron 事件源**（`kind: cron` 到点发事件，可直启工作流 trigger 或 `handle: janitor` 让管家处置；见 `docs/工作空间与事项制度.md`）。

---

## 非 Canonical

| 路径 | 说明 |
|------|------|
| [`../ref-doc/`](../ref-doc/) | 外部调研、个人笔记 |
| [`../competition/`](../competition/) | 比赛/商业素材 |
| [`../软著申请材料/`](../软著申请材料/) | 软著产出 |
| `skills/*/SKILL.md` | 运行时技能（LLM 可见） |
| `src/tools/builtin/**/*.py` | 工具类 `description`（LLM 可见） |
| `android-app/docs/` | Android 端专题（如 Markdown 渲染架构） |

---

## 路径速查

```text
{coara_home}/registry/workspaces.yaml
{coara_home}/users/default/assets/vault/     # 加密宝箱
{coara_home}/workspaces/{workspace_id}/      # trace、log、subagents…
{coara_home}/runtime/active.json
{workspace}/.coara/inbox/                    # 工作空间动态（空间自治布局）
{workspace}/.coara/matters/definitions/      # 事件源定义（空间自治布局）
{coara_home}/users/default/workflows/        # 草案、触发器、实例 DB
{coara_home}/system/                         # .env、config.yaml、providers.yaml
```

工作空间源码在 registry `path` 指向的目录，不在 `workspaces/` 下。
