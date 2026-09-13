# GoMatrix

轻量级 Matrix homeserver，纯 Go 编写、零 CGO。**定位：coara 的内嵌接入层（married sidecar）**——核心职责是连接手机与本地 coara；外部智能体可接入，但只是访客。一体化定位与托管语义见主仓 `docs/接入层-gomatrix.md`。

## 特点

- **零 CGO** — 使用 `modernc.org/sqlite`（纯 Go SQLite），不需要 MinGW/WSL
- **Windows 原生** — 任何装了 Go 的 Windows 机器上 `go build` 即可编译
- **单文件二进制** — `gomatrix.exe`（约 12MB），无外部依赖
- **coara 托管** — coara 启动时以 `--config` 拉起/看护/关停（无头子进程），数据目录归 `<coara_home>/matrix/`；也可独立运行
- **多 agent** — 启动时按 `[[agents]]` 自动注册机器人账号（默认仅 `@coara`）

## 启动方式

**正常用法：不需要单独启动 gomatrix。** 它由 coara 托管——启动 coara 时自动拉起（`matrix.host_enabled` 默认开），数据目录在 `<coara_home>/matrix/`，配对二维码在 coara WebUI 侧栏「手机」页。

开发调试需要单独跑时：

```bash
cd gomatrix
go build -o gomatrix.exe ./cmd/gomatrix   # Windows
go build -o gomatrix ./cmd/gomatrix       # Linux
./gomatrix --config gomatrix.toml         # 无头服务模式（唯一形态，托盘与 dashboard 页面已退役）
```

注意：单独启动会占用 `gomatrix.toml` 里的端口与当前工作目录的数据库；coara 托管实例使用 `<coara_home>/matrix/` 的数据，两者不要同时跑（会抢端口、数据分叉）。日志：`%LOCALAPPDATA%\GoMatrix\gomatrix.log`。

## 运行模式

无头服务为唯一形态：Homeserver 监听 `0.0.0.0:8008`，无托盘、无独立 dashboard 页面。配对二维码与隧道状态在 coara WebUI 侧栏「手机」页展示（经 PC 代理 `/dashboard/qr.png`、`/api/dashboard/status` 两个数据接口，仅本机/LAN 直连可访问）。

命令行参数：

```powershell
gomatrix.exe --config gomatrix.toml   # 指定配置文件（coara 托管即用此方式拉起）
gomatrix.exe --no-tunnel              # 仅局域网开发模式（无公网配对 QR）
```

复制 `gomatrix.toml.example` → `gomatrix.toml` 可自定义隧道和 agent。

## 手机连接（任意网络）

GoMatrix 默认拉起 **cloudflared**，把 8008 端口暴露为 `https://*.trycloudflare.com`，配对二维码（coara WebUI「手机」页展示）编码配对载荷供 coara App 扫码连接。需要 PATH 上有 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)。

> QR 码包含 homeserver URL（如 `https://xxx.trycloudflare.com`）与一次性配对凭据（5 分钟、单次有效），扫码后 App 自动生成 `phone_xxxxxxxx` 普通用户账号并注册/登录（不是 `@coara` agent 账号），无需手动输入凭据。

默认 **quick tunnel** 每次重启 GoMatrix 公网 URL 都会变，手机需重新扫码。固定域名用 **named tunnel**——coara 托管场景在 coara WebUI 配置页（`matrix.tunnel_*`）配置，spawn 时写入 toml；独立运行时直接在 `gomatrix.toml` 配置：

```toml
[tunnel]
enabled = true
mode = "named"
token = "<cloudflare-zero-trust-token>"
public_url = "https://matrix.example.com"
```

**隧道看门狗与进程生命周期**：

- GoMatrix 运行期间持续监控 cloudflared 进程，意外退出后按 1s / 2s / 4s…（上限 30s）指数退避自动重启，连续失败会记录日志；正常退出 GoMatrix 不触发重启
- 「手机」页隧道状态实时反映真实情况（`ready` / `reconnecting` / `error`），重连期间不再是假 Ready
- Windows 上 cloudflared 通过 Job Object（`KILL_ON_JOB_CLOSE`）绑定到 GoMatrix 进程生命周期：GoMatrix 崩溃或被强杀时 cloudflared 随之退出，不会残留孤儿进程
- quick 模式固有局限：重启隧道会分配新的 `*.trycloudflare.com` URL，手机端旧配对失效需重新扫码（「手机」页会显示新地址）——生产环境建议用上面的 named 模式固定域名

