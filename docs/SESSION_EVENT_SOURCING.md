# 会话事件溯源（已落地）

> 状态：**已实施**（2026-08-15 全量改造完成，默认全量回归 1239 passed）。
> 事件日志 `session_events.jsonl` 是全端会话冷备/审计（录像带）；模型历史、
> usage 快照、janitor 快照均为派生视图。无开关、无兼容层。
> **08-31 起 web 聊天区的唯一数据源是 `ui/web_views.py` 视图存储**，不是本录像带投影；
> 录像带退为全端冷备/审计。web 端机制见 docs/架构契约.md「web 端数据载体」。
> 2026-08-21 起加分段轮转：active 写前 ≥32MB 归档为 `session_events.<时间戳>.jsonl`
> （永久保留），读取按 文件名序（=时间序）先归档段后 active 透明拼接。

## 1. 分层模型

```
录像带（事实源）  session_events.jsonl — append-only，永不改写；≥32MB 分段归档
        │  派生
        ├── message_history 恢复（project_session 投影重放）
        ├── usage 快照（session/meta 事件，恢复工具栏上下文）
        ├── janitor 快照（投影为旧格式 JSON，janitor 读法不变）
        └── 审计视图（derive_messages(skip_shadowed=False)，被替代历史可重建）

测量流（外键锚定，独立存储）  usage/events.jsonl、llm-calls.jsonl
        锚定 session_id + turn_id 指向事件源

资产层（独立产品，不进录像带）  records 收藏、daily 日报、ws.md 概况
        生成器从录像带读历史，产出物独立存储/CRUD
```

分层理由：测量数据合进底稿会让全局费用聚合退化；资产需要独立编辑删除，
混进 append-only 流会失去可重放性。

## 2. 事件模型

| 事件 | 写入点 | 说明 |
|---|---|---|
| `turn/start`、`turn/end` | 回合循环 | 边界事件；end 带 reason（completed/interrupted/failed） |
| `user/message`、`assistant/message`、`tool/result`、`system/note` | persist 边界 `sync_history` | 消息事件（见写点纪律） |
| `session/meta` | persist 边界 | usage 快照等会话级元数据，投影取最后一条 |
| `history/shadow` | persist 边界对账 | 历史前缀被替代（压缩/概况替换/工具剥离/回滚）的兜底影子标记 |
| `compaction/summary` | 压缩路径 | 语义归档（影子区间 + 摘要文本 + split_point）；消息事件已在 persist 边界落盘，不重写 |

公共字段：`seq`（全局单调）、`ts`、`kind`、`session_id`、`turn_id`、`coara_id`、`coara_name`、`agent_kind`（main/subagent/flow）。

## 3. 写点纪律（防双写、防漏记）

消息事件**只**在回合落盘边界（`persist_session_to_disk`）经 `sync_history` 写入：

- recorder 维护已记投影（消息指纹列表 + 对应事件 seq）
- 每次落盘把内存 `message_history` 与已记投影做指纹前缀对账：
  - 追加 → 增量事件化补记（幂等：无差异零写入）
  - 前缀分叉 → `history/shadow`（keep_until_seq）+ 分叉后消息全部重写
  - 截断 → shadow 的特殊情形
- 任何历史写入路径（30+ 个 message_history 写点：注入、压缩、控制面剥离、
  概况替换、回滚）都会被对账自动兜住——**不需要逐点挂记录，也不会漏记**

压缩路径额外写一条 `compaction/summary`（语义归档：split_point、摘要文本、
影子区间）——被替代的消息事件本身已在此前 persist 边界落过盘，归档**不重写
消息事件**（避免与对账双写；审计视图经 `skip_shadowed=False` 仍可重建全部原始消息）。

## 4. 恢复（事件重放）

`recover_session`（workspace_state.py）：

- `session_state.json` 索引（session_id + last_updated，旧机制不变）
- 按 session_id 过滤事件 → `project_session` 单遍投影：
  - `history/shadow`：截掉 keep_until_seq 之后的已投影尾部
  - `compaction/summary`：影子区间跳过
  - `session/meta`：取最后 usage 快照
- in-flight 标记注记机制不变（先落盘后清标记的推断仍成立）
- 恢复后 recorder 投影游标对齐（`reset_projection`），
  首次落盘只记增量，不重放全量

## 5. 各模块接入

| 模块 | 接入方式 | 行为变化 |
|---|---|---|
| 回合循环 | turn 边界事件 + persist 对账 | 无（消息事件收敛到一个写点） |
| 压缩 | compaction/* 归档 + 对账 shadow | 历史不再物理丢失 |
| janitor | 快照从事件投影生成（同格式 JSON） | 提示词/读法零变化 |
| usage/llm-calls | 独立测量流，锚定 session_id+turn_id | 无 |
| 子智能体 | 自带 recorder（agent_kind=subagent） | 事件进同一底稿 |
| FlowRoot（工作流第二主体） | `agent_kind=flow`；索引 `flow_session_state.json` | 与主会话同工作空间同条 `session_events.jsonl`；**禁止**写主会话 `session_state.json`；冷启动经 `recover_flow_session` 恢复；图结构仍靠 `orchestrator(save)` 草案（暂不事件化） |
| WebUI | 对话行由**服务端会话视图存储**（`ui/web_views.py`）逐帧落盘 + hydrate 呈现 | **08-31 起 web 聊天区不再读录像带投影**；`view_seq` 自增对账，孤儿回合标 `recovered`。实时显示与刷新恢复读同一份视图存储 |

## 6. 保障

- **底稿可见性**：写失败 warning + `/message` 系统消息（同类失败进程内去抖一次）
- **seq 全局分配**：进程内锁保护，外部截断/删除自动重扫校准
- **旧数据**：`session_history.json` 不迁移，root 启动时一次性清理（含 .tmp）
- **测试**：tests/test_session_log + tests/test_coara/test_workspace_session_history
  覆盖幂等/分叉/截断/往返/in-flight/全量恢复/影子审计
