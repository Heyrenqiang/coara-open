# Todo 系统（会话自驱待办）

> 本文定义会话级 `todo(action=read|update)` 的数据模型、持久化、与 Turn 循环的交互。
> 相关：[`CLI_DISPLAY_FRAMEWORK.md`](./CLI_DISPLAY_FRAMEWORK.md)（`todo_update` 展示）· [`技能系统.md`](./技能系统.md)

---

## 1. 职责边界

| 能力 | 用什么 | 说明 |
|------|--------|------|
| 会话内多步自驱 | `todo` 工具 | Root / 子智能体（白名单内）按 session 隔离；`update` 整表覆盖 |
| 需用户审批的规划 | `plan_mode` 工具 | 不是 todo |
| 工作空间触发 / cron | cron 事件源（`<workspace>/.coara/matters/definitions/*.yaml`） | 绑定工作空间别名，非会话 todo |

---

## 2. 架构

```text
todo.py（工具，src/tools/builtin/todo/）
  → registry.py（进程内 TodoStore 缓存 + 每会话 asyncio.Lock）
  → store.py（JSON 持久化，原子写；replace_all 整表替换）
  → types.py / changes.py / display.py / loop.py / open.py / trace_payload.py
```

- **存储路径**：`<workspace>/.coara/todos/{session_id}.json`（按工作空间 + 会话隔离，原子写 `write_json_atomic`）
- **进程内**：`get_todo_store()` 单例缓存 + `get_todo_store_lock()` 串行化同会话读写；两个缓存均为 **64 条 LRU**（`_MAX_CACHE_ENTRIES`），防长会话膨胀
- **跨进程**：依赖原子写；多进程同 session 没有文件锁

## 3. 数据模型（`types.py`）

单个 todo 项字段：

| 字段 | 取值 | 说明 |
|------|------|------|
| `id` | string | 合并主键；`update` 可省略（同 content 续用旧 id，否则自动 `tN`） |
| `content` | string | 必填，步骤描述（宜短） |
| `status` | `pending` / `in_progress` / `completed` / `failed` | 省略默认 pending |
| `priority` | `high` / `medium` / `low`（默认 medium） | **仅控制展示排序** |
| `notes` | string | 结果或卡点 |
| `updated_at` | string（ISO 时间） | 最近一次落盘时间 |

展示排序：`in_progress` → `pending` → `completed` → `failed`，同状态内按 priority 再按 id。

---

## 4. 写入语义（行为契约）

### `update`（整表覆盖）

- `todos` 为**完整**步骤列表；**空数组 = 清空**
- 每条必填 `content`；`status` 省略默认 `pending`
- `in_progress` 可多项（并行任务如实标注）
- 显式 `id` 优先；否则按相同 `content` 续用旧 id，再否则自动编号
- 可选 `explanation`（改计划理由）、`summary`（回执开头摘要）
- 每次必填 `description`（CLI ✓ 行）

### `read`

- 返回当前清单 markdown（工具输出即展示通道）

### 状态转换

软约束表（`ALLOWED_STATUS_TRANSITIONS`）：异常转换允许，仅记 warning；整表替换时对同 id 项做校验。

### 失败处理

持久化失败 → `TodoStoreError` → 工具返回 error。

---

## 5. Model 反馈（`display.py`）

`todo(update)` 成功的 content = 可选摘要 + 紧凑清单行 + `<系统提醒>`：

- 状态标记：`[ ]` / `[•]` / `[✓]` / `[!]`
- 提醒强调：不要在对话里复述整表；完成项点名剩余与下一步

---

## 6. Turn 循环

| 条件 | 决定 |
|------|------|
| 有 tool_calls | 继续 |
| 无 tool_calls，有待办未完成 | 继续并注入未完成清单（提示 `todo(update)`） |
| 未完成但实质长文交付 | 允许结束 |
| 未完成且停滞 | 停止 |
| 无待办 | 结束 |

---

## 7. CLI 展示

CLI 不渲染 sticky 待办面板；工具回执即给人看的通道。executor 仍发 `todo_update` trace。

---

## 8. 刻意不做

| 项 | 原因 |
|----|------|
| 独立 UI 面板 | 工具回执代替 UI |
| CRUD 分 action | 整表覆盖更简单，少 id 漂移 |
