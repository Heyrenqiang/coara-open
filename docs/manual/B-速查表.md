# 附录 B 速查表

日常高频操作的一页纸。每条命令都与当前代码核对过。

## B.1 启动

| 命令 | 效果 |
|------|------|
| `coara` | 确保常驻内核在跑，本终端作为 CLI 端接入对话（终端只是端，关掉窗口内核照常运行） |
| `coara tray` | 启动系统托盘常驻内核（右键：开 Web / 手机二维码 / 退出） |
| `coara daemon` | 无头常驻内核（无 pystray 时 tray 的退化形态） |
| `coara attach <空间名>` | 外挂 CLI：另开终端接入指定工作空间 |
| `coara -p <provider> -m <model>` | 指定本次使用的 provider 和模型 |
| `coara --workspace <路径>` | 指定工作空间目录（默认当前目录） |

> Web 与 Matrix 由内核托管，无需单独启动：浏览器访问 `http://127.0.0.1:8080`（默认端口），手机配对二维码在系统托盘。gomatrix 由 coara 自动拉起（`matrix.host_enabled` 默认开）。

## B.2 状态与查询

| 命令 | 效果 |
|------|------|
| `coara status` | 运行时状态 |
| `coara providers` | 已配置的 LLM provider |
| `coara llm-profiles` | 已配置的 LLM profile |
| `coara usage summary [--days N]` | 用量摘要（默认最近 7 天） |
| `coara usage session <session_id>` | 单会话用量 |
| `coara search <query>` | 搜索宝箱 `open/` 文件名（提示解锁；`--no-prompt` 配合 `COARA_VAULT_PASSWORD` 用于脚本） |

## B.3 工作空间与事项

| 命令 | 效果 |
|------|------|
| `coara ws list` | 列出已登记工作空间 |
| `coara ws add <路径> --name <名> [--summary 简介]` | 登记工作空间 |
| `coara ws rename <旧名> <新名>` | 重命名（路径与磁盘不动） |
| `coara ws remove <名> [--delete-disk]` | 取消登记（默认保留磁盘目录） |
| `coara ws default <名>` | 设为启动默认（不影响正在运行的会话） |

## B.4 宝箱、工作流、示例

| 命令 | 效果 |
|------|------|
| `coara vault status` | 初始化/锁定状态与已封存条数 |
| `coara vault init` | 设置主密码（≥8 位，仅 PC） |
| `coara vault unlock` | 校验密码（离线 CLI 不跨命令保活） |
| `coara vault lock` | 封存上锁（本进程无打开会话时为空操作） |
| `coara vault list / read <路径> / write / import <路径>` | 在 `open/` 下列、读、写、导入（同进程内提示解锁） |
| `coara vault passwd` | 修改主密码并重加密全部条目 |
| `coara workflow list` | 列出工作流草案与运行状态 |
| `coara examples install stocks-watch` | 安装内置示例项目 |

## B.5 聊天斜杠命令

| 命令 | 效果 |
|------|------|
| `/help` | 常用命令与快捷键 |
| `/new` | 新开一轮对话 |
| `/report` | 向开发者提交问题报告（附带本轮会话 → 开发者 webhook） |
| `/stop` | 中断当前回合（保留但不宣传；打断主走 Ctrl+C / 端内打断按钮） |
| `/status` | 状态摘要（模型、工作空间、偏好） |
| `/usage [N]` | 本轮或最近 N 天用量 |
| `/model` | 列出或切换模型 |
| `/ws` | 工作空间列表（输入 `/ws` 即可 ↑↓ 选择切换；也可 `/ws <序号>` / `/ws switch <名>`） |
| `/events` | 事件源（`/events reload` 热重载） |
| `/sandbox` | 切换沙箱 |
| `/thinking` | 思考模式 |
| `/theme` | CLI 配色主题（`/theme dark` / `/theme light`） |
| `/tools` | 可用 / 已隐藏工具 |
| `/vault` | 宝箱状态（`/vault lock` 上锁） |
| `/qrcode` | 在终端显示手机配对二维码 |
| `/message`、`/messages` | 查看全局系统消息历史（`/messages <N>` 看最近 N 条） |
| `/log` | 回看最近工具执行（`/log <n>` 展开；`--seq` / `--tool` 按录像带定位） |
| `/email` | 配置 / 查看邮箱（写入 COARA_EMAIL*，供邮件工具使用） |
| `/compact` | 手动压缩当前会话历史（LLM 摘要，被替代部分归档可回取） |
| `/login` | 邮箱验证码登录（Web 在跑时打开登录页，纯 CLI 走终端向导） |