**链路可观测性**：

- cloudflared 的 stderr/stdout 按级别映射进 gomatrix 日志（error/failed/warn → Warn，连接注册/断开/重试 → Info），隧道抖动不再无痕
- 隧道每 5 分钟写一行 `tunnel heartbeat`（state/url/restarts/uptime）：日志连续即 gomatrix 存活，心跳中断本身即是告警
- 日志启动时轮转：超过 10MB 的旧日志改名为 `gomatrix.log.1`（只留一份备份）

## 配置文件

查找顺序：

1. `--config <path>` 命令行参数
2. `GOMAX_CONFIG` 环境变量
3. 当前目录的 `gomatrix.toml`

都不存在时用内置默认值（`server_name=coara.local`、`port=8008`）。

> 相对的 `database_path` / `media_path` 以**配置文件所在目录**为基准解析，不随进程工作目录移动。

完整示例见 [`gomatrix.toml.example`](gomatrix.toml.example)。核心字段：

```toml
server_name = "coara.local"
database_path = "./gomatrix.db"
address = "0.0.0.0"
port = 8008
allow_registration = true
allow_encryption = false
default_room_version = "10"
media_path = "./media"
log_level = "info"

[[agents]]
name = "coara"
password = "coara-bot-password"
display_name = "coara"
default = true
```

环境变量覆盖：`GOMAX_SERVER_NAME`、`GOMAX_PORT`、`GOMAX_ALLOW_REGISTRATION`、`GOMAX_DATABASE_PATH`、`GOMAX_TUNNEL_*` 等（见 `internal/config/config.go`）。

`[rate_limit]` 节控制 `/login`/`/register` 的限流：来源 IP+账号 桶与纯来源 IP 聚合桶都须通过（默认 login 10/min、register 5/min、IP 聚合 30/min、burst 3，超限 429 + `Retry-After`）；`/sync` 长轮询不限流。

## coara 集成

1. 启动 coara（`coara` / `coara -cx`）→ gomatrix 由 coara 托管自动拉起（`<coara_home>/matrix/` 数据目录），coara 连接 `http://127.0.0.1:8008`
2. 手机扫 coara WebUI 侧栏「手机」页的配对码

