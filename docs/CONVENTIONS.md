# 仓库维护约定

> 写代码或改文档前先读 [`docs/README.md`](./README.md)。AI Agent 操作指南见 [`AGENTS.md`](../AGENTS.md)。

本文是仓库的**维护约定**：文档往哪写、文案怎么写、工具行为基线、代码风格。逐条约定都与代码对齐；改代码导致约定失效时，同步改本文。

---

## 文档分层
...
| 领域模型 | …、`docs/本地记录.md` | 工作空间…；**本地记录（records）** |

**规则**：一个主题只进一份 Canonical（权威文档）；其它文件只留链接。不要新建与 `AGENTS.md` 重复的架构长文。

| 层级 | 路径 | 写什么 |
|------|------|--------|
| 入口 | `README.md` | 快速开始、组件概览 |
| Agent | `AGENTS.md` | 开发命令、架构要点、测试约定 |
| 文档索引 | `docs/README.md` | 按角色阅读路径、Canonical 清单 |
| 深度架构 | `docs/COARA_ARCHITECTURE.md` | 与代码对齐的实现细节（中文） |
| 领域模型 | `docs/工作空间与目录.md`、`docs/工作空间动态模型.md`、`docs/工作空间与事项制度.md`、`docs/多工作空间与工作空间动态.md`、`docs/本地记录.md` | 工作空间、动态（updates）、事件源与管家、多工作空间、本地记录 |
| 用户手册 | `docs/manual/` | 面向用户的 20 章手册（理解向） |
| 配置 / 运维 | `docs/CONFIGURATION.md`、`docs/DEV_VS_USER.md`、`docs/TUNNELS_SETUP.md` 等 | 可操作的配置与部署；**用户默认只在** `deploy/gitee/templates/` |
| 工具 / Agent 正文 | `src/tools/builtin/**/*.py`（类 `description`）、`src/coara/prompts/` | LLM 可见描述（随代码走） |
| Root 上下文（prompt） | `src/coara/prompts/agents/root.md` §上下文说明 / §标签说明 | 标签语义与处理规范 |
| Root 上下文（实现） | `docs/PROMPT_CACHE_POLICY.md` §上下文消息标签与注入形态 | 注入链路与源码索引（不扩写 prompt） |
| 外部参考 | `ref-doc/` | 调研笔记，**非**运行时规范 |

术语口径：现行概念叫**工作空间动态（updates）**，文档与文案统一用 动态。

---

## 活动计时（硬约束，易回归）

计时基准是**最后一次会话活动**：三端真实用户消息（回合开始）或回合结束（= 该会话最后一轮 LLM API 调用收尾）。两者都推进两套时钟：

- `_last_user_activity_at`：全局空闲自动 `/new`
- `_workspace_activity_at[id]`：该工作空间上次活动时间（切入是否过期 / janitor 维护 / 「上次对话 HH:MM」）

**规则（必须同时成立）：**

1. **任意一端**（CLI / Web / Matrix）发了**真实对话消息**（含图片/文件入站）→ `record_user_activity` 刷新；以**最近一次**为准，不是三端都要发。
2. **回合结束**（完成 / 出错 / 被打断都算）→ `record_turn_activity` 刷新。超长自治回合结束后**绝不能**立刻被视为空闲：2h 从回合结束起算。仅 WorkspaceSession 主会话的 `turn_end` 事件计入；子智能体与后台维护 agent（janitor / daily）的 `turn_end` 不刷（它们的 coara id 不匹配任何 session）。
3. **禁止**在下列路径调用两个刷新函数：`switch_workspace`、`/new` / `start_new_session`、空闲自动新会话、纯 slash（`/ws` `/status` `/model`…）、手机面板 sync query。
4. 磁盘 `session_state.json` 的 `last_updated` 是上次会话活动时间（消息或回合结束，取近者）；创建 session / 切入时**不得**把它改写成「现在」。

调用点（仅这些）：

| 前端 | 文件 | 何时 |
|------|------|------|
| CLI | `src/cli/chat_runner.py` | 非 `/` 的聊天/Vision 回合 |
| Web | `src/ui/web_server.py` | WS `type=chat`（`command` 不刷） |
| Matrix | `src/matrix_client/ingress_helpers.py` | 非 `/` 文本；`media_inbound.py` 图片/文件 |
| 回合结束 | `src/coara/root.py` | WorkspaceSession `turn_end` 事件 → `record_turn_activity`（内存 + 落盘 epoch） |