## B.6 聊天里让它调用工具时

这些话术对应工具动作，列出来方便你理解它的行为（也可以直接说人话让它自己选）：

| 工具 | 动作 |
|------|------|
| `ws` | `list` / `add` / `remove` / `rename` / `switch`（登记生命周期） |
| `review` | `board` / `list` / `pending` / `stats` / `read` / `archive` / `dismiss` / `elevate` / `resolve` / `mark_read`（动态批复，janitor 专属） |
| `orchestrator` | `spawn`（登记节点）/ `run`（点火/提交引擎）/ `wait`（等收尾）/ `status`（看输出）/ `save`（从图自动投影落盘）/ `load`（恢复）/ `result`（查结果）/ `delete`（删草案）/ `update` / `edge` / `remove`（运行中调整） |
| `todo` | `read` / `add` / `adjust` / `remove` / `clear` |
| `skill` | `search` / `activate` |
| `vault` | `open` / `close` / `status` |
| `reminder` | `add_once` / `add_interval` / `add_cron` / `list` / `remove` |
| `plan_mode` | `enter` / `submit` / `exit` |

## B.7 常用路径速查

假设全局 Home 为 `D:\coara`：

| 数据 | 路径 |
|------|------|
| 配置文件 | `D:\coara\system\`（`config.yaml`、`providers.yaml`、`.env`） |
| 工作空间登记 | `D:\coara\registry\workspaces.yaml` |
| 事件源定义 | `D:\coara\users\default\matters\definitions\*.yaml` |
| 工作空间动态 | `D:\coara\users\default\inbox\{名字}\` |
| 工作流草案 | `D:\coara\users\default\workflows\drafts\{草案id}.json` |
| 宝箱 | `D:\coara\users\default\assets\vault\`（`sealed/`、`open/`、`vault.meta.json`） |
| 在线绑定 | `D:\coara\runtime\active.json` |
| trace | `D:\coara\workspaces\{workspace_id}\traces\trace_events.jsonl` |
| 文本日志 | `D:\coara\workspaces\{workspace_id}\logs\coara.log` |
| 错误日志 | `{工作空间}\.coara\logs\errors.jsonl` |
| 待办 | `{工作空间}\.coara\todos\{session_id}.json` |

## B.8 决策表

**这事该让谁干？**

| 场景 | 推荐路径 |
|------|----------|
| 单文件查询/小改 | Root 直接用工具 |
| 多路并行找资料 | 委派多个 coaras（任务书写明只读） |
| 独立工程子任务 | 委派 coaras |
| 已登记工作空间的定时巡检/维护 | 配 cron 事件源（`handle: janitor` 让管家过目，或直启工作流） |
| 多步骤、有依赖、要质检 | 工作流 |

**事件来了怎么办？（事件源 `salience` / `handle`）**

事件内容无条件进工作空间动态收件箱；下面两个字段决定曝光与处置：

| 配置 | 行为 | 适用 |
|------|------|------|
| `handle: park`（默认） | 挂住等用户，不打扰 Root | 大多数事件 |
| `handle: janitor` | 叫醒管家 janitor 过目（勾掉/呈阅/自处理，写处置轨迹） | 需要先有人筛一遍的事件 |
| `ttl_seconds` | 消息保质期，过期未读自动勾掉 | 低价值通知 |
| `salience: high` | 上浮跨空间前台待处理视图（`review(action=pending)`） | 需要尽快被看见的事件 |

旧 `trigger_mode` 字段已废弃并被忽略（处置缺省 `handle: park`）。不再有「事件自动叫醒主会话跑回合」。

## B.9 故障排查线索

| 现象 | 先看哪里 |
|------|----------|
| 启动报"找不到 LLM 配置" | `<coara_home>\system\providers.yaml` 是否存在；模板复制了没有 |
| 提示缺 API Key | `system\.env` 里变量名是否与 `api_key_env` 一致 |
| LLM 不调用工具 | 该模型 `function_calling` 是否为 true |
| 工具调用老被弹窗/拒绝 | `config.yaml` 的 `security.call_policy` |
| 文件工具报路径错误 | 必须用绝对路径，且在工作空间内 |
| Matrix 连不上 | 托盘「手机连接」二维码能否弹出；homeserver 地址、账号密码是否正确 |
| Web UI 打不开 | 端口被占？改 `COARA_WEB_PORT`（默认 8080）；访问 `http://127.0.0.1:8080` |
| 宝箱解不开 | 密码错会明确提示；脚本场景设 `COARA_VAULT_PASSWORD` |
| 后台任务完成没动静 | 是否切换过工作空间（跨工作空间保护会跳过注入，任务列表里能查到） |
| 工作流卡住 | 节点是否在等上游（扇入未齐）或已触激活上限标 failed；Web /workflow 页看状态与结果 |
| 配置改了不生效 | 多数配置需重启；确认改的是 `system\config.yaml`（加载顺序见 [第 19 章](./19-配置系统.md)） |