本机 8008 上已有健康实例时 coara 直接接入（adopt）；否则拉起安装目录旁的 `gomatrix.exe`（依次查找 `COARA_ROOT\bin\`、`%LOCALAPPDATA%\coara\bin\`、`~/.local/coara/bin/`、开发仓库 `gomatrix/`）；找不到则跳过 Matrix 并提示。连接配置见 `config.yaml` 的 `matrix:` 节或 `COARA_MATRIX_*` 环境变量。

## 已实现的 Matrix Client-Server API

| 功能 | 端点 | 状态 |
|------|------|------|
| 认证 | `/register`、`/login`、`/logout`、`/account/whoami` | 完整 |
| 设备 | `/devices`（GET/PUT/DELETE） | 完整 |
| 资料 | `/profile/{userId}/displayname`、`/avatar_url` | 完整 |
| 房间 | `/createRoom`、`/join`、`/leave`、`/invite`、`/forget`、`/joined_rooms` | 完整 |
| 别名 | `/directory/room/{alias}`（GET/PUT/DELETE） | 完整 |
| 状态 | `/rooms/{roomId}/state`、`/state/{type}/{key}` | 完整 |
| 消息 | `/rooms/{roomId}/send/{type}/{txnId}`、`/messages` | 完整 |
| 同步 | `/sync`（长轮询，有数据立即返回） | 完整 |
| 输入中 | `/rooms/{roomId}/typing/{userId}` | 完整 |
| 媒体 | `/media/v3/upload`、`/download`、`/thumbnail`（含 `_matrix/client/v1/media/*` 认证路径） | 完整 |
| E2EE | `/keys/upload`、`/query`、`/claim` | 桩（客户端关闭加密） |
| Federation | `/key/v2/server`、`/federation/v1/send` | 桩（仅 `allow_federation = true` 时挂载） |
| Well-known | `/.well-known/matrix/client`、`/server` | 完整 |
| Agent 发现 | `/api/agents`（自定义） | 完整 |
| 配对数据接口 | `/dashboard/qr.png`、`/api/dashboard/status`（页面已退役，UI 在 coara WebUI「手机」页） | 完整（仅本机/LAN 直连） |
| Capabilities | `/capabilities`、`/versions` | 完整 |
| CORS | 全局中间件 | 完整（仅 localhost/隧道来源） |

> **授权**：`/messages`、`/state*`、`/rooms/{roomId}/members` 要求请求者已加入该房间（否则 403）。`PUT /state*` 额外执行 `m.room.power_levels` 校验（`events[type]` → `state_default` = 50，`users[sender]` → `users_default` = 0）。


## 多 agent 自动注册

启动时按 `[[agents]]` 配置自动注册 agent 账号，并在 agent 数 ≥ 2 时创建共享房间：

- 注册配置的 agent（默认 `@coara`），agent 数 ≥ 2 时创建 `#agents:<server_name>` 共享房间并拉入全部 agent
- 后续启动：确保账号存在、更新显示名、确保房间成员关系
- `/api/agents` 返回 agent 列表及 `connected` 状态，供手机端发现

## 账号体系

同一服务器上有两类账号：

- **Agent（bot）账号** — `@coara:coara.local`，由 `gomatrix.toml` 的 `[[agents]]` 自动注册（默认密码 `coara-bot-password`）。PC coara 运行时用这些账号登录。
- **普通用户账号** — Android coara App 登录用的账号：扫配对码时凭一次性票据自动注册（`phone_xxxxxxxx`），也支持手动注册。登录后 App 经 `GET /api/agents` 发现 agent，并与 default agent 创建/加入 1:1 房间。

## 侧信道能力

普通聊天之外，GoMatrix 在 PC coara 与 App 之间转发隐藏侧信道消息：

| 能力 | 协议标记 | 说明 |
|------|----------|------|
| 工具审批 | `[COARA_APPROVAL]` / `[COARA_APPROVAL_REPLY]` | PC 推送审批卡片，App 点按钮回复 |
| 宝箱解锁 | `[COARA_VAULT]` / `[COARA_VAULT_REPLY]` | 密码不进入 LLM 对话 |
| 选项询问 | `[COARA_ASK_USER]` / `[COARA_ASK_USER_REPLY]` | ask_user 工具的远程交互 |
| 文本询问 | `[COARA_ASK_TEXT]` / `[COARA_ASK_TEXT_REPLY]` | 自由文本输入 |
| 工作空间动态 | `[COARA_UPDATES]` | PC 推送工作空间动态（未读数/摘要）到手机红点 |
| 模型/工作空间/思考同步 | `[COARA_MODELS]` / `[COARA_WORKSPACES]` / `[COARA_THINKING]` | 手机命令面板与思考状态同步 |
| 控制命令 | `/new` `/stop` `/restart` | 远程控制 coara |

完整协议见 [`../docs/MATRIX_APP_LINK.md`](../docs/MATRIX_APP_LINK.md)。

## 可靠性设计（matrix-nio / coara App 兼容）

面向个人规模，借鉴 Dendrite/Conduit 等生产 homeserver 的做法：

- **空安全的 `/sync` JSON** — 所有 `*.events` 数组为 `[]`，绝不返回 JSON `null`（matrix-nio 要求）
- **立即返回的 sync** — 有新事件时 `/sync` 不等长轮询超时立即返回
- **稳定的超时 token** — 空长轮询响应保持 `next_batch` 不变（客户端可安全重试）
- **房间级唤醒** — 只通知加入了受影响房间的客户端
- **脏房间增量同步** — 只组装 `since` 之后有时间线活动的房间
- **sync 写屏障** — `/sync` 等待在途时间线写入提交后再读；`Notify` 只在提交后触发
- **fail-closed `next_batch`** — token 绝不越过未投递给用户的事件（防止静默丢消息）
- **初始 `/sync` 不带历史时间线** — 冷启动/缺失 `since` 只返回房间状态 + 最新 token；历史走 `/messages`（防止 matrix-nio bot 重启后重放整个聊天）
- **发送事务去重** — `PUT .../send/.../{txnId}` 重放返回相同 `event_id`
- **SQLite WAL** — 并发 `/sync` 读 + 消息发送，写竞争更小（`journal_mode(WAL)` + `busy_timeout`）
- **Typing** — `m.typing` ephemeral 事件驱动 Android 端「对方正在输入」

拉取代码变更后重新编译并重启 `gomatrix.exe` 和 PC coara：

```powershell
cd gomatrix
go build -o gomatrix.exe ./cmd/gomatrix
```

## 数据库

单文件 SQLite，外键约束开启；写事务经互斥锁串行化（适合个人 1-5 客户端规模）。Schema 见 `internal/db/schema.sql`。

运行时数据与配置文件同目录，重启后保留：

开发机如需重置本地服务状态，先停止 GoMatrix，再删除 `gomatrix.db`、同目录的
`gomatrix.db-*` 和 `media/`；这些文件与构建产物均已被 Git 忽略，不能作为源码提交。
不要删除 `gomatrix.toml`，其中可能含本机隧道或 agent 配置。

- SQLite 数据库：`gomatrix.db`
- 上传的媒体文件：`./media/`

## 项目结构

```text
gomatrix/
├── cmd/gomatrix/main.go     # 入口（无头服务模式，由 coara 托管）
├── internal/
│   ├── api/                  # HTTP 路由 + 处理器
│   │   ├── apiutil/          # 认证上下文、错误、JSON 辅助
│   │   ├── client/           # Client-Server API 处理器
│   │   └── federation/       # Federation 桩处理器
│   ├── config/               # TOML 配置 + 环境变量覆盖
│   ├── connect/              # 手机扫码连接载荷
│   ├── dashboard/            # 配对 QR PNG / 状态 JSON 数据接口（页面已退役）
│   ├── db/                   # SQLite schema + 数据访问
│   ├── logging/              # 日志
│   ├── models/               # 共享数据模型
│   ├── service/              # 业务逻辑（用户、房间、时间线、sync、媒体、agent）
│   ├── tunnel/               # Cloudflare 隧道管理
│   └── utils/                # ID 生成、校验、内容类型
├── gomatrix.toml.example
└── go.mod
```

## 安全限制

GoMatrix 面向**个人/局域网**使用。暴露到公网前，注意以下限制（跟踪见 [`../docs/REMAINING_ISSUES.md`](../docs/REMAINING_ISSUES.md)）：

- **注册仅限配对票据** — 默认关闭注册；发布安装会开启注册，但配对数据接口每次生成 QR 时才签发一张 5 分钟、单次使用的票据。票据用后或过期即失效，切勿公开配对 QR。
- **agent 必须配置密码** — GoMatrix 不再提供默认 bot 密码；`gomatrix.toml` 的每个 `[[agents]]` 都必须配置随机高熵密码，发布安装器会自动生成。
- **数据接口仅限本机/局域网直连** — `/dashboard/qr.png`、`/api/dashboard/status` 只对 loopback/私网直连来源放行（独立 dashboard 页面已退役，配对二维码由 coara WebUI「手机」页代理展示）；经 Cloudflare tunnel 中继的请求（带 `CF-*` 头）一律 403，LAN IP、TunnelURL、配对 QR 不经 tunnel 公网可见。
- **登录/注册有限流** — `/login`、`/register` 按 来源 IP+账号 token bucket 限流，并叠加纯来源 IP 聚合桶防账号名轮换（注册洪水/密码喷洒），两桶都须通过（默认 login 10/min、register 5/min、IP 聚合 30/min、burst 3，`gomatrix.toml` 的 `[rate_limit]` 可调或关闭），超限回 429 + `Retry-After`（`M_LIMIT_EXCEEDED`）；经 Cloudflare tunnel 的请求按边缘写入的 `CF-Connecting-IP` 分桶（以客户端无法抑制的 `CF-Ray` 头为 tunnel 判据），伪造 XFF 最左值不能换桶；`/sync` 长轮询与其余端点不限流。
- **schema 有版本管理** — `PRAGMA user_version` 递增迁移框架，当前基线 version 1；无版本号的存量库自动标记为 1，版本高于二进制支持值时拒绝启动。
- **Federation 桩** — 密钥每次重启重新生成，`ReceiveTransaction` 不验签。Federation 默认关闭（`allow_federation = false`），保持关闭。
- **宝箱密码明文（已过渡缓解）** — `[COARA_VAULT_REPLY]` 侧信道把宝箱密码以明文放在 Matrix 消息体里。由于 sync 投递走读库回放无法完全不落盘，GoMatrix 采用定期清理：回信实时投递不受影响，超过保留窗口（5 分钟，大于解锁交互的 60s 等待窗口）即从事件表删除（timeline 行级联清除），启动时也会清一次存量。密码不进入 LLM 上下文，但投递窗口内仍明文落库，且 SQLite 空闲页/WAL 可能短期残留已删数据——彻底方案仍是端到端加密侧信道（REMAINING_ISSUES #2）。

## License

MIT
