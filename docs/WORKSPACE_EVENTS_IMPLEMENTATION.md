# 外部事件源 — 实现说明与运维手册

> **范围**：`src/event_sources/` 的实现细节 + webhook 反馈闭环的运维步骤。
> 概念层（多工作空间、动态、Root/janitor）以 [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md) 为准；存储布局见 [STORAGE_AND_WORKSPACES.md](./STORAGE_AND_WORKSPACES.md)。

**一句话**：事件源把「外部世界发生的变化」（文件落盘 / 定时扫描发现 / HTTP POST / cron 到点）规范化为统一事件；事件内容**无条件**落所属工作空间的动态收件箱，再按 `salience`（显著性）与 `handle`（park / janitor）决定曝光与处置。

**智能体配置**：与 workflow 相同——技能 `event-source` + 规格 [`../skills/event-source/EVENT_SOURCE_SPEC.md`](../skills/event-source/EVENT_SOURCE_SPEC.md) 指导设计；CRUD 用工具 `event_source`（list/toggle/remove 可直接调）。本文是实现与运维手册。

---

## 目录

1. [总览](#1-总览)
2. [四种事件源](#2-四种事件源)
3. [配置字段](#3-配置字段)
4. [事件流水线与 salience / handle](#4-事件流水线与-salience--handle)
5. [去重与状态](#5-去重与状态)
6. [webhook 接收服务](#6-webhook-接收服务)
7. [与 janitor / Workflow 的衔接](#7-与-janitor--workflow-的衔接)
8. [操作命令](#8-操作命令)
9. [运维手册：App 反馈 webhook 闭环](#9-运维手册app-反馈-webhook-闭环)
10. [文件索引](#10-文件索引)

---

## 1. 总览

```text
file_watch / interval_poll / webhook / cron
        ↓ InboundEvent
EventSourceManager._handle_event
        ↓ 去重（cooldown + 路径冷却）
        ├─→ workflow trigger 注册表（事件直启 WDL）
        ├─→ 无条件落工作空间动态收件箱（<workspace>/.coara/inbox/）
        │     salience=high → 跨空间前台待处理视图
        └─→ handle=janitor → 叫醒管家 janitor 过目（勾掉/呈阅/自处理，轨迹写回消息）
```

关键约定：

- 事件源定义的**唯一加载目录**是各空间 `<workspace>/.coara/matters/definitions/*.yaml`（`EventSourceManager.event_source_dirs()` 遍历 registry 全部空间）；同 id 重复定义时先加载的生效
- 事件源的 `workspace` 必须已在 registry 登记（`coara ws add <path> --name <名>`），否则启动时跳过并告警
- `file_watch` / `interval_poll` 的 `watch_path` 相对该工作空间根解析（内部经 VFS `@<workspace>/<rel>`），不存在时自动创建

## 2. 四种事件源

| kind | 机制 | 事件类型 | 典型用途 |
|------|------|----------|----------|
| `file_watch` | watchdog 监听目录（非递归，0.5s 防抖） | `file.created` / `file.modified` | 反馈 JSON 落入 `inbox/`、构建产物落盘 |
| `interval_poll` | asyncio 定时比对目录文件名集合 | `poll.new_files` | 文件事件不可靠的环境、兜底 |
| `webhook` | 内嵌 aiohttp HTTP 服务接收 POST | 由请求体 `event_type` 决定（默认 `webhook.received`） | App / 后端主动推送 |
| `cron` | cron 表达式到点触发（Asia/Shanghai） | `cron.tick` | 定时巡检 / 定时跑工作流（停机错过不补发） |

实现要点：

- **file_watch**（`sources/file_watch.py`）：`watch_pattern` 按文件名 fnmatch 过滤（默认 `*`）；`watch_events` 决定监听 `created` 还是 `modified`；同一文件 0.5s 内的连续变更只发一次（代次计数防抖）
- **interval_poll**（`sources/poll.py`）：`interval_seconds`（默认 60）扫一次，新文件数 ≥ `poll_min_count`（默认 1）才发一条批量事件；已见文件名快照持久化，重启不重复
- **webhook**：详见 §6

## 3. 配置字段

`EventSourceDefinition`（`src/event_sources/types.py`），YAML 示例见仓库 `events/*.yaml.example`：

| 字段 | 默认 | 说明 |
|------|------|------|
| `id` | 必填 | 事件源 id（webhook URL、去重状态、cron trigger 绑定都用它） |
| `enabled` | `true` | 停用的定义加载但不启动 |
| `kind` | 必填 | `file_watch` / `interval_poll` / `webhook` / `cron` |
| `workspace` | 必填 | 归属工作空间名（须在 registry 登记） |
| `watch_path` | — | file/poll 监看的目录（相对工作空间根） |
| `watch_pattern` | `*` | 文件名过滤 |
| `watch_events` | `[created]` | `created` / `modified` |
| `interval_seconds` | — | poll 间隔（缺省 60s） |
| `poll_min_count` | `1` | poll 最少新文件数才触发 |
| `cron` | — | 5 字段 cron 表达式（`kind: cron` 必填，Asia/Shanghai） |
| `webhook_secret` | — | webhook 鉴权令牌；不设置则不鉴权（见 §6 安全提示） |
| `salience` | `normal` | 显著性 `low` / `normal` / `high`；`high` 的事件内容进跨空间前台待处理视图（见 §4） |
| `handle` | `park` | 处理模式 `park`（挂住等用户）/ `janitor`（叫醒管家 janitor 过目），见 §4 |
| `cooldown_seconds` | `30` | 去重冷却窗口 |
| `ttl_seconds` | — | 消息保质期（秒）：落箱超时未读 → 纯规则过目自动勾掉（dismissed，留轨迹） |
| `routing_domain` / `suggested_delegate` | — | 路由提示，随事件写入动态，供 Root 决策 |
| `message_template` | 内置模板 | 事件文本模板，占位符 `{{source_id}}` `{{workspace}}` `{{event_type}}` `{{details}}` |

渲染出的文本统一用 `<事件提醒>` 标签包裹（`formatter.render_inbound_message`）。

> **旧 `trigger_mode` 字段已废弃并被忽略**（`src/event_sources/types.py` 只做未知 `handle` 归一为 `park`，不再迁移 `trigger_mode`）。新配置请直接写 `salience` / `handle`。

## 4. 事件流水线与 salience / handle

每个事件先去重（§5），然后**无条件**落收件箱与做衔接（§7），最后按 `handle` 决定是否叫醒 janitor：

| 机制 | 行为 | Root 被打断？ |
|------|------|----------------|
| 落收件箱（无条件） | 追加到工作空间动态（`<workspace>/.coara/inbox/`），推送 Matrix 红点；类型为 `webhook`、`file_change` 或 `note`（cron）；`source_kind=external_sensor` | 否。用户经 `/ws updates` 或 Web UI 查看后决定处理 |
| `salience: high` | 该条内容同时进跨空间「前台待处理视图」（`review(action=pending)` / `/ws updates pending`） | 否。只是曝光上浮，仍不自动跑回合 |
| `ttl_seconds` | 消息带保质期（`expires_at`），过期未读被纯规则过目自动 dismissed（`store.sweep`，janitor watcher 每拍执行，不经 LLM） | 否 |
| `handle: janitor` | 叫醒管家 janitor 过目（`dispatch_janitor_message_review`，`src/coara/workspace_protocol.py`）：系统派发，三选一——`review(dismiss)` 勾掉 / `review(elevate)` 呈阅 / `review(resolve)` 自处理，理由写进消息处置轨迹 | 否。janitor 后台执行，结果只落轨迹不注入主会话 |

旧 `trigger_mode` 的 `auto_run` / `queue_if_busy` / `inject_only`（注入 Root 对话/自动开回合）已整体废弃并被忽略：消息内容体系下不再有「事件自动叫醒主会话跑回合」，处置缺省 `park`。

## 5. 去重与状态

状态目录：`<coara_home>/users/default/matters/.state/`（每源一个 `{id}.json`，路径冷却单独存 `__path_cooldown__.json`）。

- **去重键**：文件事件 = `path:{ws}:{绝对路径}`（多文件批量 = 排序后哈希）；webhook = 优先 `feedback_id`，其次请求体 `id`，否则整体内容哈希
- **冷却**：同一源在 `cooldown_seconds` 内只发一条；同一文件路径跨源也有独立冷却（防止 watch + poll 双发）
- **并发安全**：check-and-mark 全程持锁（`EventSourceStateStore.locked()`），重复事件不会双双通过
- **上限**：seen_keys 保留最近 500 条；poll 文件名快照保留 1000 条；路径冷却表 2000 条（超出裁到 1500）

## 6. webhook 接收服务

`WebhookIngressServer`（`src/event_sources/sources/webhook_server.py`）是所有 webhook 源共享的一个 aiohttp 服务：

- **监听地址**：`config.yaml` 的 `events.webhook_host` / `events.webhook_port`，默认 `127.0.0.1:8765`
- **路由**：`POST /webhook/{source_id}`（每个启用的 webhook 源一条）；`GET /health` 返回已注册源列表
- **鉴权**：源配置了 `webhook_secret` 时，请求须带 `Authorization: Bearer <secret>` 或 `X-Coara-Webhook-Token: <secret>`（hmac 比较）；不匹配返回 401
- **请求体**：必须是 JSON object，≤ 2MB（超出 413）；整体作为事件 payload
- **事件类型**：取请求体 `event_type` 字段，缺省 `webhook.received`
- **启动日志**：coara 启动时打印 `Webhook server listening on http://127.0.0.1:8765` 和每个源的完整接收地址

> **安全提示**：不设置 `webhook_secret` 的 webhook 源没有任何鉴权。经隧道暴露公网时务必配置。

### 6.1 端用户 `/report` → 开发者本机

端用户电脑上的 `/report` **不会**在本地创建「用户反馈」工作空间，而是把会话打包后 `POST` 到**软件内置的开发者接收地址**（[`src/coara/report_defaults.py`](../src/coara/report_defaults.py)，发版前由开发者填入稳定 named-tunnel URL）。用户无需也不应去改 `config.yaml`。

本地调试可用环境变量 `COARA_REPORT_WEBHOOK_URL` / `COARA_REPORT_WEBHOOK_SECRET` 临时覆盖；未配置时 `/report` 提示「报告通道尚未就绪」。

**开发者机接收侧：**

1. `coara ws add <path> --name 用户反馈`
2. 拷贝 [`events/coara-user-report-webhook.yaml.example`](../events/coara-user-report-webhook.yaml.example) → `<workspace>/.coara/matters/definitions/coara-user-report.yaml`
3. 用 **named** Cloudflare 隧道暴露 `events.webhook_port`（quick 隧道域名会变，不适合写进软件）
4. 把公网 URL 写入 `report_defaults.py` 后再发版

注意：GoMatrix 的 Matrix 隧道（手机聊天）与这条 webhook 隧道是**两条线**，重启 gomatrix 不会自动带上 `/report` 接收。

## 7. 与 janitor / Workflow 的衔接

事件通过去重后、落收件箱**之前**，先做工作流 trigger 自动启动：

1. **Workflow trigger 自动启动**：`RootCoara._on_workflow_trigger(event_id, payload)` 查 trigger 注册表，命中的 WDL 直接提交引擎执行（事件 payload 作为 inputs）。注册机制见 [工作空间与事项制度.md](./工作空间与事项制度.md) §4。cron 源到点的 `cron.tick` 同样可命中 trigger 直启定时工作流。

落箱之后按 `handle` 处置（§4）：`park` 挂住等你批示；`janitor` 叫醒管家过目。这三条（trigger 直启 / 落箱曝光 / janitor 过目）**并行生效**，互不排斥。

概念与设计见 [工作空间与事项制度.md](./工作空间与事项制度.md)。

## 8. 操作命令

```bash
# 工作空间登记（事件源的前置条件）
coara ws list
coara ws add <path> --name <名> --summary "一句话"
coara ws default <名>
coara ws remove <名>
coara --workspace-alias <名>     # 启动时指定活动工作空间
```

聊天内：

```text
/events                # 列出事件源：id、kind、工作空间、运行状态、salience/handle、webhook 接收地址
/events reload         # 热替换重载：file_watch/poll 先起新源再停旧源（无监听空窗，双开期重复事件由 dedupe 吸收）；webhook 端口独占退化为 stop→start
/ws updates list @<名> # 查看工作空间动态
```

改 YAML 后用 `/events reload` 或重启 coara 生效。

## 9. 运维手册：App 反馈 webhook 闭环

场景：手机 App → 云端后端 → POST 到本机 coara 的 webhook → 写入 `暄` 工作空间动态（或叫醒 janitor 处置）。

### 9.1 一次性配置

```powershell
# 1. coara Home（按需）
setx COARA_HOME "D:\coara"

# 2. 登记工作空间
coara ws add D:\code_ws\xuan_android_local --name 暄 --summary "暄 App"

# 3. 放置事件源定义（注意：唯一加载目录是 <workspace>/.coara/matters/definitions/）
copy events\xuan-feedback-webhook.yaml.example D:\code_ws\xuan_android_local\.coara\matters\definitions\xuan-feedback-webhook.yaml
# 编辑：enabled: true；webhook_secret 改成强随机串

# 4. （后端不在本机时）开公网隧道，见 TUNNELS_SETUP.md
scripts\tunnels\Start-AllTunnels.ps1 -SkipCoara
```

> **注意**：`Start-AllTunnels.ps1` 的 `Ensure-WebhookEventSourceEnabled` 把示例复制到**传入工作空间目录**的 `.coara/matters/definitions/`（脚本传 `COARA_REPO` 指向的工作空间）；若定义指向别的空间（如 `workspace: 暄`），请按上一步手动放到该空间目录下。

### 9.2 后端配置

云端后端 `.env`：

```env
COARA_WEBHOOK_URL=http://127.0.0.1:8765/webhook/xuan-feedback-webhook      # 后端与 coara 同机
# 或 https://<隧道域名>/webhook/xuan-feedback-webhook                        # 后端在云端（经隧道）
COARA_WEBHOOK_TOKEN=<与 yaml 中 webhook_secret 相同>
```

未配置 `COARA_WEBHOOK_URL` 时后端不调用 webhook，互不影响。

### 9.3 事件到达后

默认模板源 `handle: park`：事件写入 `暄` 工作空间动态，Root 不自动开回合。用户查看动态后说「处理一下」，或由 Root 按 `suggested_delegate: coaras` 提示 `delegate(coaras, workspace="暄")`。

要事件**自动**触发处理，二选一：

- 把源的 `handle` 改为 `janitor`（事件落箱后叫醒管家 janitor 过目，勾掉/呈阅/自处理写轨迹）
- 或配工作流 trigger：把 WDL 草案绑定该事件源（`schedule.kind: event` + `event_id`），事件命中直启工作流（不经 janitor）

### 9.4 file_watch / poll 备用通道

`events/xuan-feedback.yaml.example`（file_watch）和 `xuan-feedback-poll.yaml.example`（interval_poll）监听工作空间内 `feedback/inbox/*.json`，适合「反馈先落盘」的部署形态。用 webhook 推送时这套可以不启用。

### 9.5 模拟一条反馈（联调）

```powershell
curl -X POST http://127.0.0.1:8765/webhook/xuan-feedback-webhook `
  -H "Authorization: Bearer <webhook_secret>" -H "Content-Type: application/json" `
  -d '{"id":"test-001","event_type":"feedback.created","title":"测试反馈","description":"联调"}'
```

返回 `{"ok": true, "source_id": "xuan-feedback-webhook"}` 即接收成功；随后看 `/events` 状态和工作空间动态。

## 10. 文件索引

```text
src/event_sources/
├── types.py                 # EventSourceDefinition / InboundEvent / HandleMode（未知 handle 归一为 park）
├── manager.py               # EventSourceManager：加载、启动、分流
├── dedupe.py                # 路径去重键
├── formatter.py             # message_template 渲染 + <事件提醒> 包裹
├── state.py                 # 去重/冷却/poll 水位持久化（matters/.state/）
└── sources/
    ├── file_watch.py        # watchdog 文件监听（0.5s 防抖）
    ├── poll.py              # 定时轮询
    ├── webhook_server.py    # aiohttp webhook 接收（/webhook/{id}、/health）
    └── cron.py              # cron 定时事件源（Asia/Shanghai，按分钟去重）

相关：
src/coara/workspace_protocol.py   # dispatch_janitor_message_review（handle=janitor 派发）
src/coara/root.py                 # _on_janitor_dispatch 接线
src/workspace/updates/store.py    # 落箱、处置轨迹、纯规则过目（sweep）
events/*.yaml.example             # 事件源模板（复制到 <workspace>/.coara/matters/definitions/ 使用）
```

测试：`pytest tests/test_event_sources/ -q`
