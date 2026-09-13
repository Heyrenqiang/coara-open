# 性能调研报告（2026-08-07）

> 只读调研产出，非运行时规范。行号基于调研快照（HEAD 36ef34a0 前后），与当前工作区可能有少量漂移；web trace 重构（src/ui/web/*、web_server.py 等）为并行进行中的改动，不在本报告范围。
> 原则：本报告只列「不改变功能语义」的性能优化点，每条含位置、问题、影响、建议、风险。

## 1. 背景与方法

对 src/ 全部 433 个 Python 文件按五块并行只读审查：核心热路径（回合循环 / LLM / 上下文窗口 / prompt）、存储与 I/O 层、并发基础设施（调度 / 事件总线 / Matrix / Web UI / 工作流）、工具层、CLI / 显示 / 技能 / 注入 / 启动路径。关键点用临时微基准量化（已清理，未留文件）：

- 真实 tokenizer 编码 3 万 token 历史约 54ms；启发式约 0.04ms（1000 倍差距）
- trace 全链路（serialize + sanitize + json.dumps）200 条消息约 0.7ms
- 流式 300 chunk 每 chunk 建 task 约 1.1ms（对比直接 async-for 0.03ms）

## 2. 总体判断

优化空间集中在三类模式，而非架构缺陷：

1. 重复全量计算（token 估算、trace 序列化、技能重扫、可见定义重建）
2. 事件循环上的同步 I/O（runtime state 读写、updates 全量重读、records 全量扫描）
3. 每次调用都做的可省开销（UTF-8 编码、deepcopy、Path.resolve、shutil.which、md5 指纹）

主路径已有不少正确设计，见 §6「已确认无需改动」。

## 3. 高价值（收益大，风险可控）

### 3.1 token 估算重复全量

- 位置：`src/coara/turn_loop/context_prep.py:76`、`src/coara/base.py:444-446`、`src/context/window.py:768`、`src/llm/tokenizer.py`
- 问题：每轮 LLM 迭代有三处触发 token 估算——context_prep 算一次（入参 input_tokens）、`_evaluate_context_guard` 内部再算一次、`maybe_compress_messages` 在判断阈值之前无条件全量估算。真实 tokenizer 编码 3 万 token 历史约 54ms，无 provider usage 快照时每轮最多 2-3 次全量。
- 影响：长历史 + 多工具迭代时每轮白付 100ms+ 纯 CPU（事件循环上）。
- 建议：把 context_prep 已算出的 input_tokens 直接传入 guard；maybe_compress 先按 usage_ratio 判断、低于阈值跳过全量估算。
- 风险：低（纯计算路径重构，不动压缩语义）。

### 3.2 delegate 生成路径全量重扫

- 位置：`src/skills/manager.py:46-96`、`src/coara/base.py:939`、`src/tools/builtin/delegate/delegate.py:996,1011`
- 问题：每次委派子智能体都调用 `skill_manager.discover()`（清空后全目录 rglob SKILL.md + frontmatter 解析，全局锁串行化并发 spawn）并重建父级全部可见工具定义（含对全部工具 schema 的 deepcopy）。
- 影响：每次委派约 10-50ms，多子智能体并行时被锁串行放大；是委派路径单点最大可省开销。
- 建议：技能发现按目录 mtime 做进程级缓存（目录变化才重扫，缓存键含 workspace_dir 保留工作区技能隔离）；父级可见定义改为轻量工具名集合缓存（只需 name / owner_only / plan_mode 过滤，无需深拷贝 schema）。
- 风险：中（缓存一致性；需保留跨进程/离线写盘兜底）。

### 3.3 trace runtime state 同步 I/O

- 位置：`src/ui/trace_recording.py:136-148`、`src/ui/trace_store.py:304-332`
- 问题：`_on_event` 对 turn_start / llm_turn_start / turn_end 等事件调用 `sync_runtime_from_event`，每次先 flush（默认 timeout=2.0，等待后台 writer 队列排空）再同步读 + 原子写 dashboard_runtime.json，全在 EventBus publish 调用栈（事件循环）。
- 影响：每回合 turn_start 1 次 + 每 LLM 迭代 1 次 + turn_end 1 次；每次 2 次 flush（积压时阻塞数百 ms 至 2s）+ JSON 读写。
- 建议：runtime state 维护为内存值（provider / model / running），turn 事件只更新内存，写盘去抖（回合结束或心跳时在 writer 线程写一次）。
- 风险：中（Web UI / Matrix 依赖该文件路由，需保持字段契约）。

### 3.4 workspace/updates 每次入站事件全量重读

- 位置：`src/workspace/updates/store.py:106-110,124,245-273`
- 问题：append() 写后 _prune_workspace 全量读一遍排序；事件源 / 工作流完成等路径均走此入口。
- 影响：500 条上限 × 1-3KB ≈ 1-3MB 读取 + JSON 解析，50-300ms 同步阻塞。
- 建议：进程内维护 dedupe_key 集合（启动加载一次）+ 追加式 index.jsonl 供重启恢复；prune 仅超限时全读（保留「不删 unread」语义）。
- 风险：中。

## 4. 中价值（顺手，纯局部）

| 位置 | 问题 | 建议 | 风险 |
|------|------|------|------|
| `src/runtime/tool_output_store.py:25,87,232` + `src/agent/executor.py:196` | spill 链路对同一大结果做 3-4 次全量 UTF-8 编码计字节（30KB 每次约 0.5-1ms） | 单次编码后复用，或 ToolResult 缓存字节长度 | 低 |
| `src/agent/executor.py:304` | 每次工具调用无条件计算缓存 scope（含 Path.resolve），仅 SEARCH/FETCH 用 | 移进 `tool.kind in _cacheable_kinds` 分支 | 低 |
| `src/coara/tool_manager.py:260-280` | 可见定义重建对全部工具 deepcopy，仅 4 个审批工具需要 | 仅对 write/edit/delete/shell 拷贝 | 低 |
| `src/tools/builtin/file_io/grep.py:64`、`search_support.py:212-214` | 每次调用 shutil.which 找 rg（Windows 约 1ms） | 模块级缓存一次 | 低 |
| `src/coara/workspace_state.py:358-374` + `base.py:1146` | 每回合无条件全量落盘 session_history（空回合也写） | 脏标记 / 节流（保持崩溃恢复覆盖） | 中 |
| `src/coara/base.py:1032-1033` | /new 时 force_reload 全量配置重载 + 技能全量重扫（5-30ms） | 收敛为按需重读；discover 加指纹缓存 | 低-中 |
| `src/records/agent_store.py:249-276,113-117` + `user_store.py` | 每次 record 写先全目录扫描做标题去重；index.yaml 每操作全量 YAML 读改写 | 标题相似度改走 index 内存字段；index 进程内缓存（保留跨进程回读兜底） | 中 |
| `src/llm/anthropic.py:446` | debug 日志对全量请求 payload eager json.dumps（debug 不开也执行） | 惰性求值（opt(lazy=True) 或级别判断） | 低 |
| `src/runtime/tool_output_store.py:130-175`、`src/ui/trace_store.py:613-632` | 裁剪逻辑每次全量读入再写回 | stat/mtime 粗判，仅接近上限时读 | 低 |
| `src/coara/turn_completion.py:72-96` | 流式聚合每 chunk 创建 asyncio task（signal 为空也建） | signal None 时直接 anext | 低 |
| `src/context/compression_prompt.py:8-14` | 模板文件每次读盘 | 模块级常量缓存 | 低 |
| `src/agent/loop.py:257-271` | 同一 tool_call 指纹 md5+json.dumps 计算 2 次（should_block + record） | record 复用 should_block 结果 | 低 |
| `src/coara/rules_glob.py:155-175` | 每回合重新 glob + frontmatter 解析规则文件 | 目录 mtime 缓存 | 低 |
| `src/coara/turn_loop/context_prep.py:41-47` | 每迭代 sanitize/close 各全历史遍历 | 先扫描标记位，无孤儿直接返回 | 低 |
| `src/coara/tool_output/diff.py:67-84` | 对每个 diff block 二次跑 SequenceMatcher | build 时顺带返回统计 | 低 |
| `src/core/config.py:165-186` | 配置链 3 次 load × 每文件双解析（冷启动 5-30ms） | 预解析复用；启动序列收敛为一次 load | 低 |
| `src/tools/cache.py:68-72`、`src/agent/loop.py` | 缓存 key / 指纹重复 json.dumps | 缓存规范化 key | 低 |
| `src/tools/builtin/file_io/file_support.py:129,168`、`edit.py:208-215` | 写类工具一次调用链 3-5 次 Path.resolve | invocation 内复用解析结果 | 低 |

## 5. 需拍板的行为微调

- web_search 门面工具 kind 为 OTHER，executor 层 5s 缓存未接通（cache.py 中 web_search TTL 是死配置）；接受短时新鲜度折衷可把 facade kind 设为 FETCH 或显式接入缓存
- `src/tools/builtin/file_io/read.py:257-308` read 带 offset/limit 仍全量读入再切片并全量缓存快照，大文件可改字节 seek + 行索引
- `src/workflow/persistence.py` SQLite 未开 WAL，每步一次 commit fsync（工作流执行路径）
- `src/tools/builtin/delegate/delegate.py:523,540` aide深拷贝完整历史 + 全量序列化落盘，可改浅拷贝 + 终态保存

## 6. 已确认无需改动

- 工具定义下发已有缓存（tool_manager `_tool_definitions_cache`，注册/揭示/plan 切换时失效）
- 系统 prompt 静态段缓存（base `_static_prompt_cache`）
- trace 写盘后台线程批量（_AsyncFileWriter，64 条/批）、Web UI WS 事件批处理（100ms 冲刷）
- Web 心跳不读 JSONL、dashboard 增量读（size cursor）、session 持久化走 to_thread
- 流式渲染 O(chunk)、spinner 12.5Hz 节流、diff 行数封顶
- 技能 search/activate 走内存，不重扫盘；todo 状态注入内存读
- 延迟 import（trafilatura/pygments/docx/openpyxl/SDK），provider client 惰性创建，冷启动无网络
- 调度器空队列等待模式、workflow scanner 5s 低频、Matrix sync 退避重试
- web_search provider 扇出已并行（asyncio.gather），错峰 sleep 是防限流刻意取舍
- web_fetch 有 singleflight + 失败缓存 + executor 300s 缓存，无重复下载
- EventBus 待处理任务上限 256，溢出有跟踪

## 7. 落地顺序与验证

1. 第一批（高价值）：§3.1 token 估算去重（含 guard 复用 + 阈值前置）→ §3.2 delegate 缓存（技能发现 + 轻量可见定义）
2. 第二批（中价值低风险组）：§4 中「低」风险项按行号清一遍
3. 第三批（需拍板）：§5 逐项确认后实施
4. 每批改完跑 `ruff check .`、`ruff format .`、`pytest tests/ -q`（默认套件）；涉及缓存一致性的项补针对性测试
