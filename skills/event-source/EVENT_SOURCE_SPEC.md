# 事件源规格（EVENT_SOURCE_SPEC）

> **权威单一源**：事件源 YAML 字段、接入种类、`salience` / `handle`、模板占位符与校验约定。
> 运维细节见仓库 `docs/WORKSPACE_EVENTS_IMPLEMENTATION.md`；本文件面向智能体写配置。

事件源**不是**第二套工作流语言。它只把外部变化与定时点收成统一外壳，事件内容无条件落所属空间的动态收件箱，再由 `salience` / `handle` 决定曝光与处置。业务载荷（payload）可以长得很不一样——用模板与约定表达，不要发明新节点类型。

---

## 1. 磁盘位置

- 定义目录：`<workspace>/.coara/matters/definitions/*.yaml`（空间自治布局；经 registry 解析，旧集中布局 `users/default/matters/definitions/` 启动时迁移）
- 每文件一个源；`id` 全局唯一；同 id 先加载的生效
- `workspace` 必须是登记表里已有的工作空间名（`ws list` 可见），否则加载时跳过

用 `event_source` 工具写入即可，一般不必手写路径。

---

## 2. 四种 kind（接入方式）

| kind | 机制 | 典型 `event_type` | 何时用 |
|------|------|-------------------|--------|
| `file_watch` | 监听目录（非递归，短防抖） | `file.created` / `file.modified` | 本地目录落文件即可触发 |
| `interval_poll` | 定时扫目录文件名集合 | `poll.new_files` | 文件事件不可靠时的兜底 |
| `webhook` | `POST /webhook/{id}` JSON | 请求体 `event_type`，缺省 `webhook.received` | App / 后端 / 远程主动推 |
| `cron` | cron 表达式到点触发（Asia/Shanghai） | `cron.tick` | 定时巡检 / 定时跑工作流 |

每种 kind 的**配置字段**不同；**进入系统后的外壳**相同（见 §4）。cron 源在进程停机期间错过的触发**不补发**，重启后从当前时间重算下一次。

---

## 3. 定义字段（EventSourceDefinition）

### 3.1 通用

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `id` | 是 | — | 英文短 id；webhook URL 与去重状态都用它 |
| `kind` | 是 | — | `file_watch` / `interval_poll` / `webhook` / `cron` |
| `workspace` | 是 | — | 归属工作空间名 |
| `enabled` | 否 | `true` | `false` 时加载但不启动 |
| `salience` | 否 | `normal` | 显著性 `low` / `normal` / `high`，见 §5 |
| `handle` | 否 | `park` | 处理模式 `park` / `janitor`，见 §5 |
| `priority` | 否 | `normal` | 已废弃 |
| `cooldown_seconds` | 否 | `30` | 同源冷却窗口 |
| `ttl_seconds` | 否 | — | 消息保质期（秒）：落箱超时未读 → 纯规则过目自动勾掉（dismissed，留轨迹） |
| `routing_domain` | 否 | — | 路由提示，随事件进动态 |
| `suggested_delegate` | 否 | — | 建议子智能体类型（如 `coaras`），仅提示 |
| `message_template` | 否 | 内置默认 | 见 §6 |
| （其他历史字段） | 否 | — | 加载旧 YAML 时忽略或映射为现行 `handle` / `salience` |

### 3.2 按 kind

**`cron`**

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `cron` | 是 | — | 5 字段 cron 表达式（分 时 日 月 星期），按 Asia/Shanghai 计算 |

到点产生 `cron.tick` 事件，payload 含 `cron` / `fired_at`；同一分钟只发一次（reload 双开窗口由去重吸收）。定时跑工作流：cron 源 + 工作流 trigger 绑定（trigger 注册表按事件源 id 命中即启）。

**`file_watch` / `interval_poll`**

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `watch_path` | 是 | — | 相对工作空间根的目录；不存在会自动创建 |
| `watch_pattern` | 否 | `*` | 文件名 fnmatch |
| `watch_events` | 否 | `[created]` | `created` / `modified`（主要 file_watch） |
| `interval_seconds` | poll 建议 | `60` | 仅 `interval_poll` |
| `poll_min_count` | 否 | `1` | 新文件数达此值才发一条 |

**`webhook`**

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `webhook_secret` | 强烈建议 | — | 鉴权令牌；公网隧道暴露时必须设 |
| （无 watch_*） | — | — | 不要填 `watch_path` |

Webhook HTTP