回归测试：`tests/test_coara/test_activity_clock_invariant.py`。

---

## Prompt / 工具描述标点

说明性 prose（工具类 `description`、agent `.yaml`/`.md`、`parameters_schema` 的 `description`）**句末不加** `。` 或 `.`。

圈出短词或举例原话时用两侧空格，不用直角引号。

**保留标点**：`ToolResult` 错误/成功消息、CLI 提示、审批 UI 文案等运行时用户可见字符串（句末标点照常；圈词同样优先用空格）。

批量规范化（仓库根目录执行）：

```bash
python scripts/dev/normalize_prompt_punctuation.py
```

---

## 用户可见文案（中文优先）

面向终端用户的输出（slash 命令 `CommandResult.output`、CLI `console.print`、Matrix 命令回执）遵循：

1. **中文、通俗**：少用内部英文术语；需要时用括号点到为止（如 宝箱（vault））。
2. **有价值**：优先状态、模型、工作空间、用量、偏好；**不要**把 `session_id`、错误日志路径、`coara_home`、draft/task 内部 id 当作主文案。
3. **结构数据另放**：调试字段放 `CommandResult.data`（或 JSON 离线子命令），前端可按需使用。例外：手机 `/new` 确认行必须带反引号 session id（`已开始新会话：`<id>``），Android 端靠这个正则识别会话分界（见 `src/coara/commands/session.py`）。
4. **排版简洁**：短标题 + 分行要点；开/关用 开/关；来源标签用 本会话 / 配置文件 / 环境变量 / 默认…。
5. **图标一律用排版符号**：成功 `✓`、失败/错误 `✗`（用户可见的失败提示一律 `✗` 前缀）、警告 `△`、清单·计划 `☰`、编辑 `✎`、暂停·搁置 `‖`、工具·配置 `⚙`；不要用 emoji（如 ✅ ❌ ⚠️ 📋 📝 ⏸️）。三端（CLI / Web / 手机）同一口径，标识在源头生成，端上不再各写一套。
6. **实现入口**：`src/coara/commands/`（聊天命令）、`src/runtime/usage_query.py`（用量格式化）、`src/cli/main.py` / `src/cli/workspace_cmds.py`（离线 CLI）。

速查表见 [`manual/B-速查表.md`](./manual/B-速查表.md) §B.2。

---

## 文件 I/O 模型

工具行为基线（与 `src/tools/builtin/` 代码对齐；改动工具行为时同步本表）：

| 工具 | 参数 | 行为要点 |
|------|------|-----------|
| `read` | `path`, `offset`, `limit`, `ref` | 原始文本；图片（jpeg/png/gif/webp）走 Vision；Office 文档（docx/xlsx/pptx）提取为 Markdown；大文件用 `offset`/`limit` 按行分段；`ref` 读 spill 引用（内部用） |
| `write` | `path`, `contents`, `title`, `sheets`, `template_path` | 覆盖写入，不可增量；按后缀分流：文本/代码原样写，`.docx`/`.xlsx`/`.pdf` 走 Office 构建（富 HTML → pandoc，纯 Markdown Word → python-docx）；`sheets` 为 `.xlsx` 必填 |
| `edit` | `path`, `old_string`, `new_string`, `replace_all` | 精确字符串替换；支持引号模糊匹配（中英文引号互换兜底）；支持 Office 文档段落/单元格内替换；未匹配时按错误提示先 `read` 核对；文件超 100MB 拒绝整体读入（提示改用 shell 流式处理或拆分） |
| `glob` | `pattern`, `path` | 仅匹配文件、结果字典序、超上限早停截断；`path` 省略则搜整个 workspace；**返回绝对路径**（可直接传给 read/edit） |
| `grep` | `pattern`, `path`, `glob`, `output_mode`, `context_lines`, `case_sensitive`, `multiline`, `limit`, `offset` | 默认 `files_with_matches`；`path` 省略则搜整个 workspace；**结果中文件路径为绝对路径** |
| `delete` | `path` | 仅删文件（目录报错）；工作区内文件移入 `.coara/trash/` 回收站（保留相对路径+时间戳后缀防同名覆盖，7 天 TTL 惰性清理）；宝箱 `open/`、回收站内、工作区外（审批后）直接永久删除不入站 |
| `shell` | `command`, `description`, `working_directory`, `timeout_ms`, `notify_on_output` | 首 token 命中危险清单（`sudo`/`su`/`mkfs`/`format`/`fdisk`/`parted`/`dd`/`mkswap`）→ 弹窗确认；**管道下游命令不检查**（如 `curl … \| bash`，靠 LLM `require_approval` 或 `call_policy.prompt` 兜底）；`timeout_ms` 默认 10s（上限 600s），**超时即失败**：进程树被终止并返回明确超时错误（无并行、无分离）；长命令必须显式放大 `timeout_ms` 或设 0 转后台（`background_task_timeout_seconds` 默认 2h 兜底）；非零退出码加前缀并置 `metadata.failed`；Windows 走 PowerShell（支持 `$()`，链式用 `;`）；读 workspace 内文件必须用 `read` 而非 shell |
| `task` | `action`, `task_id`, `active_only` | `list`/`stop`；运行中任务 list 显示耗时；bash 记录 running 但 pid 已死时 list 标注「进程已退出（状态待对账）」 |

