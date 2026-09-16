# 开发机 vs 用户机（配置与发布隔离）

> **发版流程（现行）**：deploy/official/RELEASE_WORKFLOW.md（闭源仓）  
> 部署入口：deploy/README（闭源仓）  
> 用户默认模板：deploy/official/templates/（闭源仓）

## 一句话

**同一套运行时代码（`src/`）；程序怎么装可以不同；配置/数据必须落在 `COARA_HOME`，且用户默认只来自 `deploy/official/templates/`。禁止把本机私货打进安装包。**

```text
┌──────────────────────────────────────────┐
│ 公用：src/ · skills/ · prompts · 工具逻辑 │  ← 开发与用户完全一样
└───────────────────┬──────────────────────┘
                    │
     ┌──────────────┴──────────────┐
     ▼                             ▼
 开发机                          用户机
 pip install -e 仓库              嵌入式 Python + wheel
 仓库根配置会覆盖 COARA_HOME       无仓库根 → 只读 COARA_HOME
 可发布 / 可 Build-Release         无发布能力
```

## 两层目录（必须分清）

| 层 | 环境变量 / 含义 | 开发机典型值 | 用户机典型值 |
|----|-----------------|--------------|--------------|
| **程序** | `COARA_ROOT`（安装包的 `bin/coara.cmd` 启动时设置） | 无（用 PATH 里的 editable 安装） | `%LOCALAPPDATA%\coara` |
| **数据+配置** | `COARA_HOME` | 建议 `%USERPROFILE%\coara` 或独立测试目录 | `%USERPROFILE%\coara` |

- 程序目录：可替换、可更新、**不含 API key**。
- `COARA_HOME`：升级**不覆盖**；密钥、providers、会话、vault、工作空间索引都在这里。

## 配置加载（谁盖谁）

YAML（深度合并，**后者覆盖前者**；代码见 `src/core/config.py`）：

1. `<COARA_HOME>/system/providers.yaml` + `config.yaml`
2. `<COARA_HOME>/users/default/config.yaml`（用户级覆盖）
3. **仅开发**：仓库根 `providers.yaml` / `config.yaml`（仓库根 = 能向上找到 `pyproject.toml` 的目录；用户安装包里没有这一层）
4. `llm_preferences.yaml`（`/model` 聊天命令写入，最后加载、优先级最高）

`.env`（`override=True`，后加载的赢）：

1. `<COARA_HOME>/system/.env`
2. **仅开发**：仓库根 `.env`

**用户机**没有仓库根 → 只有 `COARA_HOME`，行为干净。
**开发机**若同时有仓库根配置，会盖过 home——测「用户体验」时不要依赖仓库覆盖，或临时把仓库根配置改名。

另一个开发机差异：配置里没显式写 `coara_home` 时，若仓库根存在 `providers.yaml`，运行时 `coara_home` 会解析到**仓库根**（`src/core/config.py` 的 `_resolve_coara_home_config`）。这意味着开发机上的 traces/logs/vault 等数据默认落在仓库目录，而不是 `%USERPROFILE%\coara`。

## 三类「默认」文件（勿混用）

| 种类 | 路径 | 用途 |
|------|------|------|
| **用户默认（唯一进 zip）** | `deploy/official/templates/*` | `install.ps1` 首次写入 `COARA_HOME`（已存在则保留，不覆盖） |
| **开发 example** | 仓库根 `*.example` | editable 安装时人手复制参考；**禁止**被 Build-Release 拷进用户包（`Build-Release.ps1` 只从 `deploy/official/templates/` 拷贝） |
| **本机私货** | 真实 `config.yaml` / `.env`（gitignore） | 只留在本机；永不提交、永不打进 zip |

## 发布 vs 本机跑

| 动作 | 谁做 |
|------|------|
| `deploy/official/Build-Release.ps1` + `Publish-Official.ps1` | 开发机发版 → 见 RELEASE_WORKFLOW（闭源仓） |
| `install.ps1` / `install.sh`（coara.top） | 用户 |
| `pip install -e` | 仅开发机本地跑；**不是**用户路径 |

## 验收「新用户」时怎么测（最佳实践）

不要用已经脏的 `%USERPROFILE%\coara`。例如：

```powershell
$env:COARA_HOME = "$env:USERPROFILE\coara-fresh-test"
# 若已装过程序：新开终端运行 coara；或再跑一次 install
# 确认 system\ 来自 templates，且无仓库根路径、无旧 Matrix 账号
```

测完可删整个 `coara-fresh-test`。

## 安装入口（对照）

| 脚本 | 用途 |
|------|------|
| coara.top `install.ps1`（母本 `deploy/official/install.ps1`） | **用户正式路径** |
| `pip install -e ".[dev]"` | 仅开发机 |

## 改默认时的检查清单

- [ ] 改 `deploy/official/templates/` 对应文件（`config.yaml` / `providers.yaml` / `env.example`）
- [ ] 改 `src/cli/product_defaults.py`（Matrix 服务器名/端口/机器人账号、默认 provider）——与 templates、install.ps1 保持同步
- [ ] 改 `install.ps1` 里 `gomatrix.toml` 生成段（若涉及 Matrix 默认值）
- [ ] 仓库根 `*.example` 头部注释仍指向本隔离规则（内容可更丰富，但默认 provider 勿与用户模板冲突）
- [ ] 重新 Build / Publish 后用户才能拿到新默认（已存在的 `COARA_HOME` 文件不会被安装器覆盖）
