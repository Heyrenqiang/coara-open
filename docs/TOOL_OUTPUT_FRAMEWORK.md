# Tool Output Framework（工具输出框架）

> coara 自有架构。要解决的问题：**同一次工具执行，给 LLM 看的内容和给人看的内容不是同一种东西。**

---

## 1. 核心问题与结论

工具结果会进入 `message_history`，成为后续每一轮 LLM 调用的长期前缀（影响 token 成本、prompt cache、推理质量）。同时 CLI 需要**可读、可扫、带 diff 色块**的展示。

若混在一个字符串里，要么浪费 token，要么终端难看。框架的解法：**一次执行，三条出口**。

| 通道 | 数据载体 | 消费者 | 原则 |
|------|----------|--------|------|
| **Model** | `ToolResult.content` | `wrap_tool_result()` → `message_history` | 紧凑、稳定、可解析 |
| **Terminal** | `ToolResult.display` | `tool_complete` 事件 → `pipeline.render_terminal_from_event()` → CLI 滚动区 | 富 diff、语法高亮；**永不进 history** |
| **Gate** | 执行前临时计算，不落 `ToolResult` | `pipeline.render_gate_preview()` → 审批弹窗 | 仅 edit 类调用确认前；**不持久化** |

```text
                    ┌── Gate ──► 审批弹窗（执行前，tool_policy.py 调用）
                    │
  ToolInvocation ───┼── execute() ──► ToolResult
                    │                      │
                    │                      ├── content ──► Model（进 message_history）
                    │                      └── display ──► Terminal（CLI 滚动区）
                    │
                    └──（可选）maybe_spill_tool_result 只改写 content，display 原样保留
```

---

## 2. 各通道的实现

### 2.1 Model 通道（给 LLM）

- 数据源是 `ToolResult.content`。
- 进 history 前经 `src/coara/injections/tool_result_wrapper.py` 的 `wrap_tool_result()` 最后包装：
  - 成功且有正文 → **原样透传**，不加任何包装（省 token）；
  - 成功但为空 → `<系统消息>工具输出为空</系统消息>`；
  - 失败 → `<系统消息>工具执行失败：…</系统消息>`；
  - 取消 → `<系统消息>工具 … 已被用户取消</系统消息>`。
- 典型形态：`read` 的 content 是带行号的正文（见 §4）；`edit` 的 content 是短状态句（`已编辑 <path>：已替换（N 处匹配）`），diff 不放 content。

### 2.2 Terminal 通道（给终端）

- 数据源是 `ToolResult.display`（一组 `DiffDisplayBlock`）。目前只有 **edit** 工具产出 display（`src/tools/builtin/file_io/edit.py` 在写盘后调用 `build_diff_display()`）。
- 投递链路：executor 把 display 序列化进 `tool_complete` 事件的 `display_blocks` 字段 → `CliDisplayController._on_tool_terminal_display()` → `pipeline.render_terminal_from_event()` → `diff_render.render_display_blocks(preview=False)` → `CliScrollback` 输出 Rich 面板。
- 渲染规则（`src/coara/diff_render.py`）：
  - 滚动区完整 diff 面板最多 **15 行**（`MAX_SCROLLBACK_DIFF_LINES = 15`），超出显示 `... N more lines`；
  - 每个 hunk 带 **3 行**上下文（`diff.py` 的 `N_CONTEXT_LINES = 3`）；
  - 新旧文件任一超过 **10 000 行**（`_HUGE_FILE_LINE_THRESHOLD`）时不做逐行 diff，只产出摘要块（`(N lines)` / `(N lines, modified)`）；
  - 预览末尾的空行自动裁剪（`_drop_trailing_empty_display_lines`）；
  - Matrix 端共用 `flatten_diff_lines`，同样 15 行上限（`src/matrix_client/diff_bridge.py`）。

### 2.3 Gate 通道（审批前）

- 触发点：`src/agent/tool_policy.py` 在需要人工确认的调用上，先 `render_gate_preview(invocation)` 拿到预览，再连同确认问题一起弹给 `prompt_select`。
- 构建逻辑（`src/coara/tool_output/gate_preview.py`）：只对带 `old_string` / `new_string` 参数的 invocation（即 edit 类）生效；读实时文件内容、`simulate_edit_replacement()` 模拟替换、对模拟结果生成 diff。**不写盘、不执行**，文件读不到或模拟失败时返回 `None`（无预览，照常弹确认）。
- 预览渲染：只显示变更行（`changed_only=True`），最多 **6 行**（`MAX_PREVIEW_CHANGED_LINES = 6`），超出显示 `... N more changed lines`。

