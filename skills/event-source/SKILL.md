---
name: event-source
description: "事件源配置技能。使用场景：需要为已登记工作空间**新建或改设计**外部事件触发（目录监听 / 定时扫描 / webhook）。不用于：仅 list / 开关 / 删除 / reload（直接用 event_source 工具）；端用户 /report（开发者内置通道，与本机事件源无关）"
references:
  - EVENT_SOURCE_SPEC
---

# 事件源配置技能

你现在作为事件源配置助手运行：把「外部世界一变就通知某工作空间」配成可热重载的 YAML 定义。

## 必读：规格

**激活本技能后，第一件事是 read `EVENT_SOURCE_SPEC.md`（与本 SKILL.md 同目录）**

`EVENT_SOURCE_SPEC.md` 是字段与分流约定的权威源，涵盖

- 四种 `kind` 与按种必填字段
- 统一外壳 `InboundEvent`（payload **不**要求长一样）
- `salience` / `handle` 与并行衔接（事项 / 工作流 trigger）
- `message_template` 占位符
- 校验与常见错误

写配置前必须读规格——本 SKILL.md 只给流程与选型，不重复字段表。

## 何时使用

- 用户要「某目录有新文件就进动态 / 提醒」
- 用户要「给某 App 一个 webhook 推进某空间」
- 用户要改已有源的监听路径、模板、显著性/处置方式

## 不适用

- 只查看或开关已有源 → 直接 `event_source(action=list|toggle|remove|reload)`，无需本技能
- 端用户 `/report` 故障上报 → 走软件内置开发者 webhook，**不要**在用户机上为「报告」再建事件源
- 复杂多步编排 → 用 **workflow** 技能 + WDL；事件源只负责把变化送进来
- 工作空间消息处置 → janitor 专属 `review` 工具；事件源最多用 `handle: janitor` 去**叫醒管家**过目

## 与工具的分工（对标 workflow）

| 动作 | 做法 |
|------|------|
| 列表 / 开关 / 删除 / 热重载 | **直接** `event_source`，不必激活技能 |
| 新建或改设计（kind、路径、模板、mode） | **先**本技能 + 读 SPEC，再 `event_source(action=add|update)` |

`event_source` 可能是 deferred：若当前可见工具里没有它，先 `tool(action="activate", name="event_source")`。

写操作（add/update/toggle/remove）会走人工审批。

## 完整生命周期

1. `event_source(action="list")` — 避免重复 id
2. `ws(action="list")` — 确认目标空间已登记；没有则先 `ws(action="add", …)` 再配事件源
3. **read `EVENT_SOURCE_SPEC.md`**
4. 选型（见下节），写出字段意图
5. 同一轮调用 `event_source(action="add", …)` 或 `update`
6. 需要时 `event_source(action="list")` 或让用户查 `GET /health`（webhook）确认已挂上

不要只口头说「下面会配」就停；必须发出工具调用。

## 选型速查

| 用户说法 | 优先 kind | 推荐配置 |
|----------|-----------|----------|
| 文件夹一丢文件就知道 | `file_watch` | 默认（park + normal） |
| 监听不稳 / 网络盘 | `interval_poll` | 默认（park + normal） |
| 手机 App / 后端 POST | `webhook` | 默认（park + normal） |
| 定时巡检 / 定时跑工作流 | `cron` | 配 `cron` 表达式 + `message_template` 写明要干什么 |
| 需要尽快被看见 | 任一 + `salience: high`（进前台待处理视图） | — |
| 低价值通知（不怕漏） | 任一 + `salience: low` 或 `ttl_seconds`（到期自动勾掉） | — |
| 来了就后台自动巡检 | 任一 + `handle: janitor`（叫醒管家过目处置） | — |

**默认倾向 `park` + `salience: normal`**：入库 + 红点，先报告用户，同意后再 delegate。消息内容体系下事件不会再自动叫醒主会话跑回合（旧 `trigger_mode` 已废弃并被忽略，处置缺省 `handle: park`）。

## 自由度在哪

- **固定**：三种接入、定义字段表、外壳与分流枚举（见 SPEC）
- **自由**：payload 业务形状、`message_template` 文案、`routing_domain` / 与事项或 WDL 的衔接方式

不同事件本来就可以长得不一样；用 `event_type` + 模板说明白即可。

## 常见陷阱

- 空间名写错或不在 registry → 热重载成功但源被跳过
- webhook 公网暴露却不设 `webhook_secret`
- 把 GoMatrix / 聊天隧道当成 webhook 隧道（端口与进程都不同）
- 为 `/report` 在用户电脑复制「用户反馈」事件源（应只在开发者机接收）
- 未读 SPEC 就猜字段名

## 验证

1. 对照 SPEC §7 自检
2. `event_source(action="list")` 能看到新源且为开
3. file/poll：往 `watch_path` 丢一个匹配文件，看对应空间动态
4. webhook：对本机 `/webhook/{id}` 带 Bearer 发一条最小 JSON，期望 200