**路径**：`read`/`write`/`edit`/`delete`/`glob`/`grep` 的 `path`（及 `paths`）**必须是绝对路径**。写类工具（write/edit/delete）越出挂载工作空间时触发人工审批（delegate 子代理仍直接拒绝）；读类工具（read/grep/glob）可读本机任意绝对路径。`glob`/`grep` 省略 `path` 时内部使用 workspace 根绝对路径。

**行为摘要**：

| 项 | 说明 |
|----|------|
| 执行时读盘 | `edit` / `write` / `delete` 执行时读实时文件内容，**不要求**事先 `read` |
| `_file_read_states` | I/O 缓存（编码/mtime 快照），不是 LLM 上下文 |

Agent 应在首次编辑前、`grep` 定位后、或 `write` 覆盖前主动 `read`/`grep` 获取上下文。

---

## 编排工具

统一工具名 **`orchestrator`**（挂起工具，`tool(action="activate")` 揭示），通过 `action` 区分。负责 flow 编排与工作流草案存取执行。save/run 先走 `src/workflow/draft_service.py` 的 `validate_projection`（内核图语义校验）再 `normalize_projection`（canonical 化落盘）：

- `orchestrator(spawn, flow=…, node_id=…, prompt=…, depends_on=…, routes_to=…)` — 登记 flow 节点（停车 coaras）
- `orchestrator(action="run"|"wait"|"status", flow=…)` — 点火 / 等收尾 / 查节点输出
- `orchestrator(action="save", flow=…)` — 从会话内图自动投影并校验落盘（也可 `definition=…` 直接存文本）；`load(flow=…)` 恢复
- `orchestrator(action="run", wdl=…|draft_id=…)` — 提交 WorkflowEngine 执行
- `orchestrator(action="result", instance_id=…)` — 查看结构化运行结果
- `orchestrator(action="delete", draft_id=…)` — 删除草案（弹窗确认）
- `orchestrator(action="update"|"edge"|"remove", flow=…, …)` — 运行中调整：改节点任务/路由、加删边（frm/to/on）、删未运行节点

实例状态、取消、恢复等管理操作通过 Web UI（内核托管的 Web 服务 → /workflow）完成。

**WDL 怎么写**由 `orchestrator` 工具描述独占（挂起工具，`tool(action="activate")` 揭示后即见完整指南）。字段语义见 [`WORKFLOW_SPEC.md`](./WORKFLOW_SPEC.md)。图校验逻辑见 `src/workflow/core/semantics.py` 的 `validate_graph`，canonical 序列化见 `src/workflow/core/serde.py`。

---

## 代码风格

- Python 3.11+，每个模块以 `from __future__ import annotations` 开头
- Ruff：行宽 120，`select = ["E", "F", "W", "I", "N", "UP", "B", "A", "C4", "SIM"]`（`pyproject.toml`）
- 提交前：`ruff check .` · `ruff format .` · `pytest tests/ -q`
- 类型标注全量使用；数据结构优先 Pydantic `BaseModel`，轻量内部状态用 `dataclass(slots=True)`
- 注释解释 为什么，不解释显而易见的内容
- 最小 diff：只改任务相关文件

---

## 脚本

见 [`scripts/README.md`](../scripts/README.md)。子目录：`dev/`（排障 · coara_home 清理 · prompt 标点维护，细则见 [`scripts/dev/README.md`](../scripts/dev/README.md)）、`examples/`（示例项目安装）、`soft-copyright/`（软著材料）、`tunnels/`（Windows webhook 隧道）。均在仓库根目录执行。