## B.10 资源上限速查

| 资源 | 上限 |
|------|------|

| 子智能体工具迭代 | 1500 轮（Root 1200 轮） |
| 审批弹窗超时 | 5 分钟 |
| 宝箱空闲自动封存 | 约 120 秒（仅 `open/` 内活动续期） |
| EventBus 待处理订阅任务 | 256 |
| trace 详情文件 | 5000 条（超出裁剪最旧 20%）；JSONL 超 50MB 轮转 |
| 分身 / 后台任务持久化记录 | 各 500 条 |
| 单工作空间动态 | 500 条 |
| 工作流终态实例 | 30 天后归档 |

## B.11 章节索引

| 主题 | 章节 |
|------|------|
| 整体架构 | [第 1 章 系统总览](./01-系统总览.md) |
| 启动流程 | [第 2 章 启动与初始化](./02-启动与初始化.md) |
| 对话机制 | [第 3 章 对话与轮次](./03-对话与轮次.md) |
| 工具 | [第 4 章 工具系统](./04-工具系统.md) |
| 子智能体 | [第 5 章子智能体与委派](./05-子智能体与委派.md) |
| 工作流 | [第 6 章 工作流系统](./06-工作流系统.md) |
| 工作空间 | [第 7 章 工作空间与多工作空间](./07-工作空间与多工作空间.md) |
| 事项 | [第 8 章 事项与日程](./08-事项与日程.md) |
| 事件源 | [第 9 章 事件源与工作空间动态](./09-事件源与工作空间动态.md) |
| 宝箱 | [第 10 章 宝箱](./10-宝箱.md) |
| 技能 | [第 11 章 技能系统](./11-技能系统.md) |
| 待办 | [第 12 章 待办系统](./12-待办系统.md) |
| 上下文压缩 | [第 13 章 上下文窗口与压缩](./13-上下文窗口与压缩.md) |
| 提示词 | [第 14 章 提示词系统](./14-提示词系统.md) |
| 观测 | [第 15 章 观测与追踪](./15-观测与追踪.md) |
| CLI/Web | [第 16 章 CLI 与 Web UI](./16-CLI与WebUI.md) |
| 远程前端 | [第 17 章 远程前端](./17-远程前端.md) |
| 安全 | [第 18 章 安全与治理](./18-安全与治理.md) |
| 配置 | [第 19 章 配置系统](./19-配置系统.md) |
| 存储 | [第 20 章 存储布局](./20-存储布局.md) |
| 术语 | [附录 A 术语表](./A-术语表.md) |

---

**上一章**：[附录 A 术语表](./A-术语表.md)
**返回首页**：[coara v8 系统运行说明书](./README.md)