- 本机：`POST http://127.0.0.1:<events.webhook_port>/webhook/{id}`（默认端口 `8765`）
- 鉴权：`Authorization: Bearer <secret>` 或 `X-Coara-Webhook-Token: <secret>`
- Body：JSON object，≤ 2MB；整包进入 `payload`
- `event_type`：取 body 字段，缺省 `webhook.received`

---

## 4. 统一外壳 InboundEvent（载荷仍自由）

规范化后每条事件是：

| 字段 | 含义 |
|------|------|
| `source_id` | 事件源 id |
| `workspace` | 归属空间名 |
| `event_type` | 字符串类型名 |
| `payload` | **自由 dict**（文件路径、远程 JSON、业务字段……） |
| `dedupe_key` | 去重键 |
| `occurred_at` | ISO 时间 |

`{{details}}` = 把 `payload` 逐键摊成 `key: value` 文本。不同业务事件**可以、也应该**长得不一样；用 `event_type` + payload 约定区分，不要强行同一套字段。

与「工作空间动态」卡片的关系：事件内容**无条件**写入 inbox；`title` 常从 payload 的 `content`/`title`/`summary`/`subject` 截取。信封另带 `salience` / `handle_mode` / `source_kind`（事件源恒为 `external_sensor`）。

---

## 5. salience 与 handle（曝光与处置）

| 字段 | 值 | 行为 | 是否打断 Root 对话 |
|------|----|------|-------------------|
| `handle` | `park`（默认） | 内容挂在工作空间动态里等用户（红点提醒） | 否 |
| `handle` | `janitor` | 叫醒管家 janitor 过目（`dispatch_janitor_message_review`）：勾掉 / 呈阅 / 自处理，理由写进处置轨迹 | 否 |
| `ttl_seconds` | — | 消息保质期：过期未读被纯规则过目自动 dismissed（留轨迹） | 否 |
| `salience` | `low` / `normal`（默认） | 仅收件箱红点 | 否 |
| `salience` | `high` | 上浮跨空间「前台待处理视图」（`review(action=pending)`） | 否 |

> 旧 `trigger_mode` 已废弃并被忽略，不再有「事件自动叫醒主会话跑回合」。新配置只写 `salience` / `handle`（可配 `ttl_seconds`），`handle` 缺省 `park`。

**并行衔接**（与落箱/处置同时发生，不是互斥）

1. 若工作流 trigger 注册表命中该事件 → 可直启 WDL
2. `handle: janitor` 与 trigger 直启互不排斥——事件既落箱叫醒管家，也按 trigger 启工作流

默认优先 `park` + `salience: normal`：先入库，让用户决定要不要 delegate。

---

## 6. message_template

占位符（仅此四个会替换）

- `{{source_id}}`
- `{{workspace}}`
- `{{event_type}}`
- `{{details}}`

渲染结果会包在事件提醒标签里。模板应使用**用户语言**，写清：发生了什么、建议用户先看什么、是否要等用户同意再动手。

默认模板大意：报告事件 → 先向用户报告 → 未同意前不要立刻 delegate。

---

## 7. 校验与常见错误

1. `workspace` 未登记 → 源被跳过（健康检查里看不到）
2. `file_watch`/`interval_poll` 缺 `watch_path`
3. webhook 无 `webhook_secret` 却暴露公网
4. `id` 含空格或与已有源冲突
5. 把端用户 `/report`（开发者内置接收地址）当成「在用户机上再配一个反馈事件源」——二者无关
6. 期望「所有事件同一 JSON 形状」——规格不要求；按业务写 payload 约定即可

---

## 8. 最小示例

### 目录落盘 → 动态

```yaml
id: app-inbox-watch
enabled: true
kind: file_watch
workspace: my-app
watch_path: feedback/inbox
watch_pattern: "*.json"
watch_events: [created]
salience: normal
handle: park
cooldown_seconds: 30
routing_domain: app-maintenance
suggested_delegate: coaras
message_template: |
  [事件 — {{source_id}}]
  workspace: @{{workspace}}
  event_type: {{event_type}}
  {{details}}
  请先向用户报告；用户同意后再处理。
```

### Webhook → 动态

```yaml
id: app-hooks
enabled: true
kind: webhook
workspace: my-app
webhook_secret: "换成足够长的随机串"
salience: normal
handle: park
cooldown_seconds: 5
message_template: |
  [Webhook — {{source_id}}]
  {{details}}
```

仓库根 `events/*.yaml.example` 有更多拷贝模板（含反馈类示例）。
