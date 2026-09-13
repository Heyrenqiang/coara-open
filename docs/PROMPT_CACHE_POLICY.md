# Prompt Cache Policy（提示词缓存策略）

> **核心约束**：为最大化 LLM provider 的 prompt cache 命中率，**已写入 `message_history` 的前缀内容不得就地修改或删除**，且发送给 LLM 的 `system` / `tools` / `messages` 静态前缀必须逐字节一致。

---

## 1. 原则

1. **System prompt 保持静态**
   同一会话内 system prompt 逐字节不变：模板只渲染一次并缓存（`CoaraBase._static_prompt_cache`，见 `src/coara/base.py::_build_system_prompt`）。动态信息（日期、cwd、todo、后台任务等）一律通过 **message 级注入** 进入上下文，不写进 system 模板。

2. **History 前缀不可变**
   一条 message 进入 history 后，禁止：
   - 编辑、截断或删除它来「撤销」先前的技能激活 / 提醒 / 工具结果；
   - 在中间插入 message 导致前缀位移（`/new` 全量重置除外）。

3. **会话内不撤销技能**
   已 activate 的技能指南留在 history 中；需要换指南时用 `/new` 开新会话。不要从历史中删掉 activate 的 tool result。

4. **新会话边界**
   `/new` 或 `start_new_session()` 清空 `message_history` 与技能会话状态，重新发现技能（`load_skills()`）。system prompt 不注入动态技能清单，按需 `skill(action="search"|"activate")`（见 [`技能系统.md`](./技能系统.md)）。

5. **工具 / 系统定义保持前缀一致**
   tool definitions 与 system content 是 prompt cache 的「前缀」。以下做法会让缓存失效：
   - tool `description` / `parameters` 在请求间变化（含字段顺序变化——JSON 语义等价但缓存按字节匹配）；
   - system prompt 混入动态变量；
   - tool 数组顺序不稳定，或工具集合在无用户操作时变化。

## 2. 与技能设计的关系

| 内容 | 落点 | 变更方式 |
|------|------|----------|
| 技能列表（name + description） | `skill(action="search")`（query 留空）的 tool result | 上下文中已有则勿重复调用 |
| `activate` 的完整正文 | tool result（`<系统提醒>` + `<activated_skill>`） | 追加；`/new` 清空会话状态 |

---

## 3. Provider 专项

### 3.1 Anthropic / MiniMax（Anthropic 兼容）

coara 对所有 Anthropic 端点（官方与兼容端点，如 MiniMax）放置 **3 个** `cache_control: {type: ephemeral}` 断点（上限 4 个），实现在 `src/llm/anthropic.py`：

1. **tools 末尾**（`_convert_tools`）——缓存全部工具定义，要求工具定义逐字节稳定；
2. **system 末尾 block**（`_apply_system_cache_control`）——缓存完整 system prompt；
3. **最后一条 message 的最后一个可缓存 block**（`_apply_message_cache_control`）——多轮对话的增量缓存。

token 口径（`src/llm/usage.py`）：

- `total_prompt_tokens()` = `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`（MiniMax 的 `input_tokens` 不含 cache 部分，需三项相加）；
- `prompt_cache_hit_ratio()` = `cache_read_input_tokens / total_prompt_tokens`。

### 3.2 MiMo（OpenAI 兼容）

MiMo **不需要**客户端传 cache 开关或 `cache_control` 断点——服务端对相同前缀自动命中。coara 不向 MiMo 请求注入任何 cache 相关字段。

响应 usage 形态：

```json
"usage": {
  "prompt_tokens": 19744,
  "completion_tokens": 72,
  "prompt_tokens_details": { "cached_tokens": 18000 }
}
```

| 字段 | 含义 |
|------|------|
| `prompt_tokens` | 整段 prompt 总 token（**已含** cache 命中部分） |
| `prompt_tokens_details.cached_tokens` | 其中从 cache 读取的 token 数 |

**不要**像 MiniMax 那样做三项相加——MiMo 的 `prompt_tokens` 已是全量。命中率 = `cached_tokens / prompt_tokens`（`usage_dict_from_openai()` 把 `prompt_tokens` 归一为 `input_tokens`）。

coara 对 MiMo 的对接项：

