# coara App 与本地 PC 通信链路

> **Canonical**：Android coara App 如何通过 Matrix/GoMatrix 与 PC 端 coara 通信——架构、消息流、侧信道协议、可靠性语义。
> GoMatrix 部署见 [`../gomatrix/README.md`](../gomatrix/README.md)；工作空间动态模型见 [`工作空间动态模型.md`](./工作空间动态模型.md)。

## 目录

- [1. 一句话模型](#1-一句话模型)
- [2. 角色与账号](#2-角色与账号)
- [3. 连接流程](#3-连接流程)
- [4. 普通聊天消息流](#4-普通聊天消息流)
- [5. 侧信道协议](#5-侧信道协议)
- [6. 工作空间绑定](#6-工作空间绑定)
- [6.5 REST 代理（/coara-api）](#65-rest-代理coara-api)
- [7. 信任与安全](#7-信任与安全)
- [8. 后台与通知](#8-后台与通知)
- [9. 文件桥](#9-文件桥)
- [10. 可靠性语义（/sync 同步屏障）](#10-可靠性语义sync-同步屏障)
- [11. 源码索引](#11-源码索引)
- [12. 常见误区](#12-常见误区)

---

## 1. 一句话模型

**Android coara App 和 PC 端 coara 都是 Matrix 客户端，它们通过同一个 Matrix room（房间）交换消息；GoMatrix 是运行在 PC 上的轻量级 homeserver（Matrix 服务器），提供账号、房间、同步等基础设施。**

> 术语：**Matrix** 是一种开放即时通信协议；**homeserver** 是 Matrix 的服务器，负责存账号、存消息、转发同步；**MXID** 是 Matrix 用户 ID，形如 `@名字:服务器名`。

```text
┌─────────────────┐      Matrix CS API       ┌─────────────────┐
│  coara App      │  ◄────(HTTP /sync)────►  │  GoMatrix       │
│  (Android)      │      register/login       │  (PC 桌面应用)  │
│  普通用户账号    │      send/receive         │  0.0.0.0:8008   │
└────────┬────────┘                            └────────┬────────┘
         │                                              │
         │    同一个 Matrix room（通常是 1:1 直聊房）    │
         └──────────────────┬───────────────────────────┘
                            │
                    ┌───────┴───────┐
                    │  coara (PC)   │
                    │  @coara:...   │
                    │  Python 运行时 │
                    └───────────────┘
```

---

## 2. 角色与账号

| 角色 | 典型 MXID | 说明 | 代码位置 |
|------|-----------|------|----------|
| **coara (PC)** | `@coara:coara.local` | Python 运行时的 Matrix bot 账号，处理用户消息并回复 | `src/matrix_client/bot.py` |
| **手机用户** | `@phone:coara.local` | App 的普通 Matrix 用户（**不是 agent**）。`gomatrix.toml` 的 `[pairing]` 配置固定账号后，配对码直接携带凭据，重装/换机扫码都回到同一账号（房间与历史保留）；未配置时退回随机 `phone_xxxxxxxx` 注册 | `gomatrix/internal/service/pairing.go` |
| **GoMatrix** | — | homeserver，管理账号、房间、媒体、同步 | `gomatrix/` |

GoMatrix 启动时按 `gomatrix.toml` 的 `[[agents]]` 自动注册 agent 账号；配置 **2 个及以上** agent 时还会创建 `#agents:<server_name>` 共享房间（`gomatrix/internal/service/agents.go`）。
App 登录后通过 `GET /api/agents`（免登录）发现有哪些 agent，再与 default agent 建 1:1 房间。

---

## 3. 连接流程

### 3.1 PC 端

1. 编译 GoMatrix（coara 托管会按这个二进制拉起，重编译后重启 coara 即生效）：

   ```powershell
   cd gomatrix
   go build -o gomatrix.exe ./cmd/gomatrix
   ```

2. 常驻内核启动即托管 Matrix：

   ```powershell
   cd D:\my-workspace
   coara                     # 拉起/复用常驻内核（tray/daemon），Matrix 由内核托管
   coara tray                # 无头内核 + 系统托盘（右键开 Web / 手机二维码 / 退出）
   ```

3. GoMatrix 默认监听 `0.0.0.0:8008`（`gomatrix/internal/config/config.go`）。coara 读取 `config.yaml` 的 `matrix:` 节（或 `COARA_MATRIX_HOMESERVER` / `COARA_MATRIX_USER` / `COARA_MATRIX_PASSWORD` 环境变量）连接。探测到本机 GoMatrix 未运行时，coara 会尝试拉起捆绑的 `gomatrix.exe`（依次找 `%COARA_ROOT%\bin\`、`%LOCALAPPDATA%\coara\bin\`、`~/.local/coara/bin/`），拉起失败则跳过 Matrix 并提示（`src/cli/matrix_connect.py`）。
4. coara 登录 bot 账号，开始 `/sync` 长轮询（timeout 30s）。
5. 收到第一条用户消息后，`bind_matrix_active_room` 把当前 room 写入 `{coara_home}/runtime/active.json` 的 `matrix_room_id`，并把房间号持久化到 `{coara_home}/matrix_notify_room_id.txt`（重启后通知房间不丢）。

### 3.2 手机端

1. 打开系统托盘「手机连接」弹出配对二维码（由 gomatrix 数据接口产出、PC 代理展示；08-30 起二维码入口从 WebUI 移入托盘）。App 安装包在官网首页扫码下载（`https://coara.top/`）。
2. 手机装好 App 后，在「设置 → 连接设置」扫配对码，或手动输入 homeserver URL。公网场景是 Cloudflare tunnel URL（`https://*.trycloudflare.com`）；LAN 调试用本地 URL（`http://192.168.x.x:8008`）。
3. **扫码即连**：QR② 配置了 `[pairing]` 时携带固定账号凭据，App 直接注册/登录该账号（`authenticate` 先注册、已存在则登录），重装后找回同一账号与房间；未配置时退回 `phone_xxxxxxxx` 随机账号（`MatrixIdentity.ensureCredentials` / `freshCredentials`）。同一服务器换了 URL（如 quick tunnel 重启）时会复用旧凭据找回旧账号旧房间；隧道暖机有几秒窗口，扫码首连失败会自动重试两次。
4. App 调用 `GET /api/agents` 发现 agent 列表。
5. App 查找或创建与 default agent（`@coara`）的 1:1 房间（`is_direct = true`），开始 `/sync`。

> **QR② 的内容**：一个 JSON payload（`gomatrix/internal/connect/qr.go`），App 也兼容直接扫纯 URL（`android-app/.../matrix/ConnectQr.kt`）：

```json
{
  "m.server": "https://xxx.trycloudflare.com",
  "coara.server_name": "coara.local",
  "coara.bot_user": "@coara:coara.local",
  "coara.tunnel": "true"
}
```

> QR 码**不含任何账号密码**。手机端**没有独立工作空间概念**——它绑定的是 PC 端 `active.json` 里当前活跃的工作空间，因为代码执行、动态、宝箱都在 PC 端完成。

---

## 4. 普通聊天消息流

一条普通聊天消息从手机到 coara 再回到手机经过的每一站：

1. App 调用 `MatrixApiService.sendMessage(roomId, body)`。
2. `PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}` 到 GoMatrix（txnId 去重，重放返回同一 `event_id`）。
3. coara bot 的 `/sync` 长轮询收到事件。
4. `route_inbound_text_event`：跳过自己的 echo，按 `@mention` 前缀决定由哪个 bot 处理（`mention_routing.should_process`）。
5. `prefilter_matrix_text_event`：侧信道消息在此被消费（详见 §5），被消费就不再进入 LLM 回合。
6. `try_defer_to_continuation_input`：如果 Root 正在跑回合，普通文本（非 `/` 开头）被包装成来源标签（`<手机消息>`，见 `src/core/message_tags.py`）缓冲为回合中追加输入，不进新回合。
7. `MatrixMessageDispatcher` 按 session_key 分锁调度（同一工作空间串行，跨空间并发；`/ws` 走旁路锁）。schedule 时绑定当前前台 session（**入队即绑定**：排队期间切空间，消息仍在发送时所见的空间处理）→ `process_matrix_text_message` → `deliver_remote_text_to_coara`。
8. 进入 `remote_turn(room_id, …)` 上下文，`stream_coara_reply_to_matrix` 调用 `RootCoara.process_message`，每个流式分片立即发到房间；`✓/✗` 工具摘要行不发往手机（CLI 模式下本地回显）。**yield 是手机端唯一消息来源**——turn 循环每个 continue/退出分支都先 flush 本轮文本（不变量见 `COARA_ARCHITECTURE.md` §5.2.1）；轮次结束后的 leftover 注入（子智能体结果等 `<系统消息>`）同样走 `stream_coara_reply_to_matrix` 投递，不丢弃输出。
9. 回合由 `matrix_turn_scope` 包裹，收尾（正常/异常/slash 提前返回）必发 `[COARA_TURN]` 信封。**手机端 typing 指示灯三态**：发出消息即亮三点动画；`{"event":"end","background":true}`（主轮次收尾但后台级联工作仍在跑）降级为单点闪烁；`{"event":"end","background":false}` 或后台全部归零后补发的 `{"event":"quiet"}` 灭灯。中途任何输出都不影响指示灯；PC 崩溃导致信号永失时靠 30 分钟本地兜底收场，下一次发送/信号会重校状态。所有 bot 消息在 content 自定义键 `coara_turn` 打标（intermediate/final）。m.typing ephemeral 不再使用（隧道链路下不可靠）。
10. App `/sync` 收到回复，`processSync` 按 `event_id` 去重后上屏。

### 多 agent 路由

多个 agent 共享房间时按消息开头的 `@mention` 前缀路由（`src/matrix_client/mention_routing.py`）：

| 手机输入 | 处理者 | 说明 |
|----------|--------|------|
| `@coara 帮我写代码` | coara | 去掉 `@coara` 前缀后交给 coara |
| `@somebot 分析代码` | somebot | 去掉前缀后交给对应 agent |
| `帮我写代码` | default agent（coara） | 无 mention 走默认 agent |
| `@其他名字 …` | 无人处理 | mention 不是已知 agent 也不匹配自己时，bot 保持沉默 |

---

## 5. 侧信道协议

本节是 App ↔ PC 全部隐藏协议的总表和完整负载示例。

> 术语：**侧信道（side-channel）**指夹在普通聊天文本里的机器协议消息——它们同样在 Matrix 房间里同步，但 App 聊天 UI 默认**不展示**（`shouldHideInChatUi`），PC 端 prefilter 也不会让它们进入 LLM 回合。

| 协议 | 方向 | 用途 | 源码（PC · Android） |
|------|------|------|------|
| `m.coara.approval` / `m.coara.approval_reply` / `m.coara.approval_resolved`（自定义 msgtype，非文本信封） | 双向 | 工具调用前确认 | `approval_bridge.py` · `ui/ApprovalMessage.kt` |
| `[COARA_VAULT]` / `[COARA_VAULT_REPLY]` | 双向 | 宝箱主密码解锁 | `vault_bridge.py` · `ui/VaultMessage.kt` |
| `[COARA_COLLECT]` / `[COARA_COLLECT_ACK]` | 双向 | 消息卡片收藏 → `records/user/`（取消同步删除） | `collect_bridge.py` · `ui/CollectMessage.kt` |
| `[COARA_UPDATES]` | PC → App | 工作空间动态（updates）推送：红点未读数 + 最新摘要 + `pending` 待批复列表 | `src/coara/updates_matrix_sync.py` + `src/workspace/updates/catalog.py` · `coara/UpdatesSyncParser.kt` |
| `[COARA_UPDATE_CMD]` / `[COARA_UPDATE_ACK]` | 双向 | 批复指令（read/archive/mark_read/review）与回执，执行后重推 `[COARA_UPDATES]` | `src/matrix_client/update_cmd_bridge.py` + `src/workspace/updates/review.py` · `coara/UpdatesSyncParser.kt` |
| `[COARA_MODELS]` `[COARA_WORKSPACES]` `[COARA_THINKING]` `[COARA_USAGE]` `[COARA_STATUS]` | 双向 | slash / 工作空间浏览器面板同步（含 `type:"query"`） | `src/coara/mobile_sync.py` · `coara/MobileSyncParser.kt` |
| `[COARA_DIFF]` | PC → App | 文件编辑 diff 卡片 | `diff_bridge.py` · `ui/DiffMessage.kt` |
| `[COARA_TURN]` | PC → App | turn 信号：手机 typing 指示灯唯一准绳。`{"event":"end","background":bool}` 由 `matrix_turn_scope` 收尾时发送（正常/异常/slash 提前返回均覆盖），background=true 时手机降级为单点闪烁；后台级联工作全部归零后由 matrix_runner 补发 `{"event":"quiet"}` 灭灯；历史回填中的旧信封不触发。所有 bot 消息带 content 键 `coara_turn`（intermediate/final） | `src/matrix_client/turn_signal.py` + `send_guard.py` + `src/coara/background_activity.py` · `matrix/MatrixApiService.kt` |
| `[COARA_QUOTE_MXC]` | App → PC | 引用图片消息的 mxc 标记 | `remote_vision.py` · `ui/ChatQuoteUtil.kt` |
| `[vault] …` | PC → App | 宝箱操作回执（App 隐藏） | `vault_bridge.py` · `ui/VaultMessage.kt` |
| `/new` `/stop` `/restart` 等 slash 命令 | App → PC | 远程控制（App 侧 canonical 见闭源仓文档） | `chat_commands.py` |

### 5.1 审批协议

工具调用需要确认时经 ApprovalCenter（`src/coara/approval_center.py`，状态机 / 幂等 resolve / fail-closed）发起，审批只投递到回合来源端房间；Matrix 桥只是哑管道（`src/matrix_client/approval_bridge.py`）。

审批走自定义 msgtype 的 m.room.message 事件，`content` 即结构化 JSON（不再夹在聊天文本里；Android 载荷见 `data/model/ApprovalModels.kt`，字段以服务端 docstring 为准）：

| msgtype | 方向 | 主要 content 键 |
|---|---|---|
| `m.coara.approval` | PC → App | approval_id / question / options[] / timeout_s / workspace / created_at_ms |
| `m.coara.approval_reply` | App → PC | approval_id / approved(bool) |
| `m.coara.approval_resolved` | PC → App | approval_id / outcome(approved / rejected / timeout / cancelled) |

- App 把请求卡渲染成「同意 / 不同意」按钮，回执按 approval_id 精确路由进 `ApprovalCenter.resolve`（幂等终态转换）；卡片按 timeout_s 本地倒计时，晚到的历史卡自动禁用。
- 超时 / 打断 / 用户已答都会补发 `m.coara.approval_resolved` 终态帧，卡片据此置灰；审批只发回合来源端房间，无回合上下文的请求由 ApprovalCenter fail-closed 拒绝。
- **重启失效**：等待中的 future 在 ApprovalCenter，服务端重启后旧卡由终态帧收口，按钮不再有效。宝箱解锁卡的重启失效通知另见 §5.2 与 `src/matrix_client/pending_interactions.py`。

### 5.2 宝箱协议

密码**永远不会进入 LLM 对话**，只通过侧信道传输。PC 端需要宝箱但处于锁定状态时（`maybe_prompt_vault_unlock`，超时 60s），推送：

```text
[COARA_VAULT]
initialized: true
locked: true
title: 宝箱解锁
question: 请输入主密码（不会进入 AI 对话）
hint: Agent 需要访问宝箱
[/COARA_VAULT]

宝箱已锁定。请在下方聊天卡片输入主密码解锁（密码不会进入 AI 对话）。
```

App 的 `VaultPromptCard` 内联输入密码后发送（取消时不带 password 行）：

```text
[COARA_VAULT_REPLY]
action: unlock
password: <主密码>
[/COARA_VAULT_REPLY]
```

- `action` 合法值：`unlock` / `status` / `cancel`。
- 密码错误：回 `[vault] …` ack 并**继续等待**（卡片可重试）；取消：回 `[vault] 已取消。` 并解除工具阻塞。
- ack 形如 `[vault] 宝箱已解锁` / `[vault] 已初始化 · 已锁定 · 封存文件 3`，App 不展示。

### 5.3 消息收藏协议（`[COARA_COLLECT]`）

App 消息卡片「收藏 / 取消收藏」旁路（不进 LLM）。文本写入 `records/user/entries/*.md`；文件由 PC 下载 Matrix 媒体到 `records/user/files/<id>/` 并写 md 索引。取消收藏同步删除 PC 条目。

```text
[COARA_COLLECT]
action: add
event_id: $matrixEventId
msgtype: m.text
title: …
body:
多行正文…
[/COARA_COLLECT]
```

文件/图片额外带 `mxc:` / `filename:` / `mime:`。取消：

```text
[COARA_COLLECT]
action: remove
event_id: $matrixEventId
id: col_…          # 可选；无则按 matrix:{event_id} 查找
[/COARA_COLLECT]
```

PC ACK（App 隐藏）：

```text
[COARA_COLLECT_ACK]
event_id: $matrixEventId
ok: true
action: add
id: col_…
[/COARA_COLLECT_ACK]
```

- `action` 为 `add` / `remove`，供 App 在失败时正确回滚心形（避免连点竞态）。
- 失败时 `ok: false` 并带 `error:` 单行说明；无效信封也会 ACK 错误。

### 5.4 工作空间动态协议（`[COARA_UPDATES]`）

这条链路解决：手机不在局域网时也能看到各工作空间的红点（未读动态数）和最新动态摘要。PC 端在两种时机推送（`src/coara/root.py`）：

| 时机 | 负载 |
|------|------|
| 新动态产生（事件源、工作流终态、事项运行） | `workspaces` + `message`（本条动态详情） |
| 已读水位推进（`ws updates` 标记已读后） | 只有 `workspaces`，App 据此清零红点 |

完整负载：

```text
[COARA_UPDATES]
{"type":"workspace_updates","workspaces":[{"workspace_id":"ws_a1b2c3","name":"暄","summary":"个人工作区","path":"D:\\xuan","unread":2,"latest":{"type":"event","title":"inbox 新增 3 个文件","created_at":"2026-07-24T09:30:00"}}],"message":{"message_id":"upd-…","workspace":"暄","source_id":"inbox-watch","event_type":"file","type":"event","payload_ref":null,"status":"new","title":"inbox 新增 3 个文件","text":"…","display_text":"…","created_at":"2026-07-24T09:30:00"}}
[/COARA_UPDATES]
```

- `workspaces[]`：注册表中所有 active 工作空间（`src/workspace/updates/catalog.py`），每项含 `unread`（未读数）与 `latest`（最新一条的 `type/title/created_at`，无为 `null`）。
- `pending[]`：跨空间高显著未读列表（最多 20 条，含 `message_id/workspace/title/salience/type/summary/created_at`），驱动抽屉「待批复」区。
- 批复指令走 `[COARA_UPDATE_CMD]`（action: read/archive/mark_read/review），PC 执行后回 `[COARA_UPDATE_ACK]` 并重推本负载；review 由 `src/workspace/updates/review.py` 路由到内容所属空间的会话。
- PC 若收到入站 `[COARA_UPDATES]`，prefilter 直接丢弃（`is_updates_control_message`），不进 LLM。

### 5.5 命令面板同步协议（`mobile_sync`）

手机 slash 面板与左侧工作空间浏览器的实时数据走同信封双向协议：App 发 `type:"query"`，PC 回最新 JSON。

```text
[COARA_MODELS]
{"type":"models","current":"xiaomi/mimo-v2.5-pro","choices":[{"idx":1,"key":"xiaomi/mimo-v2.5-pro","label":"MiMo v2.5 Pro"},{"idx":2,"key":"minimax/MiniMax-M3","label":"MiniMax M3"}]}
[/COARA_MODELS]
```

```text
[COARA_WORKSPACES]
{"type":"workspaces","active_id":"ws_a1b2c3","workspaces":[{"id":"ws_a1b2c3","name":"暄","summary":"个人工作区","path":"D:\\code_ws\\xuan_android_local","active":true}]}
[/COARA_WORKSPACES]
```

```text
[COARA_THINKING]
{"type":"thinking","enabled":true,"source":"本会话","note":"…","model":"kimi/k3","level":"medium","supports_levels":true}
[/COARA_THINKING]
```

```text
[COARA_USAGE]
{"type":"usage","summary":"输入 1,234 · 输出 567","input_tokens":1234,"output_tokens":567,"llm_turns":3}
[/COARA_USAGE]
```

```text
[COARA_STATUS]
{"type":"status","summary":"v8 · 对话 5 条","workspace":"v8","session_id":"4e2f…","last_user_activity_at":1754000000000,"idle_timeout_seconds":7200}
[/COARA_STATUS]
```

- 推送时机：连上 Matrix 通知房间后 `push_mobile_sync_payloads` 推全套；聊天执行 `/model` `/ws` `/thinking` `/usage` `/status` `/new` 后再推相关份（`chat_commands.py`）。此外 `start_new_session` 后强制推 status；用户活动（CLI / Web / Matrix）触发节流 60s 的 status 推送（`mobile_sync.push_status_payload`）。
- App 展开 slash 面板时 query models / usage / status / thinking；打开工作空间浏览器时按需 query workspaces。`try_handle_mobile_sync_query` 经 `configure_mobile_sync_sender` 回包，**与回合无关**。
- `supports_levels` 为 true 时低/中/高可点（如 Kimi K3）；否则档位灰显（`src/llm/thinking_mode.py`）。
- 状态摘要含工作空间名、本会话消息条数，以及 Root 空闲时钟字段（`session_id` / `last_user_activity_at` 毫秒 / `idle_timeout_seconds`）——手机据此镜像 Root 时钟，**不**自行发 `/new`；在线指示在 App 顶栏绿点。

### 5.6 Diff 推送协议（`[COARA_DIFF]`）

Matrix 回合中 `edit`/`write` 工具完成后，PC 把紧凑 diff 推到手机渲染成 diff 卡片（`wire_matrix_diff_display` 订阅 `tool_complete` 事件，仅 remote turn 内、仅当前会话）：

```text
[COARA_DIFF]
{"path":"D:\\xuan\\app.py","added":3,"removed":1,"remaining":0,"lines":[{"t":"ctx","s":" def main():"},{"t":"del","s":"-    run()"},{"t":"add","s":"+    run(debug=True)"}]}
[/COARA_DIFF]
```

- `lines[]` 为逐行负载（`t` 为行类型：上下文/删除/新增），超上限时折叠并计入 `remaining`。
- App 端 `ui/DiffMessage.kt` 解析渲染；PC 端格式构造函数是 `build_matrix_diff_message`（`diff_bridge.py`）。

### 5.7 引用图片标记（`[COARA_QUOTE_MXC]`）

App 引用一张图片回复时，把被引用图的 mxc URI 附在正文尾部：

```text
这张图哪里有问题？

[COARA_QUOTE_MXC]mxc://coara.local/abc123[/COARA_QUOTE_MXC]
```

PC 端 `remote_vision.strip_quote_mxc_marker` 剥掉标记并下载被引用图，与新发图片一起组成 Vision 多模态块（尺寸上限 1280px / base64 350KB，防止撑爆上下文窗口）。

---

## 6. 工作空间绑定

PC 端维护「当前活跃工作空间」文件 `{coara_home}/runtime/active.json`（`src/coara/workspace_runtime.py`）：

```json
{
  "workspace_id": "ws_a1b2c3",
  "workspace_path": "D:\\my-workspace",
  "workspace_name": "my-workspace",
  "pid": 12345,
  "session_id": "…",
  "coara_id": "…",
  "coara_name": "…",
  "started_at": "2026-07-24T09:00:00+00:00",
  "matrix_enabled": true,
  "matrix_room_id": "!xxx:coara.local"
}
```

- coara 启动时发布 `active.json`；手机第一次发消息的房间由 `bind_matrix_active_room` 写入 `matrix_room_id`，同时持久化到 `{coara_home}/matrix_notify_room_id.txt`。
- 手机与这个工作空间交互：所有代码执行、文件读写、宝箱、动态都在该目录下完成。读取 `active.json` 时忽略 PID 已死的陈旧条目；优雅关闭时按 PID 匹配清理。

---

## 6.5 REST 代理（/coara-api）

模块页（用量/记录/工作流/配置）需要 WebUI 的 REST API，但 coara web server 只绑 `127.0.0.1:8080`，手机够不着。GoMatrix 因此内置反向代理：

```text
App ──(Matrix access token)──> GoMatrix /coara-api/* ──(注入 X-Coara-Token)──> 127.0.0.1:8080/api/*
```

- 路由挂在 `AuthMiddleware` 组内：没有 Matrix 账号令牌一律 401（`gomatrix/internal/api/router.go`）。
- 代理转发时注入 `X-Coara-Token`（读 `<coara_home>/system/dashboard_token`，mtime 变化自动重载；gomatrix 与 coara 同机运行）。dashboard token 永不下发手机、永不出本机。
- 仅代理 `/api/` 前缀（`/coara-api/usage/dashboard` → `/api/usage/dashboard`），不代理 WebSocket 与静态资源。
- coara 未启动 → 502 `{"error":"pc_unreachable"}`；token 文件不存在 → 503 `{"error":"dashboard_token_unavailable"}`。
- 配置：`coara_api.enabled / upstream_port / token_file`（TOML）或 `GOMAX_COARA_API_ENABLED / _PORT / _TOKEN_FILE`（环境变量），默认开、端口 8080、token 文件取 `$COARA_HOME/system/dashboard_token`。
- 授权语义：**有 Matrix 账号 = 有全部 REST 权限**。当前房间成员只有用户本人与 bots，信任域一致；批复等 Matrix 侧信道不受影响，继续并行存在。

---

## 7. 信任与安全

远程发来的消息在 PC 端被当成「自己人」还是「不可信来源」，取决于入口（`resolve_matrix_trust_level`）：

| 模式 | 命令 | `cli_owner` | 远程发送者信任等级 |
|------|------|-------------|-------------------|
| 内核托管 Matrix | `coara` / `coara tray`（Matrix 由内核托管） | `True` | 一律视为 `owner`（人就在 PC 前操作） |
| 独立 Bot 模式 | `python -m src.matrix_client` | `False` | 检查 `security.owner_matrix_ids`，不在列表中为 `untrusted` |

配置方式：

```yaml
security:
  owner_matrix_ids:
    - "@phone_a1b2c3d4:coara.local"
```

`untrusted` 发送者会启用执行沙箱（命令/路径/URL 阻止规则）。宝箱密码等敏感旁路不受信任等级影响——密码永不进 LLM 上下文。

---

## 8. 后台与通知

- App 退到后台时，`MatrixSyncForegroundService` 保持 `/sync` 长连接，状态栏常驻通知（标题「考拉已连接」，正文随连接状态变化）。
- 新消息到达且 App 不在前台时弹系统通知；提醒到点不再发房间消息，而是落工作空间动态收件箱（`reminder` 类型、`salience: high`），经 `[COARA_UPDATES]` 红点同步到 App（见 [工作空间动态模型](./工作空间动态模型.md)）。
- PC 侧的后台任务（bash / agent）完成时**不再**自动跑回合：目标 session 忙则注入当前回合（推送随该回合通道），空闲则 park 进驻会话历史（不再落收件箱），不单独推 Matrix（`src/coara/root.py` `_on_background_task_complete`，见闭源仓 App 文档 §后台任务完成）。
- 工作流完成结果落工作空间动态（`workflow` 类型），完成/失败/取消等终态 `normal`，统一经 `[COARA_UPDATES]` 红点推送。
- 国产机需要允许自启动、后台运行、关闭电池优化，否则前台服务可能被 ROM 杀死。

---

## 9. 文件桥

- **PC → 手机**：`MatrixFileBridge.send_file`（`file_bridge.py`）把本地文件上传到 GoMatrix 媒体库，按 MIME 发 `m.image` 或 `m.file` 消息到房间。远程回合里 LLM 可用 `send_file` 工具主动发文件——`SendFileTool` 经 `OutboundFileRouter` 统一 Matrix/Web 出站（`target=auto|matrix|web`），Matrix 桥由 `register_remote_file_tools`（`file_tools.py`）挂到共享 router，Web 桥挂载不冲突；目标房间取 remote turn 的房间或最近绑定房间。
- **手机 → PC**：App 发的图片/文件经 `media_inbound.py` 入站——图片下载后组成 Vision 多模态块（上限同 §5.7），文件保存进工作空间并把路径告知 LLM。

---

## 10. 可靠性语义（/sync 同步屏障）

目标：手机 ↔ 本地 coara **零遗漏、低延迟、单一路径**。出站走 `PUT /rooms/{id}/send/m.room.message/{txnId}`，入站走长轮询 `GET /sync?since=s{N}&timeout=30`；**GoMatrix 是唯一 homeserver**。

### 10.1 GoMatrix `/sync` 保证（对齐 Conduit）

1. **写入屏障**：timeline append 持锁至 commit；`/sync` 读前 `WaitTimelineIdle()` + `WaitWriteIdle()`（`internal/service/sync.go`）
2. **先提交再 Notify**：`Notify` 在事务 commit 之后、仍持写锁时发出（`internal/service/timeline.go`）
3. **fail-closed `next_batch`**：用户房间若有未投递 timeline，token **不前进**（`resolveNextBatch`）
4. **invitee 可见 timeline**：被邀请（尚未 join）的 bot 在 `rooms.join` 里也能收到该房 timeline，避免「邀请后第一条消息丢失」
5. **initial sync 不倾倒历史**：防 bot 重放旧消息；历史走 `GET /messages`（Android `hydrateRoomTimeline`）
6. **typing**：`m.typing` 经 ephemeral 随 sync 下发；coara 在整段 Matrix turn 内保持 typing（20s 刷新 / 60s TTL），**turn 结束**才清除；GoMatrix 对停止快照重播 45s 兜底丢失的 clear，App 端以 m.typing 为唯一准绳（90s 本地兜底超时防卡死；sync 失败恢复不复位 typing）

### 10.2 Android 收发可靠性

发送：

1. `stageOptimisticOutgoing` → 立即显示气泡
2. `PUT .../send/.../{txnId}`（最多 3 次尝试）
3. 成功：`local-{txnId}` → `$event_id`
4. 失败：撤销乐观气泡 + 错误提示

接收：

1. 后台协程：`GET /sync?since=…&timeout=30`（LAN/隧道均为 30s 长轮询）
2. `processSync`：按 `event_id` 去重（仅允许 local→server echo 匹配）
3. `timeline.limited==true` 时调用 `hydrateRoomTimeline` 补页
4. 冷启动 / 进房：`restoreCachedMessages` + 一次 `hydrateRoomTimeline`

`/sync` 是唯一实时 ingress；**不**做周期性 `/messages` 补拉。

### 10.3 coara 端可靠性

入站：

1. `matrix-nio` `/sync` 长轮询 → `on_message` 回调（回调只做分发，不阻塞长轮询）
2. 跳过自身 echo；`@mention` 路由；侧信道 prefilter（approval / vault / updates / mobile sync）
3. 回合中到达的普通文本经 `try_defer_to_continuation_input` 缓冲为来源标签（`<手机消息>`）追加输入；slash 命令不缓冲
4. `MatrixMessageDispatcher` 按 session_key 分锁处理（跨空间并发，入队即绑定发送时 session）→ `process_message` → `stream_coara_reply_to_matrix`；回合中途切空间后，剩余分片带 `[空间名]` 前缀照发到房间

出站：全部经 `matrix_room_send_text`（`send_guard.py`：含 Markdown 标记时转 HTML、最多 3 次尝试且**复用同一 txnId**（服务端按 txnId 去重）、登出竞态防护），无双份 send 路径。

Invite 安全：

- 冷启动无 token：`park_sync_token_without_timeline()` 先把 `next_batch` 推进到服务器顶点，再挂消息回调——否则 initial `/sync` 可能把历史消息当成新回合重放
- token 报「invalid sync token」时清空并重 park（`sync_token.py` + `sync_helpers.py`）
- `sync_for_pending_invites()`：启动时一次 catch-up sync 后加入 pending invites
- 运行期靠 `InviteMemberEvent` 回调实时接邀请（`bot.py` / `matrix_runner.py`），无额外轮询

### 10.4 验证要点

- 手机发消息 → coara 终端 `[Matrix] <-` 出现
- coara 回复 → 手机 **≤1 次 sync 周期**（LAN 下通常 <1s）显示
- 重启 coara 后首条消息仍可达（invite token + 无 age 过滤）

---

## 11. 源码索引

### PC 端

| 文件 | 职责 |
|------|------|
| `src/matrix_client/bot.py` | 独立 bot 入口主循环（`python -m src.matrix_client`） |
| `src/cli/matrix_runner.py` | 内核托管 Matrix 的端内 runner（08-30 前为 `coara -x` CLI Matrix 前端入口） |
| `src/cli/matrix_connect.py` | homeserver 探测、捆绑 `gomatrix.exe` 自动拉起 |
| `src/matrix_client/client_bootstrap.py` | 两个入口共享的装配：建 client、登录、agent 发现、sync 循环（三处有意保留的行为差异见模块 docstring） |
| `src/matrix_client/ingress_helpers.py` | 侧信道 prefilter、回合中输入缓冲、房间绑定、信任判断、join 重试 |
| `src/matrix_client/inbound_handlers.py` | 文本/媒体消息入口，typing 保活，调用 Root |
| `src/matrix_client/mention_routing.py` | 多 agent @mention 路由 |
| `src/matrix_client/approval_bridge.py` | 工具审批侧信道 |
| `src/matrix_client/pending_interactions.py` | pending 交互（审批/询问/宝箱）元信息持久化与重启失效通知 |
| `src/matrix_client/vault_bridge.py` | 宝箱解锁侧信道 |
| `src/matrix_client/collect_bridge.py` | 消息收藏侧信道 → `records/user/` |
| `src/matrix_client/diff_bridge.py` | `[COARA_DIFF]` 编辑 diff 推送 |
| `src/matrix_client/remote_vision.py` | `[COARA_QUOTE_MXC]` 解析、Matrix 图片 → Vision 块 |
| `src/matrix_client/chat_commands.py` | slash 命令分发（`normalize_remote_command_body` 剥来源标签包装） |
| `src/matrix_client/response_stream.py` | 流式回复到 Matrix |
| `src/matrix_client/send_guard.py` | 发送重试（同 txnId）、关闭竞态防护、Markdown→HTML |
| `src/matrix_client/sync_token.py` / `sync_helpers.py` | GoMatrix sync token 持久化、冷启动 park、invite catch-up |
| `src/matrix_client/turn_signal.py` | `m.typing` 整回合保活 |
| `src/matrix_client/remote_channel.py` | `RemoteInteractionChannel` 的 Matrix 实现（审批/询问委托给各 bridge） |
| `src/matrix_client/file_bridge.py` / `file_tools.py` / `media_inbound.py` | 文件上传/下载、远程 `send_file` 接线、媒体入站 |
| `src/tools/builtin/integration/outbound_file.py` + `src/ui/web_file_bridge.py` | OutboundFileRouter / WebFileBridge：send_file 统一 Matrix/Web 出站（target 路由、/api/outbound-files） |
| `src/coara/mobile_sync.py` | models / workspaces / thinking / usage / status 面板同步 |
| `src/coara/updates_matrix_sync.py` + `src/workspace/updates/catalog.py` | `[COARA_UPDATES]` 信封与 workspaces 负载构建 |
| `src/coara/matrix_notify.py` | 房间外通知（事件/事项/后台完成）、通知房间持久化 |
| `src/coara/workspace_runtime.py` | `active.json` 读写 |
| `src/coara/remote_turn.py` | 当前 remote turn 上下文（房间、发送器、交互通道） |
| `src/coara/turn_source.py` | 回合来源标签与「是否推 Matrix」判定 |
| `src/runtime/restart.py` | `/restart` 内核安全交棒：落意图 → 让位 → 复用原启动方式拉起 → 端口就绪确认 |

### Android 端

| 文件 | 职责 |
|------|------|
| `matrix/MatrixApiService.kt` | Matrix API 封装、登录、sync、房间管理 |
| `matrix/ChatViewModel.kt` | 业务逻辑、命令发送、面板状态、侧信道发送、Root 会话边界分隔线 |
| `matrix/MatrixSyncForegroundService.kt` | 后台 sync 前台服务 |
| `matrix/ConnectQr.kt` | 配对 QR 解析（JSON payload 或纯 URL） |
| `matrix/MatrixIdentity.kt` | 扫码自动准备账号（`phone_xxxxxxxx` + 随机密码） |
| `matrix/SessionInactivityTracker.kt` | Root 空闲时钟的本地镜像（不自发 `/new`，超时时触发 status 查询） |
| `matrix/NotificationHelper.kt` | 系统通知、`[定时提醒]` 高优先级通道 |
| `ui/AgentSelectorRow.kt` / `ui/ChatTopBar.kt` | 多 agent 选择、顶栏 |
| `ui/ApprovalMessage.kt` | 审批卡片 + `shouldHideInChatUi` 隐藏规则 |
| `ui/VaultMessage.kt` / `VaultPromptCard.kt` | 宝箱卡片 |
| `ui/CollectMessage.kt` | 消息收藏信封 / ACK |
| `ui/DiffMessage.kt` | `[COARA_DIFF]` diff 卡片 |
| `ui/ChatQuoteUtil.kt` | 引用构造（含 `[COARA_QUOTE_MXC]`） |
| `ui/WorkspaceUpdatesMenu.kt` | 工作空间菜单（红点 + 未读数 + 最新摘要） |
| `coara/UpdatesSyncParser.kt` / `UpdatesModels.kt` / `UpdatesStateCache.kt` | `[COARA_UPDATES]` 解析、模型、`updates_state.json` 缓存 |
| `coara/MobileSyncParser.kt` | 面板同步信封解析与 query 构造 |

### GoMatrix

| 文件 | 职责 |
|------|------|
| `internal/api/client/agents.go` | `GET /api/agents`（免登录） |
| `internal/service/agents.go` | agent 自动注册、`#agents` 共享房间 |
| `internal/connect/qr.go` | 配对 QR 的 JSON payload |
| `internal/dashboard/` | 配对 QR PNG / 状态 JSON 数据接口（页面已退役，UI 在 coara WebUI「手机」页） |
| `internal/service/sync.go` / `timeline.go` | `/sync` 屏障、fail-closed `next_batch`、commit 后 Notify |
| `internal/tunnel/` | Cloudflare quick/named tunnel |

---

## 12. 常见误区

1. **App 直接执行代码？**
   不。所有代码执行、文件读写、工具调用都在 PC 端 coara 完成。App 只是远程 UI。

2. **QR 码包含登录密码？**
   不。QR 码是 homeserver URL + 服务器名 + 默认 agent 的 JSON payload。扫码后 App 自动注册一个 `phone_xxxxxxxx` 账号（随机密码存在手机 Keystore）；也可以在连接设置里手动填写账号密码。

3. **App 是一个 Matrix agent？**
   不。App 是普通 Matrix 用户；agent 是 PC 端的 `@coara`。

4. **手机端能独立管理工作空间？**
   不。工作空间绑定在 PC 端 `active.json`。手机端通过 Matrix 与当前 PC 活跃工作空间通信。

5. **侧信道消息会出现在聊天记录里？**
   在 Matrix 房间中会同步，但 App UI 默认隐藏；PC 端 prefilter 也不会让它们进入 LLM 回合。
