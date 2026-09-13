# wdl-engine

独立 WDL 执行引擎（自 coara v8 剥离）：**引擎 + 自己的 provider + 自己的执行器**。
不依赖 coara 仓库——不 import coara、不设 sys.path hack、不 pip 安装 coara。

## 组成

- **图模型与调度语义**（`src/wdl/core/`）：FlowGraph（节点+边）是唯一表达，
  WDL 文本是图的 canonical YAML 序列化投影；wait/kick 边分类、多激活语义、
  激活上限兜底。
- **引擎**（`src/wdl/engine.py`）：in-process asyncio 运行，命令即方法调用，
  事件经 asyncio 回调下发；保留 fencing owner、孤儿实例恢复、关停收口。
- **provider**（`src/wdl/providers.py`）：`~/.wdl/providers.yaml`，openai /
  anthropic 兼容两类，api_key 可引用环境变量。
- **节点执行器**（`src/wdl/executor.py`）：节点执行 = agentic 工具循环
  （httpx 直连）。节点声明了 `tools` 即进入「LLM → tool_calls → 执行内置
  工具（`src/wdl/tools.py`）→ 结果回注 → 再调 LLM」的往返，直到模型不再
  调工具（返回终稿）或达 `max_tool_rounds` 上限（返回已有文本，不报错）。
  未声明 `tools` = 无工具单回合调用（旧行为）。
- **持久化与恢复**（`src/wdl/persistence.py` + `scanner.py`）：SQLite 实例库
  （`<wdl_home>/instances.db`）+ wake scanner 恢复中断实例。
- **画布工作台**（`src/wdl/server.py` + `workbench/`）：`wdl serve` 起内嵌
  aiohttp 服务——静态托管 workbench 前端 + WDL 文件 CRUD + parse/emit +
  实例 run/list/get/cancel/resume + WS 引擎事件转发。前端为独立
  Vite + React + TS 项目（antd + @xyflow/react），自 coara v8 画布迁移。

## 与 coara 的关系（2026-09-10 起）

**coara 不再依赖本包。** coara 的内嵌 flow 能力自持图内核副本于
`coara/src/workflow/core/`（model / semantics / serde 三个文件，从本仓库
`src/wdl/core/` 搬运，文件头有分叉声明）。因此：

- 装 coara **不需要**装 wdl-engine；本包对 coara 是可选 extra（`coara[wdl]`）。
- 两边共用同一套图语义**设计**，但**源码已分叉**，各自独立演进。
- wdl 这边改图语义（边分类、就绪规则、激活上限）时，coara 副本不会自动同步。
  如需同步，参考 coara 的漂移校验脚本 `coara/scripts/dev/check_core_fork.py`。

本包仍是完整独立的软件：自带 CLI（`wdl`）、自己的 provider（`~/.wdl/providers.yaml`）、
自己的执行器与持久化，可脱离 coara 单独使用。

## 安装

```bash
pip install -e ".[dev]"
```

## Provider 配置

`<WDL_HOME 或 ~/.wdl>/providers.yaml`：

```yaml
default: my-openai
providers:
  my-openai:
    type: openai          # openai 兼容（/chat/completions）
    base_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}   # ${VAR} 引用环境变量
    models:
      - gpt-4o-mini
  claude:
    type: anthropic       # anthropic 兼容（/v1/messages）
    base_url: https://api.anthropic.com
    api_key: ${ANTHROPIC_API_KEY}
    models:
      - claude-sonnet-4-5
```

节点 LLM 解析顺序：**节点级 provider/model > 图级 > 配置文件 default**；
只给 provider 不给模型时取该 provider 模型列表第一项。

## 工具能力（agentic 工具循环）

内置最小工具集（`src/wdl/tools.py`，schema 为标准 JSON Schema）：

| 工具 | 说明 |
|------|------|
| `read_file` | 读文件（相对工作目录） |
| `write_file` | 覆盖写文件（自动建父目录） |
| `edit_file` | 精确字符串替换（要求唯一匹配） |
| `shell` | 在工作目录下执行命令（超时默认 120s，超时被杀） |
| `list_files` | 列目录（可递归） |
| `grep` | 正则全文检索 |

文件类工具以**工作目录为根做路径约束**（绝对路径 / `..` 逃逸一律拒绝；
`wdl run` 的工作目录 = WDL 文件所在目录；引擎 API 可传 `workdir=`）。
shell 只受超时约束，不拦命令。

### 节点声明 tools

```yaml
name: write-note
settings:                  # 图级工具循环参数（覆盖 providers.yaml settings）
  max_tool_rounds: 10      # 工具循环轮数上限（默认 20）
  shell_timeout: 60        # shell 工具超时秒数（默认 120）
nodes:
  write:
    task: 想一句短句并调用 write_file 写入 note.txt
    tools: [write_file]    # 白名单；tools: [] = 全量内置；省略 = 无工具（单回合）
  review:
    task: 读 note.txt 点评
    tools: [read_file]
edges:
  - from: write
    to: review
```

工具循环参数也可放 `providers.yaml` 顶层 `settings`（WDL 图级 `settings`
同名项覆盖它）：

```yaml
settings:
  max_tool_rounds: 20
  shell_timeout: 120
```

### 事件

工具调用经引擎 `on_event` 透出 `tool_start` / `tool_result`（负载含
`node_id` / `tool` / 参数与结果摘要）；`wdl run` 打印 ✓/✗ 工具行。

### 已知边界

- **无审批门、无沙箱**：定位是开发者本机工具——模型拿到 write/edit/shell
  即直接执行，不弹确认。不要把工作流指向不可信目录。
- 路径约束只管文件类工具；shell 可 `cd` 到根外执行。
- 达到 `max_tool_rounds` 上限时返回已有的最后文本（不报错，记 WARNING）。
- openai 侧 tool 消息无 `is_error` 字段，工具错误以 `[工具错误]` 前缀文本
  回注；anthropic 侧用 `tool_result.is_error` 标记。

## CLI

```bash
# 校验
wdl validate examples/hello.wdl

# 执行（打印节点进度与最终结果）
wdl run examples/hello.wdl --inputs topic=工作流

# 工具循环演示：fake LLM（先回 tool_calls 再回终稿）跑通 write_file 落盘
python demo_tool_loop.py

# 画布工作台（REST + WS + 静态站点，默认 http://127.0.0.1:8177）
wdl serve [--port 8177] [--root DIR]   # --root 为 WDL 文件浏览/读写根目录，默认 cwd
```

### 工作台前端构建

```bash
cd workbench
npm install
npm run build        # 产物落 workbench/dist，`wdl serve` 直接托管
```

冒烟脚本（起服务 + REST 断言 + WS 事件序列断言，fake LLM 网关免真 key）：

```bash
python scripts/smoke_serve.py
```

## 测试与 Lint

```bash
pytest          # fake executor 全链路 + persistence + providers + 工具循环（MockTransport 两拍）
ruff check .
```
