# dev/ — 开发排障与仓库维护

**仓库根目录**执行：`python scripts/dev/<脚本>.py …`
更多参数：`python scripts/dev/<脚本>.py --help`

## 脚本一览

| 脚本 | 用途 |
|------|------|
| [`analyze_turn_timing.py`](analyze_turn_timing.py) | 从 trace JSONL 分析每轮 LLM / 工具耗时 |
| [`benchmark_llm_latency.py`](benchmark_llm_latency.py) | 对比各 provider 的 LLM 延迟 |
| [`check_doc_links.py`](check_doc_links.py) | 校验全仓 Markdown 相对链接是否指向真实文件 |
| [`check_core_fork.py`](check_core_fork.py) | coara 自持 flow 内核与 wdl-engine 的漂移校验 |
| [`gen_envelopes.py`](gen_envelopes.py) | 从信封协议真源生成三端常量（含 `--check` 漂移校验） |
| [`parse_web_search_log.py`](parse_web_search_log.py) | 从 `coara.log` 提取 web_search query / 结果 |
| [`prune_coara_workspaces.py`](prune_coara_workspaces.py) | 清理 coara_home 下 pytest / 临时工作空间目录 |
| [`normalize_prompt_punctuation.py`](normalize_prompt_punctuation.py) | 一键规范化 prompt + 工具 schema 标点 |
| [`strip_prompt_terminal_punct.py`](strip_prompt_terminal_punct.py) | 仅处理 `src/coara/prompts/**/*.{md,yaml}` |
| [`strip_tool_schema_punct.py`](strip_tool_schema_punct.py) | 仅处理 `src/tools/builtin/**/*.py` 的 schema 行 |

## 可观测性

### analyze_turn_timing.py

读 `{coara_home}/workspaces/*/traces/trace_events.jsonl`，输出最近 session 的 turn 分解（LLM / tools / context / overhead）。

**选哪个工作空间的 trace**（优先级从高到低）：

| 优先级 | 选项 | 说明 |
|--------|------|------|
| 1 | `--trace-file PATH` | 指定 jsonl 文件 |
| 2 | `--fresh` | 全 home 里最新的 trace |
| 3 | `--workspace PATH` | 指定工作空间目录 |
| 4 | （默认） | live `active.json` 里正在跑的 coara |
| 5 | fallback | 当前 shell 的 cwd 对应工作空间 |

```bash
python scripts/dev/analyze_turn_timing.py
python scripts/dev/analyze_turn_timing.py --session-id 8f8bed16   # 该 session 全部 turn
python scripts/dev/analyze_turn_timing.py 世界杯                   # 关键词过滤
python scripts/dev/analyze_turn_timing.py --workspace D:/code_ws/nx
python scripts/dev/analyze_turn_timing.py --last-sessions 5        # 最近 5 个 session（默认 2）
python scripts/dev/analyze_turn_timing.py --last 20                # 最近 20 轮
python scripts/dev/analyze_turn_timing.py --list-traces
python scripts/dev/analyze_turn_timing.py --coara-home D:/coara
```

### 相关 CLI（非 scripts，但同属排障）

| 命令 | 作用 |
|------|------|
| 聊天内 `/usage` | 当前 session 的 token / 工具 / fetch / search 统计 |
| `coara usage summary --days 7` | 离线汇总 `{coara_home}/usage/events.jsonl` |
| `coara usage session <id>` | 单 session usage 明细 |

### benchmark_llm_latency.py

需 `.env` + `providers.yaml`。

```bash
python scripts/dev/benchmark_llm_latency.py
```

### parse_web_search_log.py

默认读 `$COARA_HOME/logs/coara.log`，输出同目录 `_parsed.txt`。

```bash
python scripts/dev/parse_web_search_log.py
python scripts/dev/parse_web_search_log.py --log D:/coara/logs/coara.log --output D:/tmp/parsed.txt
```

## coara Home 清理

```bash
python scripts/dev/prune_coara_workspaces.py --coara-home D:/coara --dry-run   # 先预览
python scripts/dev/prune_coara_workspaces.py --coara-home D:/coara            # 实际删除
python scripts/dev/prune_coara_workspaces.py --coara-home D:/coara --only-stale
python scripts/dev/prune_coara_workspaces.py --coara-home D:/coara --keep <workspace_id>
```

保留 `registry/workspaces.yaml` 登记项；`--only-stale` 只删 `test-*` / `workspace-*` 等前缀；`--keep` 可重复指定额外保留的 id。

## 仓库维护 — prompt 标点

说明性 prose（工具类 `description`、agent `.yaml`/`.md`、schema `description`）句末不加 `。`/`.`；运行时错误/成功文案保留标点。约定见 [`../../docs/CONVENTIONS.md`](../../docs/CONVENTIONS.md)。

```bash
python scripts/dev/normalize_prompt_punctuation.py
```

## 仓库维护 — 控制信封协议

**真源**：`docs/protocol/coara-envelopes.json`（三端 `[COARA_*]` 信封的唯一定义处）。

```bash
python scripts/dev/gen_envelopes.py            # 重新生成两端常量
python scripts/dev/gen_envelopes.py --check    # 只校验漂移（提交前，漂移返回 1）
```

产物（**请勿手改**）：

| 产物 | 消费方 |
|------|--------|
| `src/matrix_client/envelope_spec.py` | 服务端 `ingress_helpers.is_unrecognized_coara_envelope` |
| `android-app/app/src/main/java/com/example/agentchat/coara/EnvelopeSpec.kt` | Android `MobileSyncParser.isUnrecognizedEnvelope` |

新增 / 移除信封：只改真源 JSON → 重跑生成器 → 两端自动对齐。两端兜底判据由同一真源派生，不会再出现「服务端有兜底、Android 端没有」的不对称。

## 仓库维护 — 自持 flow 内核漂移

`src/workflow/core/` 是 wdl-engine `wdl/src/wdl/core/` 的**自持副本**（2026-09-10 搬运），
让 coara 的内嵌 flow 能力不依赖 wdl-engine 是否安装。分叉声明见 `src/workflow/core/model.py` 文件头。

```bash
python scripts/dev/check_core_fork.py            # 等价性硬校验 + 源码差异提示
python scripts/dev/check_core_fork.py --strict   # 源码差异也拦截
python scripts/dev/check_core_fork.py --quiet    # 只返回退出码
```

| 层级 | 内容 | 漂移时 |
|------|------|--------|
| 等价性 | 装了 wdl 就用它当 oracle，逐项比对解析/序列化/校验/边分类/激活行为 | **返回 1** |
| 源码 | 三文件哈希差异 | 默认仅提示；`--strict` 返回 1 |

wdl-engine 未安装时跳过等价性比对 —— 这不是失败，正是解耦的目标状态（coara 可脱离 wdl 独立跑 flow）。

**改图语义时注意**：边分类（wait/kick）、就绪规则、激活上限的改动，必须明确判断是只改自持副本、还是两边都改。静默漂移只会在运行时表现成「工作流偶尔卡住」，没有报错、极难定位。

