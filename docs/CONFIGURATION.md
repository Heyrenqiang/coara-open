# coara v8 配置参考

> **什么时候需要这篇文档**：要改 API key / 模型、调全局行为（Matrix、审批、压缩）、或排查「配置改了没生效」时。日常聊天不需要读它。
>
> 最小可运行链路：`.env`（API Key） + `providers.yaml`（模型）→ `coara`
>
> **最终用户默认配置唯一真相**：[`deploy/gitee/templates/`](../deploy/gitee/templates/)（进安装包，由 `install.ps1` / `install.sh` 在缺失时写入 `COARA_HOME`）。
> **开发机 vs 用户机隔离**：[`DEV_VS_USER.md`](./DEV_VS_USER.md)。
> 仓库根 `*.example` 仅供 editable 开发参考，**不会**被 Build-Release 拷进用户包。
>
> **与用户手册的关系**：[`manual/19-配置系统.md`](./manual/19-配置系统.md) 讲配置系统的概念与加载机制（面向理解）；本文是**逐键速查表**（面向改配置），全部默认值已对照代码核实。

**开发机首次配置（与用户包同一套默认）：**

```powershell
$env:COARA_HOME = "$env:USERPROFILE\coara"
mkdir $env:COARA_HOME\system -Force
copy deploy\release\templates\providers.yaml $env:COARA_HOME\system\providers.yaml
copy deploy\release\templates\config.yaml $env:COARA_HOME\system\config.yaml
# .env 无需手动创建：首次启动 coara 的交互向导会写入（非交互环境走 WebUI Providers 面板）
```

测「新用户」体验时用空目录，例如 `$env:COARA_HOME = "$env:USERPROFILE\coara-fresh-test"`。

本机配置 gitignore 模板：[`templates/coara-config.gitignore`](./templates/coara-config.gitignore)。

**首次启动 API key 向导**：启动时若没有任何可用 API key（空值或 `your-key` / `sk-xxx` / `changeme` 等占位符视为未配置），CLI 进入交互式向导引导填写；若默认 provider 没配 key 但其他 provider 配了，会自动把默认对齐到可用的 provider 并持久化，不弹窗（`src/cli/first_run_setup.py`）。

---

## 目录