---

## 3. Spill：超大输出的落盘机制

Spill 与三通道**职责不同**：三通道管「内容形状」，spill 管「content 太大时不要整段塞进 history」。实现在 `src/runtime/`（不是 `src/coara/tool_output/`）。

### 3.1 触发与效果

- 触发点：`src/agent/executor.py` 在每次 `execute()` 之后、发 `tool_complete` 之前调用 `maybe_spill_tool_result()`。
- 只对**成功的字符串 content** 生效；错误、取消、已 spill 过的结果直接跳过。
- 命中阈值时：全文落盘为 `{ref}.json`，`content` 被替换成一段 `<tool_output ref="…" bytes="…" lines="…">` 摘要（含头/尾预览），`display` 原样保留。
- `ref` 是 8 位十六进制（`uuid4().hex[:8]`）；落盘目录为按工作空间的 `tool_outputs/{session_id}/`，同目录维护 `index.jsonl` 索引。

### 3.2 分层阈值（`src/runtime/spill_policy.py` + `src/core/types.py` 的 `ToolOutputStoreConfig`）

| 层 | 取值来源 | 默认值 |
|----|----------|--------|
| 全局默认 | `tool_output_store.spill_threshold_bytes` | **25 000 字节** |
| 按工具内置表 | `spill_policy._DEFAULT_TOOL_THRESHOLDS` | grep **20K**(head+tail)、glob **20K**(head+tail)、shell **30K**(tail)、delegate **32K**(tail) |
| 按工具配置覆盖 | `tool_output_store.tool_thresholds`（工具名 → 字节数） | 空 |
| 自管理工具 | `spill_policy.SELF_MANAGED_TOOLS`：read / write / edit / delete / web_fetch / web_search | 不按工具阈值 spill（执行层自己控制输出大小） |
| 批次总预算 | `tool_output_store.batch_budget_bytes` | **200 000 字节/轮工具批次**，0 = 关闭 |

预览形状（`tool_output_store.preview_head_chars` / `preview_tail_chars`）：头部 **2 000 字符**、尾部 **8 000 字符**。`preview_keep` 决定强调哪段：`head` / `tail` / `both`（both 时中间插入 `--- [PREVIEW TRUNCATED — use read(ref=…) for full body] ---` 标记）。

### 3.3 批次预算（batch budget）

一轮并行工具调用全部执行完后，executor 调 `apply_batch_spill_budget()`：若该批次所有结果合计的 model 字节数超过 200KB，则**从最大的结果开始逐个强制 spill**，直到总量回到预算内。规则：

- 强制 spill 不看按工具阈值（自管理工具的大输出也可能被卸载）；
- 不超过 256 字节的小结果不动；
- 已 spill 的结果（content 已是摘要）跳过。

### 3.4 读回与保留

- **read 工具读回**：`read(ref="…")` 从当前会话的 spill 目录加载全文，按行分页（`src/runtime/spill_read.py` 与 `read()` 共用同一套切片语义）。未指定 `limit` 且全文超过 **500 行**时自动截到 500 行（`read_format.DEFAULT_REF_READ_LIMIT_LINES`）。
- **Dashboard**：`GET /api/tool-outputs`（索引）、`GET /api/tool-output/{ref}`（行分页读回，ref 须匹配 `^[a-f0-9]{8}$`）。
- **保留策略**：`tool_output_store.retention_days` 默认 **30 天**。清理在两个时机发生：Root 启动时（`root_lifecycle.py`），以及每写满 **10 个** spill 文件后顺带清理一次（`_PRUNE_EVERY_N_WRITES = 10`）。

### 3.5 与 `coara/tool_output/` 的分工

| 模块 | 职责 |
|------|------|
| `src/coara/tool_output/` | **格式与展示**：content 怎么写（行号、截断）、display diff 怎么构建、Gate 预览怎么算 |
| `src/runtime/tool_output_store.py` / `spill_policy.py` / `spill_read.py` | **溢出持久化**：content 太大时落盘，给 model 留 ref + 预览，支持读回 |

---

## 4. read 的 Model 通道格式（`read_format.py`）

