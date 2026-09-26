---
name: event-source
description: "为已登记工作空间新建或改设计事件源（目录监听 / 定时扫描 / webhook / cron）。仅 list、开关、删除、reload 时直接用 event_source 工具即可"
references:
  - EVENT_SOURCE_SPEC
---

# 事件源配置

把「外部一变就通知某工作空间」配成可热重载的 YAML 定义。

## 必读

激活后先 `read` 同目录 `EVENT_SOURCE_SPEC.md`（字段与分流的权威源）。本文件只写流程与选型，不重复字段表。

## 何时用

- 某目录有新文件就进动态
- 给 App / 后端一个 webhook 推进某空间
- 改已有源的监听路径、模板、`salience` / `handle`
- 定时巡检（`cron`）

## 何时不用

- 只查看或开关已有源 → `event_source(action=list|toggle|remove|reload)`
- 端用户 `/report` → 软件内置通道，不要在用户机再建「报告」事件源
- 多步编排 → 挂起工具 `orchestrator` / WDL；事件源只负责把变化送进来
- 动态处置 → `/ws updates dismiss|elevate|resolve`

## 与 `event_source` 工具

| 动作 | 做法 |
|------|------|
| 列表 / 开关 / 删除 / 热重载 | 直接调工具 |
| 新建或改设计 | 先本技能 + 读 SPEC，再 `add` / `update` |

工具可能在挂起池：先 `tool(action="activate", name="event_source")`。写操作会走审批。

## 流程

1. `event_source(action="list")` — 避免重复 id
2. `ws(action="list")` — 确认目标空间已登记；没有则先 `ws(add)`
3. `read EVENT_SOURCE_SPEC.md`
4. 选型（下表），写出字段意图
5. 同轮 `event_source(action="add"|update, …)`
6. 需要时再 `list`，或让用户查 webhook 健康检查

必须发出工具调用，不要只口头说「下面会配」。

## 选型

| 用户说法 | kind | 配置要点 |
|----------|------|----------|
| 文件夹一丢文件就知道 | `file_watch` | 默认 `park` + `normal` |
| 监听不稳 / 网络盘 | `interval_poll` | 同上 |
| 手机 App / 后端 POST | `webhook` | 同上；公网须 `webhook_secret` |
| 定时巡检 / 定时跑工作流 | `cron` | cron 表达式 + 模板写清要干什么 |
| 需要尽快被看见 | 任一 | `salience: high` |
| 低价值、不怕漏 | 任一 | `salience: low` 或 `ttl_seconds` |

默认：`handle: park` + `salience: normal`（入库 + 红点，等用户处置）。

## 自由度

- 固定：四种 `kind`、定义字段表、外壳与 `salience` / `handle`（见 SPEC）
- 自由：payload 形状、`message_template`、与工作流 trigger 的衔接

## 常见陷阱

- 空间名未登记 → 热重载成功但源被跳过
- webhook 公网暴露却不设 secret
- 把聊天隧道当成 webhook 隧道
- 未读 SPEC 就猜字段名

## 验证

1. 对照 SPEC 自检
2. `event_source(action="list")` 能看到且为开
3. file/poll：往 `watch_path` 丢匹配文件，看对应空间动态
4. webhook：对本机 `/webhook/{id}` 带 Bearer 发最小 JSON，期望 200