| 能力 | 实现 |
|------|------|
| 静态 system prompt | `CoaraBase._static_prompt_cache` |
| `reasoning_content` 回写 history | assistant 消息的 `reasoning_content` 全量保存；MiMo 端点的 `_convert_messages` 原样回传（`src/llm/openai.py`） |
| 解析 `cached_tokens` | `src/llm/usage.py::usage_dict_from_openai` |
| 状态栏总输入 / cache % | `prompt_cache_hit_ratio()`（cached_tokens 分支） |

注意：history 中存在带 `tool_calls` 的 assistant 消息时，后续请求需携带同轮 `reasoning_content`，否则 MiMo 可能返回 400。

命中率预期：首轮通常较低；长会话 tool loop 可达 90%+。破坏命中率的行为：`/new` 重开、改写 system、中间插入 message、丢失 `reasoning_content`。

### 3.2b Kimi K3（OpenAI 兼容端）—— dynamic tool loading

Kimi K3（`api.kimi.com/coding/v1`，OpenAI 协议）支持**动态工具加载**（[官方文档](https://platform.kimi.ai/docs/guide/use-dynamic-tool-loading)）：把完整工具定义放进消息流里一条**无 content 的 system 消息**（`{"role":"system","tools":[...]}`），工具从该消息位置起生效；声明**只追加不插入**，不破坏前缀缓存。

coara 的挂起工具 activate 借此落地（仅 kimi+k3 生效，其它 provider 不受影响）：

| 层 | 实现 |
|----|------|
| 声明载体 | `<工具声明>` 标签包裹 `{"tools":[schema]}` JSON，进 user 角色历史（`src/core/message_tags.py::tool_declaration`） |
| 请求改写 | kimi OpenAI 驱动转换时识别标签 → 改写成 `system+tools` 电线消息（`src/llm/openai.py::_convert_messages`，仅 k3 模型；非 k3/其它 provider 按普通文本透传，语义无害） |
| activate | `tool(activate)` 追加声明消息到历史尾部（`src/tools/builtin/integration/tool.py::append_tool_declaration`），tools 数组从此不再因 activate 变化 |
| 恢复补发 | 重启恢复 revealed 时对账：历史缺声明则补发（`ToolManager._reissue_dynamic_declaration`），防 `/new` 后工具半残；已有声明不重复（K3 重复声明 = HTTP 400 duplicate tool name） |

实测行为（2026-08-22 探针）：声明后可调 / 重复声明 400 / 全局与动态共存双向可调 / 隔轮（声明留在历史中）仍可用。

缓存口径：activate 不再是前缀破坏源（此前 activate = tools 数组变化 = 全量重算 ~3% 命中）。官方约束「Append, never insert」——声明追加后**不得修改历史中的声明消息**（会 invalidate 该点之后缓存）。

注意：DeepSeek 无此机制（官方 Function Calling 仅顶层 tools 字段）；其 Context Caching 是磁盘公共前缀持久化（请求边界/公共前缀检测/固定间隔），activate 炸缓存后可自愈，kimi 严格前缀匹配不自愈——这正是动态加载在 kimi 上价值最高的原因。

### 3.3 MiniMax / Kimi thinking 块的保留

MiniMax 平台要求多轮 tool use 中 assistant 响应（含 `thinking` / `text` / `tool_use` 块）完整追加，且 `thinking` 块在后续请求中原样回传。Kimi K3（Anthropic 兼容端点）有同样要求，缺失回传会导致下一轮 HTTP 400（`src/llm/endpoints.py::preserves_anthropic_thinking_wire` 同时覆盖两家）。coara 的分层：

| 层 | 存什么 |
|----|--------|
| `message_history.content` | 仅用户可见文本 |
| `Message.provider_wire_blocks` | MiniMax `thinking` 块（API 回传专用，不进对话 content） |
| anthropic `_convert_messages` | 发送前合并 `provider_wire_blocks` + `content` + `tool_calls`（`src/llm/message_content.py`） |

跨 provider 切换（MiniMax → OpenAI 兼容）时 `provider_wire_blocks` 不会被对方识别，推理链丢失——同一会话尽量固定 provider。

### 3.4 观测

- `src/coara/llmlog.py::_usage_record()` 把 `cache_hit_ratio`（OpenAI 系）与 `anthropic_cache_hit_ratio`（Anthropic 系）写进每次 LLM 调用的记录；
- 每工作空间 × 每智能体的最后一轮完整快照落盘 `.coara/llm/llm-calls.jsonl`（每实例一行全文），由独立开发者工具 `coara-devtools` 查看。

---

## 4. 上下文消息标签与注入形态

所有运行时注入的上下文消息使用**中文 XML 标签**（识别逻辑：`src/coara/llmlog.py::_is_context_user_message`）。标签 helper 在 `src/core/message_tags.py`；`<工作空间消息>` 的解析在 `workspace_message_injector.py`。`<state_snapshot>` 是保留的英文标签（上下文压缩，见 `src/context/window.py`）。

| 标签 | 用途 |
|------|------|
| `<系统消息>…</系统消息>` | 信息性上下文，不强制行为 |
| `<系统提醒>…</系统提醒>` | 权威指令，必须遵守 |
| `<事件提醒>…</事件提醒>` | 外部事件 / webhook |
| `<工作空间消息>…</工作空间消息>` | 工作空间动态引用 |
| `<引用内容>…</引用内容>` | 用户引用的会话上文 |
| `<手机消息>…</手机消息>` | 手机端（Matrix）入站消息（08-31 起按 source 现包；CLI 无标签） |
| `<web消息>…</web消息>` | Web 端入站消息 |
| `<接续输入>…</接续输入>` | 当前 turn 运行中用户的接续输入 |
| `<工具声明>…</工具声明>` | K3 dynamic tool loading 的工具定义声明载体（仅 kimi OpenAI 驱动改写；见 §3.2b） |
| `<state_snapshot>…</state_snapshot>` | 上下文压缩摘要 |

### 4.1 USER 写入形态

`message_history` 中真人输入与运行时注入**同为 `role=USER`**，API 不区分来源。两种形态：

- **主 USER 正文**：`process_message` 入参组成的一条 USER 消息，标签与用户打字可拼在同一字符串内（一般标签在前）；
- **追加 USER 消息**：同轮或异步再写入的另一条 USER 消息。

**主正文内常见标签**：

| 标签 | 组装 / 触发 |
|------|-------------|
| `<工作空间消息 …>` | 客户端引用的工作空间动态拼入；检测到后另追加一条动态 `<系统提醒>` |
| `<引用内容>` | 客户端拼入 |
| `<手机消息>` | 手机端入站整段包裹（08-31 起按 source 现包，`src/core/message_tags.py`；CLI 无标签） |
| `<web消息>` | Web 端入站整段包裹 |
| `<事件提醒>` | 事件源事件内容渲染（`event_sources/formatter.py`），落工作空间动态收件箱 |

**独立 USER 消息**：

| 标签 / 内容 | 典型触发 |
|-------------|----------|
| `<系统消息>` | 环境/概况/AGENT 等上下文模块、后台任务完成进驻会话历史 |
| `<情境>` | 情境后缀（当前对话端 + situation.md），每轮现取、只挂本轮 LLM 输入末尾、不进历史 |
| `<系统提醒>` | 用户规则（user_rules）、Vision 图片提醒、rules glob、工作空间动态指引、shell 输出哨兵匹配 |
| `<state_snapshot>` | 上下文压缩 |
| 无标签纯文本 | 压缩后的待办 / 后台快照 |

同轮注入顺序（`src/coara/turn_loop/user_turn_injectors.py`）：前缀上下文模块 → 跨天注记 →（Flow/Config 概况）→ 主正文 → Vision 提醒 → 工作空间动态指引 → rules。（`@-mention` 文件注入已于 2026-08-31 退役，CLI `@服务台` 走 attach 服务台投递。）每轮送模型前另可 ephemeral 挂 `situation.md` 于对话末尾（`context_prep`）。

### 4.2 环境种子（上下文模块）

- 注入条件：当前 `message_history` 中**尚无任一模前缀种子**时注入一次；以伪造 USER 消息形式进入 history，保持 system prompt 静态。默认拆成多条：用户规则 → 环境上下文 → AGENTS.md → ws.md（缺文件则跳过；概况缺文件用占位句）。
- 环境条内容：日期、cwd、平台、位置行等；位置行可经 `config.yaml` 的 `environment.location` 配置（默认「江西赣州信丰」，空字符串则省略该行）。
- Git 快照（`git status -sb`）只对 `coaras` 子智能体注入（`include_git=True`）。
- 顺序/开关：`<coara_home>/system/context_modules.yaml`（devtools 可改）。

### 4.3 工具回传包装

成功且有正文 → 原样进 history（不加包装）；失败 / 空 / 非文本 → 包 `<系统消息>`（`src/coara/injections/tool_result_wrapper.py`）。`skill(activate)` 成功的 content 是 `<系统提醒>` + `<activated_skill>`。

### 4.4 实现要点

| 行为 | 要点 |
|------|------|
| 打断 | 取消时保留本轮已入史内容，并为未完成 tool_calls 补「已取消」结果；文件系统副作用不回滚 |
| Todo 续跑 | 待办未完成且 LLM 无 tool call 时继续内部循环（见 [`TODO_SYSTEM.md`](./TODO_SYSTEM.md)） |
| Shell 输出哨兵 | EventBus → Root 注入 `<系统提醒>`；不自动开新 turn；CLI 另打黄字通知 |

### 4.5 源码索引

| 主题 | 路径 |
|------|------|
| 用户轮注入链 | `src/coara/turn_loop/user_turn_injectors.py` |
| 标签 helper | `src/core/message_tags.py` |
| 动态 / shell / 后台 | `workspace_message_injector.py` / `background_injector.py` |
| 压缩与快照 | `src/context/window.py`、`injections/snapshot_injector.py` |
| 远端 | `matrix_client/ingress_helpers.py`、`coara/remote_turn.py` |
| usage 归一化 | `src/llm/usage.py` |
| LLM 调用日志 | `src/coara/llmlog.py` |

---

## 5. 反模式

- ✗ 从 history 中删除 `activate` 的 tool result 以「取消技能」
- ✗ 每轮把日期等动态变量写进 system prompt（应走环境种子或 message 注入）
- ✗ 上下文压缩后丢失 tool_call / tool_result 配对（`ContextWindowManager` 的切分点逻辑负责保证配对完整）
- ✗ 工具 `description` / `parameters` 动态生成且不稳定（依赖随机 ID、时间戳、可变列表）
- ✗ 同一工具在不同请求中字段顺序不同
- ✗ 工具集合在无显式操作时变化，导致 tools 前缀不一致（kimi+k3 已用 dynamic tool loading 消解，见 §3.2b；其它 provider 仍受此约束）

---

## 6. 环境感知后缀（构想，未实现）

> 「后缀可变性」是「前缀一致性」的互补面：前缀（历史 + 静态 system）保持逐字节稳定以命中
> prompt cache，**可能动态变化的环境信息全部放到上下文最后面**，每次请求更新，不影响前缀缓存。

### 6.1 结构

上下文形态：`a1 a2 … an s`——a 是历史消息（前缀，稳定可缓存），s 是环境感知后缀（每次请求可更新）。

### 6.2 目标

让 LLM 实时感知环境变化——哪些文件被外部 / shell 改动、用户动作、事件流、时间等，
避免「凭旧记忆操作已变化的文件」一类问题；同时不破坏前缀缓存。

### 6.3 设计要点

- 后缀放在历史尾部（独立 USER 消息，标签见 §4），**绝不写进 system prompt**——system 是前缀，写入即缓存失效；
- 只收集「非 LLM 自致」的变化：外部文件改动、shell 命令效果（不可预知）、用户动作、事件流。LLM 自致的 write/edit 参数可追溯，无需提醒（见 `src/tools/builtin/file_io/edit.py` 的编辑前自查提醒）；
- 注入时机：每次请求前生成快照，或仅在有变化时注入（后者省 token）；
- 成本需评估：全量文件扫描过重，宜用轻量 diff / `git status` / 事件流作为信息源。

### 6.4 状态

构想，**暂不执行**。与 edit 工具描述的「先读取最新内容再编辑」提醒互补：描述提醒是零成本兜底，
后缀系统是系统性环境感知，留待 prompt caching 深化时一并评估。