- 每行加行号前缀 `N|内容`（Cursor 风格），行号槽位最小 6 列、按末行号宽度对齐；
- 单行超过 **2 000 字符**截断并加 `…`（`DEFAULT_MAX_LINE_CHARS`），保持 LLM 前缀稳定；
- `read(ref=…)` 的分页与 cap 见 §3.4。

---

## 5. 生命周期（顺序固定）

```text
1. tool_policy           需人工确认？→ render_gate_preview → 审批弹窗（仅 edit 类有预览）
2. invocation.execute()  产生 ToolResult(content, display?, metadata)
3. maybe_spill_tool_result   content 超阈值则落盘并改写 content；display 保留
4. （批末）apply_batch_spill_budget   批总量超 200KB 时继续卸载最大结果
5. tool_complete 事件    payload 携带序列化的 display_blocks（Matrix / 子智能体 diff 等订阅方）
6. turn_orchestrator     yield `✓ tool(...)` 单行摘要（CLI 通道 A）
7. display 渲染          CLI 在 ✓ 行之后画 Terminal diff（前台 session）；子智能体在 flush ✓ 行之后画
8. wrap_tool_result      content → message_history
```

`✓ edit(...)` 摘要在 diff 面板之前。三者不重复。

CLI 三通道（滚动区 / 动态区 / 回合产出）的整体设计见 [`CLI_DISPLAY_FRAMEWORK.md`](./CLI_DISPLAY_FRAMEWORK.md)。

---

## 6. 模块职责（代码地图）

```text
src/coara/tool_output/          # 构建层（纯数据 / 字符串，不 import Rich）
  types.py          DiffDisplayBlock（path/old_text/new_text/起始行/摘要标记）
  （read_format.py 已下沉到 src/core/read_format.py）Model：行号前缀、长行截断、ref 默认 limit
  diff.py           Terminal/Gate：old/new 文本 → diff 块（线程池执行，不阻塞事件循环）
  gate_preview.py   Gate：执行前从 invocation 模拟 edit 生成 diff（不写盘）
  syntax.py         Terminal/Gate：Pygments 行内高亮
  pipeline.py       ★ 唯一对外入口（渲染路由 + display 序列化）

src/coara/diff_render.py        # 渲染层（Rich Panel/Table）
src/coara/display.py            # CLI spinner 单行摘要（与 diff 无关）

src/runtime/spill_policy.py       # 分层阈值 + 预览形状
src/runtime/spill_read.py         # read(ref) / Dashboard 共用的行分页读回
src/runtime/tool_output_store.py  # 落盘、maybe_spill、batch budget、retention 清理

src/coara/injections/tool_result_wrapper.py   # Model 通道进 history 前的最后包装
src/agent/executor.py             # execute → spill → tool_complete(+display_blocks)
src/agent/tool_policy.py          # 需确认时 → render_gate_preview
src/cli/display_controller.py     # tool_complete → render_terminal_from_event
```

**构建与渲染分离**：`tool_output/*` 只产出 blocks / 字符串；`diff_render.py` 才碰 Rich。这样工具模块不需要 import Rich。

---

## 7. 扩展新工具的检查清单

1. Model 需要什么才能继续推理？→ 写进 `content`（尽量短）
2. 用户是否需要 rich 反馈？→ 可选产出 `display` blocks
3. 高风险变更是否需要在确认前看到 diff？→ 在 `gate_preview.py` 扩展 builder
4. 输出是否可能超大？→ 依赖 spill，**不要**把 diff / 全文塞进 content
5. **禁止**把 `display` 写进 `wrap_tool_result` 或 history

---

## 8. 文件索引

| 文件 | 改什么时读 |
|------|------------|
| `src/coara/tool_output/pipeline.py` | 改渲染入口 |
| `src/runtime/spill_policy.py` | 改 spill 阈值 / 预览形状 |
| `src/runtime/spill_read.py` | 改 read(ref) / API 行分页 |
| `src/runtime/tool_output_store.py` | 改落盘 / 批次预算 / 保留清理 |
| `src/core/types.py`（`ToolOutputStoreConfig`） | 改配置字段与默认值 |
| `src/core/read_format.py` | 改 read 行号格式 / ref 默认 limit |
| `src/coara/tool_output/diff.py` | 改 diff 块生成 |
| `src/coara/diff_render.py` | 改 Rich 渲染 / 行数上限 |
| [`CLI_DISPLAY_FRAMEWORK.md`](./CLI_DISPLAY_FRAMEWORK.md) | CLI 三通道（滚动区 / 动态区 / 回合产出） |
