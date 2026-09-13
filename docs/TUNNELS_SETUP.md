# 隧道说明（Matrix 不动 + Windows 新增第二条 webhook）

把本机 coara 的 webhook 接收服务（默认 `127.0.0.1:8765`）经 cloudflared 暴露到公网，让云端后端能把反馈 POST 进来。**不碰** Ubuntu 上已有的 Matrix 隧道。

配套文档：事件源配置与分流见 [WORKSPACE_EVENTS_IMPLEMENTATION.md](./WORKSPACE_EVENTS_IMPLEMENTATION.md)；脚本说明见 [`scripts/tunnels/README.md`](../scripts/tunnels/README.md)。

## Webhook 数据流

```text
手机 App → 云端后端 POST /api/feedback →（后端既有逻辑保留）
                              └→ POST https://<webhook隧道域名>/webhook/<source_id> → 本机 coara:8765
```

| 组件 | 位置 | 公网暴露 |
|------|------|----------|
| Matrix（聊天） | 本机 GoMatrix 或 Ubuntu 上的既有服务 | 各自既有 cloudflared（**不动**） |
| 云端后端 | 阿里云 | 已有 API |
| coara webhook | 本机 Windows `:8765` | **第二条** cloudflared（本文） |

一条 Quick Tunnel 只能指向一个内网 URL，所以 webhook 需要独立的一条。

## 先澄清三件事

### 1. 仓库有没有在 Cloudflare 替你「建好」隧道？

**没有。** 仓库里只有脚本和文档，无法登录你的 Cloudflare 账号。Ubuntu 上 Matrix 那条是你原有的，脚本不会改它。

### 2. 域名是临时的还是固定的？

| 模式 | 域名 | 重启 cloudflared 后 |
|------|------|---------------------|
| **Quick Tunnel**（默认） | `https://xxxx.trycloudflare.com`，临时 | **会变** → 脚本自动 SSH 改云端后端的 `COARA_WEBHOOK_URL` |
| **Named Tunnel**（可选） | 你自己域名的固定子域 | 不变；需在 Zero Trust **另建一条**（不要改 Matrix 那条），并填 `COARA_WEBHOOK_TUNNEL_TOKEN` |

### 3. 一共有几条隧道？

```text
Ubuntu（已有，勿动）   hiclaw-cloudflared → Matrix 服务      → Matrix / coara App
Windows（脚本新建）   coara-webhook      → 127.0.0.1:8765   → 反馈 webhook
```

## 一次性配置（Windows）

```powershell
cd D:\code_ws\v8\scripts\tunnels
copy tunnels.local.env.example tunnels.local.env
notepad tunnels.local.env
```

`tunnels.local.env` 关键项（完整注释见 `tunnels.local.env.example`）：

| 键 | 说明 |
|----|------|
| `WEBHOOK_TUNNEL_MODE` | `quick`（默认）/ `named` |
| `COARA_WEBHOOK_SOURCE_ID` | 事件源 id，拼出完整 URL `…/webhook/<source_id>` |
| `COARA_WEBHOOK_SECRET` | webhook 令牌；须与事件源 YAML 的 `webhook_secret` 一致 |
| `COARA_HOME` / `COARA_REPO` | coara Home 与仓库路径 |
| `ALIYUN_SSH` | 云端后端 SSH（如 `root@x.x.x.x`） |
| `ALIYUN_BACKEND_ENV` | 后端 `.env` 路径（默认示例 `/opt/xuan_android_local/xuan-backend/.env`） |
| `ALIYUN_RESTART_BACKEND` | `1` = 同步后重启后端（`./start.sh restart`） |
| `FETCH_MATRIX_URL_FROM_UBUNTU` / `MATRIX_SSH` / `MATRIX_CLOUDFLARED_CONTAINER` | 只读 Ubuntu 日志取 Matrix 临时域名 |
| `START_WEBHOOK_TUNNEL` / `SYNC_ALIYUN_ENV` / `START_COARA_CLI` / `UPDATE_LOCAL_MATRIX_ENV` / `OPEN_MATRIX_QR` | 各步骤开关 |
| `COARA_WEBHOOK_PUBLIC_URL` / `COARA_WEBHOOK_TUNNEL_TOKEN` | 仅 named 模式需要 |

启用事件源：把 `events/xuan-feedback-webhook.yaml.example` 复制到 `<workspace>/.coara/matters/definitions/xuan-feedback-webhook.yaml`，改 `enabled: true` 和 `webhook_secret`。

> 脚本启动时若该文件不存在，会自动把示例复制到上述目录（`EventSourceManager` 的唯一加载目录，见 [WORKSPACE_EVENTS_IMPLEMENTATION.md](./WORKSPACE_EVENTS_IMPLEMENTATION.md) §9），你只需编辑其中的 `enabled` 和 `webhook_secret`。

安装 cloudflared（Windows）：<https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>

首次运行：

```powershell
D:\code_ws\v8\scripts\tunnels\Start-AllTunnels.ps1
```

## 脚本做了什么（Start-AllTunnels.ps1）

按顺序执行四步，每步可用 env 开关或参数关掉（`-SkipCoara` / `-SkipAliyun` / `-NoQr`）：

