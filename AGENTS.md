<!-- This file is intended to be read by AI coding agents. Expect the reader to know nothing about the project. -->

> **文档索引**：[`docs/README.md`](docs/README.md) · **架构契约**：[`docs/架构契约.md`](docs/架构契约.md)（分层铁律/四梁八柱，改动前必读） · **维护约定**：[`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) · **遗留问题**：[`docs/REMAINING_ISSUES.md`](docs/REMAINING_ISSUES.md)

# coara v8 — Agent 指南

> coara 工作空间登记名：**v8**。概况 / 记录 / 动态过目由系统调度 **janitor**；跨空间任务执行用 `delegate(coaras, workspace=<登记名>)`。

## 目录

- [项目概览](#项目概览)
- [技术栈](#技术栈)
- [关键配置文件](#关键配置文件)
- [目录结构](#目录结构)
- [构建、运行与测试命令](#构建运行与测试命令)
- [代码风格与约定](#代码风格与约定)
- [测试说明](#测试说明)
- [关键架构模式](#关键架构模式)
- [部署与运行时架构](#部署与运行时架构)
- [技能系统](#技能系统)
- [配置](#配置)
- [附加文档](#附加文档)

## 项目概览

coara v8 是 **Python 优先的多智能体运行时**：asyncio 核心、内置工具、技能加载、可追踪执行，定位为能创建并组织子智能体的个人 AI 助手。以单个 `RootCoara` 为中心管理用户对话循环；子智能体是轻量级 IN_PROCESS 协程（非子进程），可前台或后台委派；WDL 工作流执行已由独立 WDL 软件承担（仓库顶层 `wdl/`，见下文目录结构），coara 只做 WDL 的生产者与文件宿主。

所有运行时节点共享同一基类 `CoaraBase`，能力由运行时接线决定：`delegate_depth`、工具白名单、`owner_only`/`is_owner_context` 可见性、plan-mode 允许列表、调用层审批（`call_policy`）。

- **语言**：Python 3.11+ / **许可证**：MIT / **包名**：`coara` / **版本**：1.0.17 / **仓库**：`D:\code_ws\v8`（Windows 开发环境）
- **规模**：`src/` 519 个 Python 文件；`tests/` 319 个测试模块（分默认 / extended / e2e / real_env 四档，见 [`tests/README.md`](tests/README.md)）

## 技术栈

- **核心**：Python≥3.11、asyncio、psutil≥6.0、watchdog≥5.0、loguru≥0.7、aiosqlite≥0.19、croniter≥2.0、cryptography≥43.0（vault）；`wdl-engine`（仓库内 `wdl/` 子项目，图模型/serde 供生产侧）
- **LLM**：anthropic≥0.40、openai≥1.50，`src/llm/` provider 注册表；其他 provider 经 `providers.yaml` 配置（MiniMax 走 Anthropic 兼容，MiMo/Agnes 走 OpenAI 兼容，DeepSeek `deepseek-flash` 走 Responses API）
- **CLI/UI**：click≥8.1、prompt_toolkit≥3.0（slash 命令与 `@` 提及补全）、rich≥13.9、questionary≥2.0
- **Vault/搜索**：`src/vault/` 加密宝箱（密码解锁 `open/` 工作树；内部用普通文件工具操作）；python-frontmatter≥1.1（技能 YAML frontmatter）
- **网络**：httpx≥0.27、aiohttp≥3.10（Web UI HTTP/WS 服务）、websockets≥13、matrix-nio≥0.25.2
- **文本处理**：tiktoken≥0.8（token 计数）、markdown≥3.7、pygments≥2.18、trafilatura≥2.0（网页正文抽取）
- **数据与校验**：pydantic≥2.9（全项目类型化模型）、pyyaml≥6、python-dotenv≥1
- **Office（可选）**：python-docx≥1.1（`write(path=*.docx)` 纯 Markdown 降级）、openpyxl≥3.1（`*.xlsx`）；富 HTML/Markdown→docx/pdf 走 **pandoc**（+typst/xelatex，非 PyPI）。安装：`pip install -e ".[dev,office]"`
- **开发工具**：pytest≥8.3 + pytest-asyncio≥0.24 + pytest-timeout≥2.3（每例 120s 硬上限）；ruff==0.15.12（钉死防漂移）；mypy≥1.13（尽力而为）

## 关键配置文件

| 文件 | 用途 |
|------|------|
| `pyproject.toml` | 主构建配置 — setuptools、元数据、依赖、入口点（`coara` → `src.cli.main:cli`）、ruff 与 pytest 工具配置 |
| `deploy/gitee/templates/` | **最终用户默认唯一真相**（Build-Release 拷进安装包）；`config.yaml.example`/`providers.yaml.example` 仅为开发参考 |
| `docs/DEV_VS_USER.md` | 开发机 vs 用户机：公用运行时 / COARA_HOME / 发布隔离 |
| `llm_preferences.yaml` | `/model` 聊天命令写入，持久化到 `<coara_home>/users/default/`（无 home 时为 `<cwd>/.coara/`） |
| `<coara_home>/system/config.yaml`、`providers.yaml`、`.env` | 本地生效配置（不入 git；从 templates 复制） |

根目录**没有** `setup.py`、`setup.cfg`、`requirements.txt`、`Makefile`、`tox.ini` 或 CI/CD 配置。构建/测试命令见下节，配置在 `pyproject.toml`

## 目录结构

```text
src/
├── agent/            # 工具执行、hooks、回合控制
├── background/       # Bash 后台运行器、任务存储
├── cli/              # CLI 入口、交互式聊天循环、显示控制器
├── coara/            # RootCoara、CoaraBase、工作流引擎、事件总线、调度器、注入、工具输出
├── context/          # 上下文窗口管理、TTL 缓存
├── core/             # 类型、配置、日志、错误、abort、coara_home、工具调用审计
├── event_sources/    # 事件源管理（文件监听、轮询、webhook）
├── examples/         # 内置示例项目安装器（stocks-watch）
├── llm/              # Provider 抽象、流式、重试、profile、service
├── matrix_client/    # Matrix 机器人集成
├── records/          # 统一记录域：facade/migrate/store + agent_* 与 user_* 实现
├── vault/            # 加密个人宝箱（凭据 + 文档）
├── prompt/           # Prompt 构建器、YAML 加载器、环境变量注入、agent 注册表
├── reminders/        # 提醒服务
├── todos/            # 会话 todo 域：类型、存储、显示、循环状态
├── runtime/          # 运行时持久化：工具输出 spill、用量统计、内核安全重启协议
├── skills/           # 技能加载、发现、激活逻辑
├── tools/            # 工具注册表、策略、缓存、内置工具、沙箱、内容策略
├── ui/               # Web UI HTTP/WebSocket 服务、trace 存储、ingest、控制面
├── utils/            # @提及解析、剪贴板、图像处理
├── workspace/        # 工作空间管理（注册表、VFS、权限）
└── workflow/         # WDL 草案生产侧：draft_store/service、flow 现场模式（执行层已剥离至 wdl/）

skills/               # 内置技能包（各含 SKILL.md 入口）
tests/                # 基于 pytest 的验证，按子系统组织
android-app/          # Jetpack Compose Android Matrix 客户端（coara App）
gomatrix/             # GoMatrix — 轻量 Matrix homeserver（纯 Go、SQLite）
wdl/                  # WDL 软件 — 独立工作流引擎 + 画布工作台（Python 包 wdl + workbench/）
deploy/               # Windows/Linux 部署；release/ = 局域网一键
scripts/              # 仓库工具脚本（dev / examples / 软著 / 隧道）
ref-doc/              # 外部笔记、调研材料、实现参考
docs/                 # 架构文档（中文）
events/ examples/ competition/   # 事件源模板 / 示例项目 / 路演答辩材料
```

> `src/coara/flow_coordinator.py` 是 src/coara/ 下的 agentic 工作流协调器（内存态 flow 图）

### 各子系统关键文件（找文件入口）

| 子系统 | 关键文件 |
|-----------|-----------|
| 运行时核心 | `src/coara/base.py`、`root.py`、`root_lifecycle.py`、`turn_orchestrator.py`、`turn_loop/`、`turn_completion.py`、`src/agent/output_truncation.py` |
| 内核重启 | `src/runtime/restart.py`（安全交棒协议：落意图 → 让位 → 同模式拉起 → 端口就绪确认 → 交棒；失败即回退）、`src/coara/commands/restart.py`（`/restart` 静默命令） |
| 工具执行 | `src/agent/executor.py`、`hooks.py`、`turn_control.py`、`loop.py`、`events.py`、`tool_policy.py` |
| 内置工具 | `src/tools/__init__.py`、`src/tools/builtin/`、`src/core/tool_base.py`、`src/tools/registry.py`、`cache.py`、`sandbox.py`、`content_policy.py` |
| 工作流 | `src/workflow/`（draft_store/service、paths）、`src/coara/flow_coordinator.py`、`src/tools/builtin/orchestrator/`；执行层在 `wdl/` 子项目 |
| LLM provider | `src/llm/provider.py`、`registry.py`、`service.py`、`drivers.py`、`endpoints.py`、`stream.py`、`retry.py`、`model_catalog.py` |
| Prompt | `src/prompt/builder.py`、`agent_registry.py`、`yaml_loader.py`、`environment.py`；agent 定义在 `src/coara/prompts/agents/{name}.yaml` + `{name}.md` |
| Vault | `src/vault/service.py`、`store.py`、`crypto.py`、`session.py`、`guard.py`、`src/tools/builtin/vault/vault.py`、`src/coara/commands/vault.py` |
| 事件总线/调度器 | `src/coara/event_bus.py`、`scheduler.py`（UnifiedScheduler）、`inbound_router.py`（workflow/event 路由） |
| 注入 | `src/coara/injections/`（background/environment/context_modules/workspace_message/snapshot/tool_result_wrapper）；标签在 `src/core/message_tags.py` |
| 工具输出三通道 | `src/coara/tool_output/`（pipeline/types/diff/syntax/gate_preview）、`src/core/read_format.py`、`src/coara/diff_render.py`；spill 在 `src/runtime/tool_output_store.py` |
| Trace/Web UI | `src/ui/trace_store.py`、`dashboard_handlers.py`、`dashboard_ingest.py`、`control_plane.py`、`dashboard_auth.py`、`web_server.py`；web 聊天区：`turn_stream.py`（回合流/微批）、`web_views.py`（视图带唯一权威：`view_seq` 单调 + epoch + 折叠映射）、`handlers/session.py::_load_view_snapshot`（快照唯一构造点）；前端 `src/ui/web/src/lib/store.ts`（序号对账/顺序归一）、`lib/toolLineGroups.ts`（折叠区组序）、`lib/subagentTree.ts`（活动树）、`features/chat/{MessageList,ToolLineRow}.tsx` |
| 后台 agent | `src/coara/background_agent.py`；Bash 后台 `src/background/bash_runner.py`、`task_store.py` |
| 工作空间 | `src/workspace/manager.py`、`registry.py`、`vfs.py`、`permissions.py`、`ephemeral.py`；切换 `src/coara/workspace_session.py`、`workspace_state.py`、`workspace_runtime.py` |
| 工作空间动态 | `src/workspace/updates/`（store/display/presentation/catalog/types）；事件源 `src/event_sources/manager.py`、`sources/` |
| CLI | `src/cli/main.py`、`commands.py`、`completers.py`、`session.py`、`display_controller.py`、`scrollback.py` |
| Matrix | `src/matrix_client/bot.py`、`file_bridge.py`、`approval_bridge.py`、`vault_bridge.py`、`chat_commands.py`、`mention_routing.py` |
| 记录/todo/提醒 | `src/records/`、`src/todos/`、`src/reminders/` |
| janitor | `src/coara/janitor_maintenance.py`、`workspace_protocol.py`、`prompts/injections/janitor.yaml` + `janitor.md` |
| 出站文件 | `src/tools/builtin/integration/outbound_file.py`、`send_file.py`、`src/ui/web_file_bridge.py`、`src/matrix_client/file_tools.py` |
| 用量统计 | `src/runtime/usage_collector.py`、`usage_query.py`、`usage_attribution.py`、`src/llm/usage.py` |
| 其他核心 | `src/context/window.py`（压缩）、`src/core/abort.py`（取消）、`src/core/coara_home.py`、`src/coara/rules_glob.py`（.mdc 规则） |

## 构建、运行与测试命令

### 安装

```bash
# 以可编辑模式安装（含开发依赖）
pip install -e ".[dev]"

# 含可选 Office 文档支持
pip install -e ".[dev,office]"
```

### 运行应用

```bash
# 默认入口：拉起/复用常驻内核（tray/daemon）并 attach（终端只是端）
coara

# 查看运行时 / provider / 状态
coara status
coara providers
coara llm-profiles

# 直接跑子命令（会自拉起内核）
coara attach        # 显式 attach 到运行中的内核
coara tray          # 无头内核 + 系统托盘图标

# 搜索 vault open/ 文件名（本进程内提示解锁；--no-prompt + COARA_VAULT_PASSWORD 用于 CI）
coara search <query>

# Vault / 宝箱（PC 操作，密码设置/修改仅限 PC；离线 CLI 按进程生效）
coara vault status|init|unlock|lock           # 状态 / 设主密码(≥8字符) / 解锁 / 锁
coara vault list [--no-prompt]|read <rel_path>|write|import <path>|passwd

# 工作空间管理（动态是聊天 slash 命令，不是 CLI 子命令）
coara ws list|add <path> --name <名>|rename <旧> <新>|remove <名> [--delete-disk]|default <名>

# 事件源查看：定义在 <workspace>/.coara/matters/definitions/*.yaml；增删改用聊天 /events 或 event_source 工具

# 用量统计（离线 JSONL 聚合）
coara usage summary
coara usage session <session_id>

# 示例
coara examples install stocks-watch
```

**首次启动向导**：无可用 API key 时，`_run_frontends` 先跑交互式配置向导（`src/cli/first_run_setup.py`）；占位符密钥（`your-key`、`xxx`、`changeme` 等）视为未配置

其他入口：`python -m src.coara`（经 `src/coara/__main__.py` 委托 `src.cli.main:main()`）、`python -m src.matrix_client`

**聊天 slash 命令**（`/help`、`/new`、`/status`、`/model`、`/ws`、`/vault`、`/workflow`、`/events`、`/stop`、`/restart`、`/exit` 等）：处理器在 `src/coara/commands/`；输出**中文优先、朴素、有价值**（正文不放 id/路径/内部 id，调试字段放 `CommandResult.data`）。完整清单见 `/help` 与 `docs/manual/`

### 测试

```bash
# 默认：核心冒烟（约 1912 例；排除 extended / e2e / real_env）
pytest tests/ -q

# 详细边界与集成测试（379 例；文件清单在 tests/conftest.py::_EXTENDED_TEST_PATHS）
pytest tests/ -m extended -q

# 完整套件（2291 例；PowerShell 下勿用 -m ""，易吞参数）
pytest tests/ --override-ini="addopts=" -q
```

布局与标记说明：[`tests/README.md`](tests/README.md)

```bash
# 严格模式（警告视为错误）
pytest tests/ -W error -m "not real_env and not e2e and not extended"

# 运行单个测试文件
pytest tests/test_coara/test_workflow_draft.py -v
pytest tests/test_agent/test_runtime.py -v   # extended
```

### Lint 与格式化

```bash
ruff check .
ruff format .
```

### 生成产物校验（改了真源后必跑）

以下文件由脚本从单一真源生成，**不要手改**；改动真源后重新生成，CI/提交前用 `--check` 校验是否漂移：

```bash
python scripts/dev/gen_envelopes.py            # 重新生成两端信封常量
python scripts/dev/gen_envelopes.py --check    # 只校验（退出码 1 = 有漂移）
```

真源 `docs/protocol/coara-envelopes.json` → `src/matrix_client/envelope_spec.py` + `android-app/.../coara/EnvelopeSpec.kt`。新增/移除信封只改真源一处。

### 自持 flow 内核漂移校验

`src/workflow/core/` 是 wdl-engine `wdl/src/wdl/core/` 的**自持副本**（2026-09-10 搬运），
目的是让 coara 的内嵌 flow 能力**不依赖 wdl-engine 是否安装**（wdl 是独立软件，对 coara 可选）。
分叉声明见 `src/workflow/core/model.py` 文件头。

```bash
python scripts/dev/check_core_fork.py            # 等价性硬校验 + 源码差异提示
python scripts/dev/check_core_fork.py --strict   # 源码差异也拦截
python scripts/dev/gen_core_golden.py            # 重新生成 flow 内核语义 golden（tests/data/core_golden.json）
```

- **等价性**（硬性）：装了 wdl-engine 时，用 wdl 当 oracle 跑 `tests/test_workflow/test_core_fork_parity.py`，逐项比对解析/序列化/校验/边分类/激活行为。wdl 未安装则跳过（不是失败 —— 那正是解耦的目标状态）。
- **源码漂移**（提示）：三文件哈希差异默认只提示。分叉后两边独立演进是**预期行为**，不该拦提交。
- **语义 golden**：`tests/data/core_golden.json` 是自持 flow 内核的语义快照，由 `scripts/dev/gen_core_golden.py` 生成；不依赖 wdl-engine 是否安装。
- 改图语义（边分类、就绪规则、激活上限）时**必须明确判断**是否两边都改 —— 静默漂移只会在运行时表现成"工作流偶尔卡住"，极难定位。



### Web UI 前端构建

`src/ui/static/dist/` **不入 git**。改 `src/ui/web` 后本地预览或发版前需 `cd src/ui/web && npm run build`，然后 hard refresh 浏览器。`deploy/gitee/Build-Release.ps1` 在 dist 过期时自动重建

### 类型检查

```bash
mypy src/
```

> 类型检查是尽力而为。部分文件使用按文件的 `# mypy: ignore-errors`（如 `src/llm/openai.py`、`src/llm/anthropic.py`）。不要在无类型标注的文件中引入新错误

## 代码风格与约定

- **ruff**（来自 `pyproject.toml`）：目标 Python 3.11、行宽 120；Lint 启用 `E`、`F`、`W`、`I`、`N`、`UP`、`B`、`A`、`C4`、`SIM`
- 每个模块以 `from __future__ import annotations` 开头；全量类型标注；数据结构优先 Pydantic `BaseModel`；轻量内部状态容器用 `dataclass(slots=True)`
- 工具描述**内联**在各工具类上（`description = """…"""` 或模块常量），位于 `src/tools/builtin/*/*.py`；动态片段注册时用 `.replace("${VAR}", …)` 注入（如 `delegate` 的子智能体列表）
- **标点规则**：说明性 prose（工具类 `description`、agent `.yaml`/`.md`、`parameters_schema` 的 `description`）**句末不加** `。`/`.`；运行时字符串（`ToolResult` 消息、CLI 提示、审批 UI 文案）保留正常标点。批量规范化：`python scripts/dev/normalize_prompt_punctuation.py`
- **命名**：Python 代码用英文；行内注释可中文或英文；docstring 一般英文；回复用户跟随其语言，代码/路径/技术术语保持英文
- **用户可见文案**：中文、朴素、有价值（规范见 `docs/CONVENTIONS.md` §用户可见文案）
- **无** pre-commit / CI 流水线；提交前手动 `ruff check .`、`ruff format .`、`pytest tests/`、`python scripts/dev/gen_envelopes.py --check`、`python scripts/dev/check_core_fork.py`

## 测试说明

- **框架**：pytest + pytest-asyncio（`asyncio_mode=auto`、`asyncio_default_fixture_loop_scope=function`）
- **标记**：`extended`（默认排除，按 `_EXTENDED_TEST_PATHS` 自动打标）、`e2e`、`real_env`（依赖真实 provider/网络/当前工作空间）
- **fixtures**：`tests/conftest.py` 提供 `isolated_coara_home`（autouse）与 extended 打标；工作流共享 fixture 在 `tests/test_workflow/conftest.py`
- **写测试**：异步用 `@pytest.mark.asyncio`、FS 用 `tmp_path`、mock 用 `monkeypatch`/`AsyncMock`/`SimpleNamespace`；真实 LLM 测试要关 provider（防 ResourceWarning）；集成用 `real_env_helpers.py::managed_initialized_root`；罐装 LLM 响应 `helpers.py` 的 `FakeProvider`/`BlockingProvider`/`make_test_coara`
- **陷阱（防挂住 / 退出卡死）**：
  - 长连接/队列资源（如 aiosqlite）测试必须显式 close，否则解释器退出挂住
  - 直接实例化 `WorkflowPersistence` / `aiosqlite.connect` 必须 `await persistence.close()`——每个连接一条非 daemon 工作线程（阻塞 `queue.get()`），不关则**用例全过但进程永不退出**（exit=4294967295）。排查「跑完不退」用 `python -c "import faulthandler; faulthandler.dump_traceback_later(25, exit=True); ..."`
  - 后台跑测试必带 `--timeout`（显式单条用例上限，配合 pyproject 120s 兜底），避免挂死套件拖成永远 running
- **布局**：约 22 个 `test_*/` 子系统目录（test_agent/…test_workspace/）+ 共享 `helpers.py` / `real_env_helpers.py` / `workflow_wdl_fixtures.py`

## 关键架构模式

> 完整深挖见 [docs/COARA_ARCHITECTURE.md](docs/COARA_ARCHITECTURE.md)；本节约 10 分钟必读核心

### 节点模型（同构运行时）

所有运行时节点（Root、子智能体）共享 `CoaraBase`，能力由 `SubAgentConfig`（`src/coara/builtin_agents.py`）+ `src/coara/prompts/agents/{name}.yaml`/`.md` 配对文件决定（见 [`docs/子智能体重构.md`](docs/子智能体重构.md)）。可委派类型：

- `coaras` — 并行工程子上下文（**仅前台**），继承 Root 可见工具集（`tools.include: ["*"]`），实例上无 `delegate`/`ws`/`reminder`/`vault`/`plan_mode`；只读摸底时 prompt 写明禁止修改；可选 `workspace=` 锁定登记工作空间目录
- `aide` — 只读智囊（read/grep/glob/web_search/web_fetch/todo），排除 write/edit/delete/shell/delegate/skill；可前台可后台；任务以 `prompt` 自洽完整
- `daily` — 系统调度（跨空间记录滑动窗口与日报）；模型不可经 delegate 点名
- `janitor` — 系统维护管道（概况 `ws.md`、记录、消息过目与巡检，`prompts/injections/janitor.*`），不经 delegate；由空闲 /new / 启动补扫 / 事件源 `handle: janitor` 触发；CLI 静默（`CLI_SILENT_SUBAGENT_TYPES` = janitor/daily，工具摘要与 diff 不进 CLI scrollback）

系统消息注入：`environment_injector.py`/`context_modules.py` 首回合前缀（用户规则 / 环境 / AGENTS.md / ws.md）与回合末情境；位置行可经 `config.yaml` `environment.location` 配置

### 三个执行层

1. **todo** — `todo(action=read|update|park)`；**每次必填 `description`**（CLI ✓ 行展示）；`update` 整表覆盖；`park` 一步收尾本轮（消息写 `message` 直接交付，不进入下轮 LLM，待办保持打开）
2. **delegate** — 前台 `asyncio.create_task` 并 await（不冻结事件循环，Ctrl+C 可中断）；后台经 `BackgroundAgentManager`（coaras 仅前台，aide 可前后台）；子智能体不能再委派（`delegate_depth>=1`）；Root 每轮 1200 次工具迭代、子智能体 1500 轮；`DelegateTool` 禁用通用超时。prompt 须自洽完整（六要素：目标/范围/方法/禁止项/验收/回报格式）——子智能体看不到本轮对话上下文
3. **workflow / flow** — 挂起工具 `orchestrator`（`tool(action="activate")` 揭示后见完整指南）；`flow` 把 `coaras` 编成内存态有向图（`FlowCoordinator`，spawn 停车、run 按 fan-in 级联推进、`routes_mode=one` 用 `deliver(next=…)`）；跑通后 `orchestrator(save)` 投影 WDL，再 `orchestrator(run)` 交 `KernelGraphRunner` 执行

### 工具注册分层（四层，按顺序）

1. **全局无状态**（每进程一次，`src/tools/__init__.py::register_builtin_tools()`，`manifest.py` 的 `PROCESS_STATELESS_TOOL_TYPES`）：`WebSearchTool`、`WebFetchTool`
2. **工作空间级**（`src/coara/runtime_tools.py::register_runtime_tools()`）：glob、grep、文件工具（read/write（含 docx/xlsx/pdf）/edit/delete）、shell、web_search（`replace=True` 重注册）、todo、delegate（仅 Root，`delegate_depth==0`）、ptc（Code Mode）
3. **Root 专属**（`src/coara/tool_registry.py::register_root_scoped_tools()` 注册到每个 WorkspaceSession）：`SkillTool`、`WsTool`、`PlanModeTool`、`EventSourceTool`、挂起 `OrchestratorTool`、`ReminderTool`、`VaultTool`（`vault_enabled` 时）
4. **磁盘工具包**（`src/tools/dynamic/loader.py`）：目录 = `TOOL.md`（YAML 头 name/description/parameters + 可选 entry）+ 入口 Python（`def run(**params)`）；发现路径用户级 `users/default/tools/` + 工作空间级 `.coara/tools/`；默认每次调用需审批（`config.yaml` `tools.auto_approve` 名单免审批）；写工具包方法论在 `tool-creator` 技能

### 工具目录布局（`src/tools/builtin/` 按类别）

| 类别 | 目录 | 工具 |
|------|------|------|
| 文件 I/O | `file_io/` | read、write（含 docx/xlsx/pdf）、edit、delete、glob、grep |
| Web | `web/` | web_search、web_fetch |
| 委派 | `delegate/` | delegate；编排走挂起 orchestrator |
| 媒体 | `media/` | 生图/视频（视频走全局 FIFO） |
| 运行时 | `runtime/` | shell |
| Code Mode | `code_mode/` | ptc（模型写 Python 程序编排多轮工具调用） |
| 规划 | `planning/` | plan_mode（enter/submit/exit） |
| 集成 | `integration/` | tool、send_file |
| 调度 | `scheduling/` | reminder（个人闹钟） |
| Todo | `todo/` | todo（description 必填 / update 整表 / park 收尾） |
| 记录 | `records/` | record（janitor/daily 写）、local_search（读+source） |
| 技能 | `skills/` | skill（search/activate） |
| Vault | `vault/` | vault（open/close/status；unlock/lock 别名） |
| 工作空间 | `ws/` | ws（list/add/remove/rename/switch） |
| 通信 | `communication/` | interact（子→主通道） |
| 具身/外设 | `embody/`、`printer/`、`screenshot/`、`email/` | 挂起工具：具身操作、打印、截屏、邮件 |
| 工作流 | `flow/`、`orchestrator/`（挂起） | flow 节点工具；多智能体编排 |
| 后台任务 | `background/` | 后台 aide 等长任务支撑 |

### EventBus vs UnifiedScheduler — 判定规则（常混淆）

| 通道 | 适用场景 | 不要用的场景 |
|------|----------|--------------|
| EventBus | 发后即忘的通知（trace、UI 更新、spinner、后台任务完成提醒） | 事件需要被 `RootCoara.process_message` 处理或注入 `message_history` |
| UnifiedScheduler | 消息**必须**进入 Root 对话循环（用户输入、工作流结果摘要、需回合响应的注入式提醒） | 消息纯观察性质，无需回复 |

**经验法则**：消息缺失会破坏对话状态或需要 LLM 响应 → `UnifiedScheduler`；只服务 Web UI / CLI spinner / trace → `EventBus`

### 其他主题（简要）

- **Abort / 用户打断**（`src/core/abort.py`）：AbortController/AbortSignal 仿 Web API；Ctrl+C 立即触发信号。打断时**保留本轮上下文**：已完成工具结果如实入史 → 未执行 tool_calls 补「[未执行]」→ 注入「当前会话已打断」→ 结束回合；`/new` 清空会话；`ws(switch)` 走 `strip_ws_switch_tail`；未预期异常整轮回滚，但会追加磁盘副作用注记（已执行的 write/edit/delete 及文件清单）。完整表格见文档 §6.5
- **TurnCompletion**（`src/coara/turn_completion.py`）：原生函数调用；provider 不支持 tools 时降为不带 tools 的纯文本完成（不再走已删除的 ReAct `<invoke>` 文本协议）；输出截断恢复在 `src/agent/output_truncation.py`
- **web 端刷新/切换同一把尺**（[Web设计体系 §0.1/§0.2](docs/Web设计体系.md)）：屏幕内容只由「权威快照 + 实时帧」驱动，两者都带 `workspace_dir` / `session_id` / `view_seq`，端上无内容缓存；按 `view_seq` 对账（已覆盖丢弃 / 接上推进 / 空洞补齐），一律原子提交、中途只显骨架。落带唯一路径 = `web sender → TurnStream._record → WebViewStore.append_event`；无在飞回合时走 standby 流。回归：`tests/test_ui/test_view_seq_frames.py`
- **Prompt 系统**：agent = `prompts/agents/{name}.yaml` + `{name}.md` 配对；`YamlPromptLoader` 支持 `extends`、`${VAR}`、`{{INCLUDE:path}}`；动态提醒在**消息层**注入，不烘焙进模板
- **上下文压缩**（`src/context/window.py`）：超窗口约 80% 自动经 LLM 压缩历史为 `<state_snapshot>`，默认保留最近 2 条；切分点回追 `pending_tool_results`，不与匹配 tool_calls 分离时才是有效切分点
- **可观测性**：TraceEvent → EventBus → TraceStore（`traces/*.jsonl`）→ Web UI WS 推送；错误进 `<workspace>/.coara/logs/errors.jsonl`；用量 `usage/events.jsonl`（`coara usage summary`）
- **后台任务通知**：完成 → EventBus → Root `_on_background_task_complete()` → 注入系统提醒；带发起 session 标记回投**发起会话**（忙进 continuation 队列）；janitor/daily 排除在对话注入外
- **主⇄子通道**：前台 coaras/aide 支持（`interact` 子→主；`delegate(action="message")` 主→子）；断点续跑 `delegate(action="resume")`；硬取消 `delegate(action="stop")`（Ctrl+C //stop 走 `hard_cancel_all_running_delegates`）
- **资源安全上限**：EventBus 256 待处理；TraceStore 5000 JSON + 50MB 轮转；WorkflowPersistence 30 天归档；SubagentStore / TaskStore 各 500 条；每工作空间动态 500 条
- **工具输出框架**（docs/TOOL_OUTPUT_FRAMEWORK.md）：每工具最多三通道 — Model（`ToolResult.content` 入历史）/ Terminal（`display` 渲染 CLI，绝不入历史）/ Gate（审批预览）；`src/coara/tool_output/pipeline.py` 唯一路由；spill 持久化（`src/runtime/tool_output_store.py`，全局默认 spill_threshold_bytes 25KB、batch_budget_bytes 200KB）
- **Rules Glob**（`src/coara/rules_glob.py`）：`.coara/rules/*.mdc` 按文件路径 glob 匹配注入（`max_total_chars` 上限）
- **事件源与 janitor**（docs/工作空间动态模型.md）：事件源是工作空间唯一触发钟（file_watch / interval_poll / webhook / cron），定义在 `.coara/matters/definitions/*.yaml`；`handle: park` 挂住待批示、`handle: janitor` 叫醒管家（`review(dismiss|elevate|resolve)`，理由写处置轨迹）、工作流 trigger 命中直启 WDL
- **Vault（保险柜）**：锁定=`sealed/<uuid>.cvault`，解锁=明文 `open/**`（120s 空闲自动关）；agent 仅 `vault(action=open|close)`（`owner_only`）；scrypt（N=2^17，旧 meta 按 legacy 2^14 惰性 rekey）→ AES-GCM；密码旁路（getpass/Web/Matrix）**绝不进 LLM 上下文**；设置/改密仅 PC CLI。已知限制：Matrix 明文传密码（被 SQLite 持久化）、`/new` 才锁箱

## 部署与运行时架构

详见 [`docs/STORAGE_AND_WORKSPACES.md`](docs/STORAGE_AND_WORKSPACES.md)、[`docs/DEV_VS_USER.md`](docs/DEV_VS_USER.md)

- **进程模型**：RootCoara（单 asyncio 进程，持 EventBus/Scheduler）；子智能体 IN_PROCESS 协程；Bash 后台任务 = 真实 OS 子进程（隔离进程组）。WDL 引擎不在内核进程模型内——它是独立软件（`wdl/`，`wdl run` / `wdl serve`）
- **coara Home**：设 `coara_home` 配置或 `COARA_HOME` env 后长期数据入全局：`workspaces/<id>/{traces,logs,artifacts,subagents,tool_outputs,usage}`、`system/`、`users/default/`；否则 `<cwd>/.coara`。实时 CLI 绑定 `{coara_home}/runtime/active.json`（Matrix/移动端经此路由）
- **工作空间切换**（`src/coara/workspace_session.py`、`workspace_state.py`）：每已切入工作空间一个 `WorkspaceSession`（独立 message_history/工具/进程锁）；切换不改 cwd、不 vault-lock；回合中切换：人类/API 不中断进行中回合（`turn_detach.py`），LLM `ws(switch)` 抛 `CoaraRunCancelledError` 并 `strip_ws_switch_tail`；陈旧会话（>7200s）下次切入开新会话
- **活动计时（易回归）**：基准是**最后一次会话活动**——`record_user_activity` 在 CLI / Web / Matrix 任一端发真实消息时刷新，`record_turn_activity` 在回合结束时刷新；`switch_workspace`、`/new`、空闲自动新会话、纯 slash 与子智能体/后台维护 agent 的 `turn_end` **不得**刷新。回归测试 `tests/test_coara/test_activity_clock_invariant.py`
- **Matrix（可选）**：GoMatrix 纯 Go homeserver（`gomatrix/`，构建 `cd gomatrix && go build -o gomatrix.exe ./cmd/gomatrix`），coara 托管拉起/看护（adopt-or-spawn）；`matrix-nio` 客户端自动接受邀请；多 agent 共享房间按 `@mention` 路由（`mention_routing.py`）；文件桥 `MatrixFileBridge`；Android Compose 客户端连同一房间（构建见 `android-app/README.md`）
- **Web UI**：`DashboardRestHandlers`（REST）+ `WebServer`（/ws `trace_batch` 实时推送）内嵌同进程；Vite React SPA（`src/ui/web/`）；token 认证。关键优化：trace 100ms 批处理、5s 轻量心跳、订阅 `workspace_switched` EventBus 刷新 trace_store、并行 root 关闭
- **安全模型**（两层）：① 调用层 `ToolExecutionPolicy`（`src/agent/tool_policy.py`）——工具声明 `requires_approval`（shell 破坏性基名集合 `{sudo,su,mkfs,format,fdisk,parted,dd,mkswap}`、写类越出挂载）/ LLM `require_approval: true` / `call_policy.prompt` 名单，任一命中弹人工确认（5 分钟超时=未执行；`auto_allow` 旁路，`prompt` 优先）；② 执行沙箱 `src/tools/sandbox.py`（仅 `trust_level="untrusted"` 启用）：拦命令/路径/私网 URL、净化环境变量
- **文件工具安全**：全部要求**绝对路径**；写类越出挂载走人工审批门（delegate 子代理 strict resolver 硬拒）；`resolve_workspace_path()` 拒相对路径/UNC/扩展路径/ADS；`glob`/`grep` 默认 workspace 根；注入检测 `detect_suspicious()` + `wrap_external_content()`

## 技能系统

技能是**面向 LLM 的运行时指令**（非代码插件）：每个技能 = 目录 + `SKILL.md`（YAML frontmatter + 正文）。权威文档：[`docs/技能系统.md`](docs/技能系统.md)

- **加载优先级**（后者覆盖前者）：内置 `skills/` → 用户级 `users/default/skills/` → 工作空间级 `.coara/skills/`（需 is_trusted）→ 运行时额外路径
- **运行时使用（仅 Root）**：`skill(action=search|activate)`（search 返回候选名+描述，activate 加载完整 SKILL.md 入历史）；`/new` 清除激活
- **内置技能**：`event-source`、`skill-creator`、`research`、`工作空间管理`、`tool-creator`、`create-rule`、`self_demo`

## 配置

1. **环境变量**：`<coara_home>/system/.env` 放 provider API keys 与可选 Matrix/vault 凭据——无需手动创建，首次启动无可用 key 时交互向导自动写入（非交互走 WebUI Providers 面板）；`env.example` 仅为变量参考
2. **Provider / 全局配置**：从 `deploy/gitee/templates/` 复制 → `<coara_home>/system/providers.yaml` / `config.yaml`（勿把本机私货打进 templates）；用户级覆盖 `users/default/config.yaml`；开发机可选仓根 `config.yaml`/`.env`（覆盖 home，见 `docs/DEV_VS_USER.md`）
3. **coara_home 解析**：合并配置 `coara_home` 字段 → `COARA_HOME` env → `<cwd>/.coara`（单工作空间试用）
4. **上下文模块**（AGENTS.md / 用户规则 / 情境注入）顺序可配置：`<coara_home>/system/context_modules.yaml`（详见 `docs/工作空间概况.md`）

## 附加文档

> 文档索引以 [`docs/README.md`](docs/README.md) 为准；**与本工作空间当前状态相关**的文档目录（含各文档现状、优先级与新增契约章节）见 [`.coara/ws.md`](.coara/ws.md) 的「目录文件」，此处不再复述。

对运行时行为有疑问时，查阅 `docs/COARA_ARCHITECTURE.md` 和 `docs/manual/`；已知问题见 `docs/REMAINING_ISSUES.md`