1. [文件位置与加载顺序](#一文件位置与加载顺序)
2. [.env — 环境变量](#二env--环境变量)
3. [providers.yaml — LLM 提供商](#三providersyaml--llm-提供商)
4. [config.yaml — 全局行为](#四configyaml--全局行为)
5. [CLI 参数（临时覆盖）](#五cli-参数临时覆盖)
6. [可配置项总览（统一管理入口）](#六可配置项总览统一管理入口)
7. [工作空间数据目录](#七工作空间数据目录)
8. [按场景：最小配置](#八按场景最小配置)
9. [常见坑](#九常见坑)

---

## 一、文件位置与加载顺序

配置文件都在 `<coara_home>/system/` 下：

```text
<coara_home>/system/
  .env
  providers.yaml
  config.yaml
<coara_home>/users/default/
  config.yaml           # 可选用户级覆盖（在 system/ 之后加载）
  llm_preferences.yaml  # /model --global 写入；最后加载，优先级最高
```

**YAML 加载顺序**（深度合并：同名字典键递归合并，后者覆盖前者；`src/core/config.py`）：

1. `<coara_home>/system/providers.yaml`、`<coara_home>/system/config.yaml`
2. `<coara_home>/users/default/config.yaml`（用户级覆盖）
3. **仅开发机**：源码仓根 `providers.yaml` / `config.yaml`（仓根 = 向上能找到 `pyproject.toml` 的目录；**会盖过** COARA_HOME，测用户体验时勿依赖）
4. `llm_preferences.yaml`（`/model --global` 写入）

**`.env` 加载顺序**：先 `system/.env`，再（仅开发机）仓根 `.env`（`override=True`，后者赢）。

**`coara_home` 解析顺序**（`src/core/coara_home.py` + `src/core/config.py`）：

1. 已合并配置中的 `coara_home` 字段
2. **仅开发机**：仓根存在 `providers.yaml` 时，运行时 home 落到**仓根**
3. 环境变量 `COARA_HOME`（Windows 上还会读注册表里的用户级 / 机器级 `COARA_HOME`；多个候选时优先选下面已有 `providers.yaml` 的那个）
4. 以上都没有：`<cwd>/.coara`（单工作空间试用模式）

**运行期写回位置**（WebUI 设置保存等写配置时）：`config.yaml` 与 `providers.yaml` 同一优先级——优先 `<coara_home>/system/` 下对应文件；不存在时写仓根同名文件，再退化到 `<cwd>/.coara/`（会创建目录）。两个文件的写回位置都保证落在读取链上（2026-08 修复过 providers 写不到读取链的缺陷）。

**`llm_preferences.yaml` 的实际位置**（`/model` 读写同一路径）：若 `<cwd>/.coara/llm_preferences.yaml` 已存在则用它，否则用 `<coara_home>/users/default/llm_preferences.yaml`。

**缺少 providers.yaml 会直接报错**：`system/providers.yaml` 和仓根 `providers.yaml` 都不存在时抛 `ConfigError`，不是静默降级。

**用户安装包**没有仓根 → 只有 `COARA_HOME`，行为干净。

---

## 二、`.env` — 环境变量

`.env` 无需手动创建：首次启动 coara 时若没有可用 key，交互向导会引导粘贴并自动写入（非交互环境走 WebUI 配置页 Providers 面板）。`deploy/gitee/templates/env.example` 与仓根 `.env.example` 仅为可用变量参考。

| 变量 | 必填 | 说明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✓ 至少一个 | 默认用户模板对应 MiniMax（`providers.yaml` → `minimax`，Anthropic 兼容端点） |
| `AGNES_API_KEY` | 可选 | Agnes（`providers.agnes`）。国际站 Key：platform.agnes-ai.com；国内站 Key：platform.agnes-ai.cn（两套不互通） |
| `AGNES_BASE_URL` | 可选 | 覆盖 Agnes Base URL。未设置时跟 `providers.yaml`，否则默认 `https://apihub.agnes-ai.cn/v1` |
| `DEEPSEEK_API_KEY` | 可选 | DeepSeek（provider `deepseek`，Responses API 驱动；模型 `deepseek-flash`） |
| `KIMI_API_KEY` | 可选 | Kimi（provider `kimi`，Anthropic 兼容端点；模型 `k3`） |
| `ZHIPU_API_KEY` | 可选 | 智谱 GLM（provider `zhipu`，Coding Plan **Responses** 端点 `…/api/v1`；模型 `glm-5.3`） |
| `OPENAI_API_KEY` | 可选 | OpenAI 系 provider 用 |
| `COARA_MATRIX_HOMESERVER` | 跨端时 | 默认 `http://127.0.0.1:8008`（本机 GoMatrix） |
| `COARA_MATRIX_USER` | 跨端时 | 默认 `@coara:coara.local` |
| `COARA_MATRIX_PASSWORD` | 跨端时 | 默认 `coara-bot-password` |
| `COARA_MATRIX_NOTIFY_ROOM` | 可选 | 事件推送到手机的房间 ID（对应 `matrix.notify_room_id`） |
| `COARA_HOME` | 可选 | 全局数据根目录（替代配置里的 `coara_home` 字段） |
| `COARA_WEB_PORT` | 可选 | 内嵌 Web UI 端口，默认 `8080` |
| `COARA_WAKE_SCAN_INTERVAL` | 可选 | 工作流唤醒扫描间隔（秒），默认 `5.0` |
| `COARA_VAULT_PASSWORD` | 可选 | 宝箱主密码；coara 启动时自动解锁（密码不进 LLM 上下文） |
| `COARA_TOKENIZER_DIR` | 可选 | 本地分词器目录（token 计数用） |
| `COARA_DEBUG_PROMPT` | 可选 | 调试模式：把最终发给模型的 prompt 落盘到 traces |
| `DOUBAO_API_KEY` | 可选 | 豆包搜索（Volcengine）provider key（`web_search` 自动模式候选之一；500 次/月免费额度） |
| `COARA_EMAIL` 等 `COARA_EMAIL_*` | 可选 | **内置邮件挂起工具**（`email`，经 `tool(action="activate")` 装载）自动登录用：`COARA_EMAIL`、`COARA_EMAIL_PASSWORD`、`COARA_EMAIL_IMAP_SERVER`（默认 `imap.qq.com`）、`COARA_EMAIL_SMTP_SERVER`（默认 `smtp.qq.com`）、`COARA_EMAIL_SMTP_PORT`（默认 `465`）；启动时读入进程内存，不写磁盘 |

> 占位符不算配置：空值、`your-key`、`sk-xxx`、`changeme` 等会被首次启动向导当作「未配置」。

---

## 三、`providers.yaml` — LLM 提供商

结构以用户模板 [`deploy/gitee/templates/providers.yaml`](../deploy/gitee/templates/providers.yaml) 为准（含 `deepseek` / `kimi`(k3) / `zhipu` / `minimax` / `agnes` 和完整 `llm_profiles`）：

```yaml
default_profile: agent.main
default_provider: minimax
default_model: MiniMax-M3

providers:
  minimax:
    driver: anthropic          # anthropic | openai | responses 协议
    base_url: "https://api.minimaxi.com/anthropic"
    api_key_env: "ANTHROPIC_API_KEY"
    max_tokens: 131072
    default_model: "MiniMax-M3"
    models:
      default: "MiniMax-M3"
      available:
        - id: "MiniMax-M3"
          name: "MiniMax M3"
          function_calling: true
          max_tokens: 131072
          temperature: 1.0

llm_profiles:
  agent.main:
    provider: minimax
    model: MiniMax-M3
    max_tokens: 131072
    temperature: 1.0
```

| 字段 | 说明 |
|------|------|
| `coara_home` | 全局数据目录（写在任一 YAML 均可）；也可用 `COARA_HOME` 环境变量 |
| `default_provider` / `default_model` / `default_profile` | 默认选择；只有一个 provider 时 `default_provider` 可省略（自动选中），`default_model` 缺省取该 provider 的 `models.default` |
| `providers.<name>` | 任意名称，需含 `driver`（`anthropic`/`openai`/`responses`——responses 为 OpenAI Responses API，当前用于 DeepSeek）、`base_url`、`api_key_env`、`models` |
| `models.default` / `default_model` | 默认模型 ID（`default_model` 缺省取 `models.default`） |
| `models.available[]` | 可选模型清单（`/model` 列表来源），每条可带 `max_tokens` / `temperature` |
| `security.call_policy` | 调用层确认策略（一般写在 `config.yaml`；写在 providers.yaml 也会被合并——用户模板就写在这里，见 §4.7） |

provider 必须写在嵌套 `providers:` 下；YAML 根级的裸 `minimax:` 写法**不会**被识别。

### 3.0 Agnes Endpoint

| 站点 | Base URL | Key |
|------|----------|-----|
| 国际站（默认） | `https://apihub.agnes-ai.cn/v1` | 国际站 Key |
| 国际站（备选） | `https://apihub.agnes-ai.com/v1` | **同一把**国际站 Key |
| 国内站 | `https://api.agnes-ai.cn/v1` | 国内站 Key（与国际站不互通） |

- 模板默认：`providers.agnes.base_url: https://apihub.agnes-ai.cn/v1`
- 图/视频 skill CLI 自动跟随 `providers.yaml`（可用 `AGNES_BASE_URL` 覆盖）
- Key 与 Endpoint 必须同站；改完重启 coara

### 3.0.1 DeepSeek（Responses API 驱动）

用户模板默认已启用 deepseek 段；关键字段：

```yaml
  deepseek:
    driver: responses                 # OpenAI Responses API（deepseek-flash）
    base_url: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
    default_model: "deepseek-flash"
    models:
      default: "deepseek-flash"
      available:
        - id: "deepseek-flash"
          name: "DeepSeek V4.1 Flash"
          function_calling: true
          vision: true
          max_tokens: 393216          # 官方最大输出 384K
          temperature: 1.0
```

- 官方规格（[模型 & 价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)）：上下文 **1M**、最大输出 **384K**、原生多模态、思考默认开、Responses / Tool Calls 支持
- 无状态 API：每次全量发送 input；tools 仅 function；图片经 Files API
- 思考：`/thinking on|off|low|medium|high` → `reasoning.effort = none/low/high/max`
- 工作空间 `/model` 换到 deepseek-flash 时，按目标型号取 `max_tokens`（384K），不沿用上一模型 profile 里的值
- 漏写 `driver` 时，`api.deepseek.com` 仍推断为 `responses`

### 3.1 LLM profiles（连接 vs 消费方）

coara 将**连接**（`providers:`，怎么连）与**消费方**（`llm_profiles:`，谁来用）分离。应用代码经 `LLMService` + profile 名发起请求。profile 支持 `inherit: <另一个 profile>` 继承后覆盖单字段（禁止循环继承）。

| Profile | 用途 |
|---------|------|
| `agent.main` | 主对话（CLI `-p`/`-m` 与 `/model` 切换的就是它） |
| `agent.<name>` | 按模型/场景的命名档案（用户模板带 `agent.minimax`、`agent.minimax_m3`、`agent.deepseek`、`agent.kimi`、`agent.zhipu`、`agent.agnes`） |
| `agent.web_search` | `web_search` 工具内决策 |
| `context.compression` | `ContextWindowManager` 上下文压缩 |
| `workflow.node` | 工作流节点（引擎子进程内节点智能体 LLM） |

- 未配置 `llm_profiles` 时，由 `default_provider` / `default_model` 合成一套等价 profile（`src/llm/profile_resolver.py` 的 `build_default_profiles`）。
- CLI 查看：`coara providers` · `coara llm-profiles`。
- 代码：`src/llm/service.py`、`src/llm/profile_resolver.py`、`src/llm/call_defaults.py`。

#### 每次调用的 `max_tokens` / `temperature` 怎么定

单次 LLM 调用按以下顺序取值（每个字段取第一个非空；`profile_resolver.py` + `call_defaults.py`）：

1. 调用方显式传入的参数
2. profile 里写的 `max_tokens` / `temperature`
3. 按 provider+model 的「最佳默认」：`providers.<name>.models.available[]` → 内置按模型表 → 同 provider 的 `llm_profiles.agent.*`（优先精确匹配 model）→ 内置按 provider 表 → `providers.<name>.max_tokens`
4. provider 实例默认；temperature 最终兜底 `0.7`

**硬上限钳制**只在厂商 API 明确拒绝更大值时生效：目前只有 Agnes（≤ 65536，`AGNES_MAX_OUTPUT_TOKENS`）。MiniMax / Kimi 等保持各自高上限。

**`/model` 切换模型**：空间 `/model <选择>` 写 `registry/workspaces.yaml` 该条目 `provider`/`model`（空间 last-run，见 [多工作空间与工作空间动态.md](./多工作空间与工作空间动态.md) §3.2.1），同时切该空间会话；**共看该空间的各端 chrome 立刻同步**。`/model --global` 写 `llm_preferences.yaml`（`default_provider` + `default_model` + `llm_profiles.agent.main`）。两者都**不改** `config.yaml` / `providers.yaml`；写入时按**目标** provider+model 解析最佳 `max_tokens` / `temperature`，不会把上一家（如 MiniMax 131K）带到新模型（如 Agnes）。

### 3.2 会话空闲自动 `/new`

```yaml
session:
  idle_timeout_seconds: 7200   # 2 小时无用户消息后自动新会话；0 = 关闭
  idle_check_interval_seconds: 60
  # janitor 模型不配置：恒跟随空间 last-run（最后一次正常对话运行的模型）
  janitor_min_interval_seconds: 600   # 每空间 janitor 最小间隔；窗口内触发只推迟不丢弃；0 = 关闭冷却
```

| 端 | 计时 | 默认 | 行为 |
|----|------|------|------|
| CLI / Root | 距上次**用户消息**（CLI / Web / Matrix 任一前端） | 7200s | **唯一**发起 `start_new_session`；CLI 提示 `[auto /new]`；经 `[COARA_STATUS]` 推送时钟 |
| Android | 跟随 Root 的 `last_user_activity_at`（本地镜像） | 同 2h | **不**自行发 `/new`；本地看似超时只拉 status；`session_event=new_session` 推送时画「新会话」分隔线，`workspace_switch` 推送时画「已切换到工作空间 X」分隔线 |
| Web UI | 无独立计时 | — | 走 Root |

三端共用 Root 上的 `_last_user_activity_at`；手机不得仅凭房间可见消息时间静默 `/new`（否则 CLI 活跃时会被误杀）。详见 [`Android远程控制与输入交互.md`](./Android远程控制与输入交互.md)。

---

## 四、`config.yaml` — 全局行为

复制 `deploy/gitee/templates/config.yaml` → `<coara_home>/system/config.yaml`（全部可选，不配也有默认值）。用户模板只含 matrix / events / reminders / dashboard / output_truncation / runtime_enhancements / security 的最小集；仓根 `config.yaml.example` 是更全的开发参考。

### 4.1 顶层通用项

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `default_provider` | 自动推断 | 只有一个 provider 时自动选中 |
| `default_model` | 自动推断 | 缺省取默认 provider 的 `models.default` |
| `coara_home` | — | 全局数据目录；也可用 `COARA_HOME` 环境变量 |
| `vault_enabled` | `true` | 是否启用宝箱（`vault` 工具）；主密码见 `.env` 的 `COARA_VAULT_PASSWORD` |
| `skills_enabled` | `true` | 是否自动加载技能 |
| `records.enabled` | `true` | agent 笔记写入总开关（janitor/daily + daily 调度）；false 不建 agent 子树。用配置助手改，改完重启内核。用户收藏不受影响 |
| `records.default_ttl_days` | `180` | agent 笔记默认归档参考天数（MVP 写入元数据） |
| `records.daily_curator_enabled` | `true` | 是否启用零点 `daily`（需 `records.enabled`） |
| `records.curator_timezone` | `Asia/Shanghai` | 零点 / 「昨日」日历所用时区 |
| `records.daily_provider` | — | daily 专用 provider；省略跟随**全局默认**（llm_preferences），不跟工作空间绑定 |
| `records.daily_model` | — | daily 专用模型；省略取 provider 默认模型 |
| `skills.default_include` | `[]` | Dashboard 推荐勾选（`/api/v1/skills` 的 recommended 标记）；**不**写入 system prompt |
| `skills.default_exclude` | `[]` | 从 default 推荐中排除 |

| `log_level` | `"INFO"` | 日志级别 |
| `tools.disabled` | `[]` | 主会话工具开关：列表内工具不注入 prompt 且执行被拒（实验/裁剪用，如 `disabled: [edit]`） |
| `detached_tool_remind_seconds` | — | ~~已移除~~：前台工具超时转并行的分离机制已废弃（超时即失败，无并行） |
| `detached_tool_max_seconds` | — | ~~已移除~~：同上 |

### 4.2 `context_compression`

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `threshold` | `0.80` | 触发压缩的上下文占用比例 |
| `preserve_ratio` | `0.30` | 保留最近消息的比例 |
| `min_compressible_fraction` | `0.05` | 最小可压缩比例 |

### 4.3 `matrix`

GoMatrix 由 coara 托管（`matrix.host_enabled` 默认开）：启动 coara 时端口上已有健康实例则接入（adopt），否则自动拉起捆绑的 `gomatrix.exe`（spawn），无需手动启动；拉不起来会跳过 Matrix 并在 WebUI「手机」页显示状态。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `server_name` | `"coara.local"` | GoMatrix 服务器域名（用于 Matrix 用户 ID：`@user:server_name`） |
| `port` | `8008` | GoMatrix HTTP 端口 |
| `homeserver` | 空 → `http://127.0.0.1:<port>` | Matrix 服务器 URL；空值或占位符自动回退本机地址 |
| `user` | 空 → `@coara:<server_name>` | 机器人账号 |
| `password` | 空 → `"coara-bot-password"` | 密码 |
| `notify_room_id` | — | 事件/提醒/动态推送目标房间（可选）；也可用 `COARA_MATRIX_NOTIFY_ROOM` |

**优先级**：`COARA_MATRIX_*` 环境变量 > `config.yaml` > 内置默认值（`src/cli/product_defaults.py`，须与用户模板和安装器保持同步）。

**GoMatrix 编译与启动**：

```bash
cd gomatrix && go build -o gomatrix.exe ./cmd/gomatrix
gomatrix.exe            # 无头服务模式（由 coara 托管拉起，无需单独启动）
```

**配对二维码**：在 coara WebUI 侧栏「手机」页查看（经 PC 代理 gomatrix 的 `/dashboard/qr.png`）；隧道未启用时手机只能局域网连接，可手动输入地址。

**信任模型**：独立运行 `python -m src.matrix_client` 时，远程发送者需在 `security.owner_matrix_ids` 列表中才被视为 owner；`coara --matrix` 模式默认将远程发送者视为 owner。完整通信链路见 [`MATRIX_APP_LINK.md`](./MATRIX_APP_LINK.md)。

### 4.5 `reminders`

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `timezone` | `"Asia/Shanghai"` | 提醒时区 |
| `tick_seconds` | `1.0` | 检查间隔（秒） |

用户模板里的 `reminders.enabled`（以及 `dashboard.enabled`）**目前代码未读取**——服务总是随 Root 启动，写 `false` 不会关闭。

### 4.6 `events`

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `webhook_host` | `"127.0.0.1"` | Webhook 监听地址 |
| `webhook_port` | `8765`（代码默认） | Webhook 监听端口；**用户安装模板改为 `18765`**（`deploy/gitee/templates/config.yaml`），开发机未配时用 8765（隧道脚本 `scripts/tunnels/` 也按 8765） |

Webhook 接收路径：`POST http://<host>:<port>/webhook/<事件源 id>`，另有 `GET /health`。

### 4.7 `security`

```yaml
security:
  owner_matrix_ids:
    - "@alice:coara.local"

  call_policy:
    prompt:
      - write
      - edit
    auto_allow:
      - read
      - grep

  # 执行沙箱（仅对 untrusted 调用者生效）
  sandbox:
    enabled_for_untrusted: true
    blocked_commands: ["rm", "sudo", "chmod", "chown", "mkfs", "dd", "shutdown", "reboot"]
    blocked_paths: ["/etc/*", "C:\\Windows\\*"]
    blocked_hosts: ["192.168.*", "10.*", "127.0.0.1"]
    blocked_env: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "*TOKEN*", "*SECRET*", "*PASSWORD*"]
```

> △ `blocked_*` 必须放在 `security.sandbox` 下，而非 `security` 根级。
>
> △ **`call_policy` 只有 `prompt` 与 `auto_allow` 两个键生效**（`src/agent/tool_policy.py`）：`prompt` 名单里的工具一律弹窗；`auto_allow` 名单里的工具即使工具/模型请求审批也直接放行（`prompt` 优先）。其它级别名单（如历史遗留的 `prompt_low`）**不消费**。

### 4.7.1 二元审批机制

工具调用是否需要人工确认，由**任一方决定**（OR 关系）：

```text
需要审批 = (工具侧认为需要审批) OR (LLM 在参数里写 require_approval: true)
```

**工具侧认为需要审批**的情况：

1. **工具类声明 `requires_approval`**——静态工具是类属性 bool；动态工具（shell/ws/task/workflow）是 `@staticmethod(args) -> bool`，按 action/参数判断
2. **工具名在 `call_policy.prompt` 列表中**（配置强制）

**LLM 明确请求审批**：调用参数 `require_approval: true`，可选 `approval_reason` 说明原因。

弹窗超时 5 分钟；超时或拒绝则调用被取消。非交互环境（无 tty、无远程通道）自动放行。

**旁路**：工具名在 `call_policy.auto_allow` 列表中时，即使命中上述条件也直接放行（同一工具同时在两个名单时 `prompt` 优先）。

**系统维护例外**：`janitor` / `daily` 跳过审批（`tool_policy.py`）。详见 [RELEASE_WORKFLOW.md §8](../deploy/gitee/RELEASE_WORKFLOW.md)。

### 4.7.2 各工具的审批触发条件

| 工具 | 触发条件 |
|------|----------|
| `shell` | 命令**首 token** 为 `sudo`/`su`/`mkfs`/`format`/`fdisk`/`parted`/`dd`/`mkswap`；管道下游命令不检查（`rm`/`docker`/`curl` 等不在清单，需要时用 `call_policy.prompt` 强制） |
| `write` / `edit` / `delete` | 目标路径（含 `template_path`）在挂载工作空间之外；挂载内写入不弹窗 |
| `ws` | `action == "remove"`（移除已登记工作空间） |
| `task` | `action == "stop"`（终止运行中的后台任务） |
| `orchestrator` | `action == "delete"`（删除草案，不可逆） |
| `vault` | 不走调用层审批（`requires_approval = False`）；开门靠密码侧信道，开门后文件操作走普通工具策略 |
| 其余内置工具 | 工具侧默认不触发；除非配置 `call_policy.prompt` 或 LLM 请求 |

### 4.8 `runtime_enhancements`（输出落盘 / 规则注入 / shell 哨兵）

```yaml
runtime_enhancements:
  tool_output_store:        # 大工具输出 spill 落盘
    enabled: true
    spill_threshold_bytes: 25000    # 全局默认阈值；自管理工具（read/write/edit/web_*）跳过
    batch_budget_bytes: 200000      # 单批工具调用模型可见总预算；0 = 关闭
    preview_head_chars: 2000
    preview_tail_chars: 8000
    tool_thresholds: {}             # 可选：按工具名覆盖阈值（字节）
    retention_days: 30
  rules_glob:               # .coara/rules/*.mdc 按路径 glob 注入
    enabled: true
    max_total_chars: 8000
    max_rule_chars: 2000
  shell_notify:             # shell 输出哨兵模式唤醒
    enabled: true
    default_debounce_ms: 5000
    default_max_notifications: 3
```

### 4.9 `output_truncation`（LLM 输出截断恢复）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 截断检测与恢复 |
| `default_policy` | `"force_tool"` | `force_tool` / `auto_continue` / `warn_only` |
| `max_continuations` | `3` | 最大续写次数（`auto_continue` 用） |
| `long_text_threshold_chars` | `4000` | 长文本阈值 |
| `output_token_ratio` | `0.98` | 输出 tokens ≥ 比例 × max_tokens 判定为截断 |

### 4.9.1 `llm`（LLM 请求超时）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `request_timeout_seconds` | `300` | 单次请求无响应读超时；语义是「两次 socket 读之间」的最长间隔，流式每收到分片即重置，挡不住滴漏式响应 |
| `total_timeout_seconds` | `300` | 单次调用全程硬上限（含连接、首字节、流式消费与重试全程）；超限即中止本次调用、释放卡死连接，错误以「已中止本次调用」抛回回合 |

SDK 内置重试已关闭（`max_retries=0`），重试统一由 `src/llm/retry.py` 管控（退避 + Retry-After + 总预算）。回合中可用 `/status` 查看相位行（等待模型响应 / 接收模型响应含分片数 / 执行工具 / 本地处理中 + 已耗时），用于区分「API 没回」与「本地卡住」。

### 4.10 其它零散节（开发参考 `config.yaml.example`）

| 节 | 字段（默认值） | 说明 |
|----|----------------|------|
| `environment` | `location: "江西赣州信丰"` | 首回合环境种子的地点行；空字符串省略该行 |
| `usage` | `enabled: true`、`max_queue: 8192` | 用量事件 JSONL 后台写入（队列下限 256）；离线查询 `coara usage summary` |
| `web_search` | `freshness_rrf_weight: 0.08` | auto 模式 RRF 排序的新鲜度加权 |
| `web_fetch` | `enabled`、`max_per_session: 25`、`max_per_domain: 6`、`min_quality_score: 0.18`、`min_meaningful_chars: 120`、`skip_duplicate_url: true` | 抓取去重、限量与低质量拒绝 |
| `content_policy` | `enabled: true`、`block_domains`、`llm_recovery_enabled: true`、`llm_recovery_providers`、`max_llm_recovery_strips: 2` | 风险域名入站拦截；provider 内容审核（如 MiniMax 1026/1027）时剥离外部工具结果重试 |

---

## 五、CLI 参数（临时覆盖）

**启动模型**：`coara` 无参数 = 拉起/复用常驻内核（`tray`/`daemon`）后 attach（终端只是端）；Web/Matrix 由内核托管，入口在系统托盘。

| 参数 | 作用 |
|------|------|
| `--workspace <path>` | 指定工作空间目录（默认 cwd） |
| `--workspace-alias <name>` | 启动时激活已登记的工作空间别名 |
| `-p, --provider <name>` | 指定本次 LLM 提供商（只覆盖 `agent.main`） |
| `-m, --model <name>` | 指定本次模型（同上） |
| `attach` / `tray` / `daemon` | 子命令：attach 到内核 / 无头内核+托盘 / 纯无头内核 |
| `-v, --verbose` | 详细日志 |

状态查看：`coara status` · `coara providers` · `coara llm-profiles`。

---

## 六、可配置项总览（统一管理入口）

系统里所有「可配置的东西」一览。统一的管理入口是 **WebUI 配置页**（`http://127.0.0.1:8080/config`，分 常规/模型/服务/自动化/工作空间 五组）；存储各归各位，不合并进单一文件。

| 配置域 | Canonical 存储 | 管理入口 | 生效时机 |
|--------|----------------|----------|----------|
| 全局行为（log_level、压缩、matrix、security 等） | `system/config.yaml` | WebUI 常规/服务组；手编 YAML | 多数新会话生效；matrix/webhook 需重启 |
| LLM 提供商与 profiles | `system/providers.yaml` | WebUI 模型组；`/model`；手编 | 保存后新会话生效 |
| LLM 请求超时（读/总预算） | `system/config.yaml` 的 `llm` 节 | 手编 YAML | 新请求即时 |
| 模型选择（空间统一，共看同步 chrome） | `registry/workspaces.yaml` 条目的 `provider`/`model` | 端 `/model`（作用调用端视图空间，写该空间 last-run + 切 session；共看该空间的各端立刻刷 chrome）；每次正常对话自动更新；`/model --global` 改全局默认 | 即时 |
| 主会话工具开关 | `system/config.yaml` 的 `tools.disabled` | `/tools off <名>` / `/tools on <名>`（三端 slash 命令） | 即时（写回 config.yaml） |
| 模型选择（全局默认，新/未绑定空间） | `users/default/llm_preferences.yaml` | `/model --global`；首启向导 | 即时 |
| 事件源 | `<workspace>/.coara/matters/definitions/*.yaml` | WebUI 自动化组；手编 YAML + `/events reload` | 即时（WebUI 保存自动重建监听） |
| 提醒（Reminder） | `<coara_home>/reminders/store.json` | WebUI 自动化组；对话里 `reminder` 工具 | 即时 |
| 工作空间登记 | `registry/workspaces.yaml` | WebUI 工作空间组；`ws` 工具；`coara ws` CLI | 即时 |
| 技能清单（default_include） | `config.yaml` 的 `skills` 节 | WebUI 模型组；手编 | 新会话 |
| 技能 listed 治理 | 每个 `SKILL.md` frontmatter | 手编 | 新会话 |
| 本地记录 | `users/default/records/{agent,user}/` | WebUI 记录页；`record` / `local_search`；手点收藏（无 CLI `/collect`） | 即时 |
| 工作流草案 | `users/default/workflows/drafts/` | WebUI 工作流页；`orchestrator` 工具 | 即时 |
| 宝箱 | `users/default/assets/vault/` | `vault` 工具（挂起） | 即时 |
| API key | `system/.env`（+ 仓根 `.env`） | 手编；首启向导 | 重启进程 |

生效时机三类：**即时**（走运行中 service 或每次读 raw）、**新会话**（`/new` 或 2h 空闲自动重读全量配置）、**重启**（matrix 连接、webhook listener、reminders 服务参数、usage writer）。

> WebUI 自动化/工作空间组的 CRUD 走 `src/ui/settings_handlers.py`（`/api/v1/reminders|event-sources|workspaces`），全部调用 Root 上的活 service，改完即时生效。CLI `coara ws` 直接改文件，运行中的进程不感知——两者别混用。

---

## 七、工作空间数据目录

> 完整布局见 **[STORAGE_AND_WORKSPACES.md](./STORAGE_AND_WORKSPACES.md)**。

配置 `coara_home` / `COARA_HOME` 后，按工作空间数据在 `<coara_home>/workspaces/<workspace_id>/`；未配置时退化为 `<workspace>/.coara/`。

| 路径 | 内容 |
|------|------|
| `<workspace>/.coara/logs/errors.jsonl` | 工作空间统一错误 JSONL（工具失败、拦截、agent/LLM 错误） |
| `<coara_home>/workspaces/<id>/logs/coara.log` | 文本运行日志 |
| `<coara_home>/workspaces/<id>/traces/` | Trace JSONL（可观测、侧栏补给、turn timing） |
| `<coara_home>/runtime/active.json` | 当前 live CLI 工作空间（手机 Matrix 绑定） |
| `<workspace>/.coara/matters/definitions/` | 事件源定义 YAML（仓库 `events/*.example` 是模板；空间自治布局） |
| `<coara_home>/users/default/matters/.state/` | 事件源去重状态 |
| `<coara_home>/users/default/skills/` | 用户级技能 |
| `skills/`（仓库内） | 内置技能（只读） |

---

## 八、按场景：最小配置

### 场景 A：仅 PC 本机聊天

1. `system/.env`：`ANTHROPIC_API_KEY=sk-xxx`
2. `system/providers.yaml`：单 provider（自动为默认）

```yaml
providers:
  minimax:
    driver: anthropic
    base_url: "https://api.minimaxi.com/anthropic"
    api_key_env: "ANTHROPIC_API_KEY"
    models:
      default: "MiniMax-M3"
```

3. 启动：`coara`（拉起/复用内核并 attach）

### 场景 B：PC + 手机 Matrix 聊天

在场景 A 基础上追加：

```bash
# .env — PC coara 连本机 GoMatrix（默认值，可不写）；手机 App 扫系统托盘「手机连接」配对码
COARA_MATRIX_HOMESERVER=http://127.0.0.1:8008
COARA_MATRIX_USER=@coara:coara.local
COARA_MATRIX_PASSWORD=coara-bot-password
```

1. 直接启动 `coara`（gomatrix 由内核托管自动拉起）
2. 托盘「手机连接」弹二维码，手机扫码配对即可，无需手动启动 GoMatrix

### 场景 C：+ Web UI 观测

```bash
coara      # 内核托管 Web 服务（默认 http://127.0.0.1:8080，浏览器自动打开或从托盘进入）
```
Web 由常驻内核托管，无独立启动开关；托盘「打开 Web」或直接访问端口。

### 场景 D：+ 邮件/截图/打印

截图（`screenshot`）、打印（`printer`）、邮件（`email`）是内置挂起工具，默认不占工具面，聊天中让 coara「截图」/「打印」/「发邮件」即可——它会经 `tool(action="activate")` 装载后使用。

可选 `.env` 追加 `COARA_EMAIL*` 自动登录邮箱（见 §二）。

---

## 九、常见坑

| 现象 | 原因 / 解法 |
|------|-------------|
| 改了配置不生效 | 写回位置优先级：`<coara_home>/system/config.yaml` > 仓根 `config.yaml` > `<cwd>/.coara/config.yaml`；确认你改的文件在加载链上 |
| 开发机上配置「串味」 | 仓根 `providers.yaml`/`config.yaml`/`.env` 优先级**高于** COARA_HOME；且仓根有 `providers.yaml` 时运行时 home 直接落到仓根。测用户体验时把仓根配置临时改名，或换干净的 `COARA_HOME` |
| `coara` 起来但没有 provider | 缺少 `<coara_home>/system/providers.yaml`（或仓根 `providers.yaml`）会直接 `ConfigError`，不是静默降级 |
| 把工具写进 `call_policy.prompt_low` 不弹窗 | 正常：执行器只读 `call_policy.prompt` 与 `auto_allow`，其它级别名单不生效（§4.7） |
| Webhook 收不到事件 | 端口不一致：代码默认 8765，用户模板 18765；以 `<coara_home>/system/config.yaml` 的 `events.webhook_port` 为准，隧道 / backend 的 URL 要跟它一致 |

| `reminders.enabled: false` 没关掉提醒 | 该开关代码未读取；`dashboard.enabled` 同（§4.5） |