1. **启动 webhook 隧道**（`START_WEBHOOK_TUNNEL=1`）
   - quick 模式：`cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765`，从日志里抓 `trycloudflare.com` 域名（最多等 45 秒）
   - named 模式：`cloudflared tunnel run --token <COARA_WEBHOOK_TUNNEL_TOKEN>`
   - 把 `base=`、`webhook=`（完整 URL）、时间戳写入 `{COARA_HOME}/system/logs/tunnels/webhook-public-url.txt`
   - 幂等：pid 文件显示隧道已在跑且能读到 URL 时直接复用，不重复起进程
2. **同步云端后端**（`SYNC_ALIYUN_ENV=1`，`-SkipAliyun` 可关）：SSH 到 `ALIYUN_SSH`，把 `COARA_WEBHOOK_URL=<base>/webhook/<source_id>` 和 `COARA_WEBHOOK_TOKEN=<COARA_WEBHOOK_SECRET>` 写入 `ALIYUN_BACKEND_ENV`（有则改、无则增）；`ALIYUN_RESTART_BACKEND=1` 时重启后端
3. **读取 Matrix 地址**（`FETCH_MATRIX_URL_FROM_UBUNTU=1`）：SSH 到 Ubuntu 执行 `docker logs hiclaw-cloudflared | grep trycloudflare`（**只读**，不重启任何东西）；`UPDATE_LOCAL_MATRIX_ENV=1` 时把它写进本机仓库 `.env` 的 `COARA_MATRIX_HOMESERVER`；`OPEN_MATRIX_QR=1` 时打开 App 扫码页（二维码图由 GoMatrix 自己的 `/dashboard/qr.png` 提供，不经过第三方）
4. **启动 coara**（`START_COARA_CLI=1`，`-SkipCoara` 可关）：新开 PowerShell 窗口在 `COARA_REPO` 下运行 `coara`

cloudflared 的日志和 pid 文件都在 `{COARA_HOME}/system/logs/tunnels/`。

## 电脑重启后

一条命令（Windows）：

```powershell
D:\code_ws\v8\scripts\tunnels\Start-AllTunnels.ps1
```

| 会做 | 不会做 |
|------|--------|
| 启动第二条 cloudflared（webhook → `127.0.0.1:8765`） | 不改 Ubuntu Matrix 的 `hiclaw-cloudflared` |
| quick 模式下 SSH 更新云端 `COARA_WEBHOOK_URL`（域名可能已变） | 不重启 Matrix 服务 |
| 只读 Ubuntu 日志拿 Matrix 当前地址 | 不在 Cloudflare 替你建隧道 |
| 可选：更新本机 `.env`、打开 App 扫码页、启动 coara | |

**域名变化**：quick 模式下 Windows webhook 隧道重启可能变 URL → 脚本自动 SSH 改云端。Matrix 域名仅当 Ubuntu 上 Matrix cloudflared 重启时才变，那时重跑本脚本刷新 `.env` 和二维码即可。

**开机自动**（可选，当前用户、无需管理员）：

```powershell
powershell -File D:\code_ws\v8\scripts\tunnels\Register-TunnelAtLogon.ps1
```

注册一个登录计划任务 `CoaraTunnelsAtLogon`，登录时以 `-SkipCoara` 跑主脚本（起隧道 + 同步云端，不自动启动 coara）。

## 可选：webhook 固定域名（named）

1. 在 Cloudflare Zero Trust **新建一条**隧道（不要改 Matrix 那条）
2. Public hostname 指向 `http://127.0.0.1:8765`
3. `tunnels.local.env`：`WEBHOOK_TUNNEL_MODE=named`，填 `COARA_WEBHOOK_PUBLIC_URL` 和 `COARA_WEBHOOK_TUNNEL_TOKEN`

named 模式下 URL 不变，`SYNC_ALIYUN_ENV` 只需成功跑一次。

## 注意事项

- **端口对齐**：脚本把隧道指向固定的 `127.0.0.1:8765`。如果你在 `config.yaml` 的 `events.webhook_port` 改了端口，需要同步改脚本的本地 URL（`TunnelCommon.ps1` 的 `Start-WebhookTunnel`）
- **令牌一致**：`tunnels.local.env` 的 `COARA_WEBHOOK_SECRET`、事件源 YAML 的 `webhook_secret`、云端 `COARA_WEBHOOK_TOKEN` 三处必须相同
- **不要**在 Ubuntu 上覆盖现有 Matrix 隧道配置，**不要**停掉 `hiclaw-cloudflared`
- **不要**让 App 直连 coara；链路仍是 App → 云端后端 → webhook
- 只同步云端不重启隧道：`scripts/tunnels/Sync-AliyunWebhook.ps1`

## 文件

| 文件 | 说明 |
|------|------|
| `scripts/tunnels/Start-AllTunnels.ps1` | 主入口（webhook 隧道 + 云端同步 + Matrix 只读 + coara） |
| `scripts/tunnels/TunnelCommon.ps1` | 公共函数（被其它脚本引用） |
| `scripts/tunnels/Sync-AliyunWebhook.ps1` | 仅同步云端 `COARA_WEBHOOK_URL` |
| `scripts/tunnels/Register-TunnelAtLogon.ps1` | 注册登录自启计划任务 |
| `scripts/tunnels/tunnels.local.env.example` | 配置模板（复制为 `tunnels.local.env`，已 gitignore） |
| `{COARA_HOME}/system/logs/tunnels/` | cloudflared 日志、pid、`webhook-public-url.txt` |
