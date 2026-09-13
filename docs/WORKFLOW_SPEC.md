# WDL 规范（内核投影格式）

**本文件是工作流定稿文本的权威格式规范。**

2026-08-16 起，工作流的唯一核心是**编排图**（`src/workflow/core`）：节点是智能体，
边是拓扑，控制流全部塌缩为图的形状。WDL 文本不再是独立语言——它只是图的
**canonical 序列化投影**（`src/workflow/core/serde.py`），用于定稿落盘、diff、
UI 渲染与外部引擎执行。

- 图模型与语义（wait/kick 边、激活上限）→ `src/workflow/core/semantics.py`
- 引擎侧持久宿主 → `wdl/src/wdl/core/kernel_runner.py`
- Agent 编排指引 → `orchestrator` 工具描述（挂起工具，`tool(action="activate")` 揭示）

## 1. 唯一性保证

一套编排唯一确定一个工作流，由四层锁死：

- **边是唯一拓扑存储**。依赖与路由都是从边集派生的只读视图，不允许独立指定。
- **canonical 序列化**：固定键序、省略默认值、节点按 id 排序、边按 (from, to, on) 排序。
  同一张图 emit 出的文本逐字节相等；`parse(emit(g)) == g` 逐字段恒等（契约测试在
  `tests/test_workflow/test_core_graph.py`）。
- **控制流即拓扑**：并行=扇出、汇聚=扇入、分支=one 路由、循环=回边、重试=error 边。
  图上没有 if/for/while/merge 节点，同一语义只有一种写法。
- **布局纯函数**：UI 由拓扑分层渲染，同图必同布局。

## 2. 文档结构

```yaml
name: 评审流水线            # 必填，unicode 词字符（中文可用）
description: 调研写作评审发布  # 可选
schedule:                    # 可选，缺省 {kind: manual}
  kind: cron
  cron: "0 9 * * *"
max_activations: 200         # 可选，全局激活上限（缺省 100）
nodes:                       # 必填，至少一个节点
  调研:
    task: 调研内核设计
    routes: one              # 可选，缺省 all
  写作:
    task: 写初稿
    input: "{{steps.调研.text}}"   # 数据进模板（隐式依赖）
  评审:
    task: 审阅并选择后继
    routes: one
  发布:
    task: 发布
    max_activations: 5       # 可选，节点级上限覆写
edges:                       # 可选，单节点图可省略
  - { from: 调研, to: 写作 }
  - { from: 写作, to: 评审 }
  - { from: 评审, to: 发布 }
  - { from: 评审, to: 写作 }        # 回边：打回重写（循环）
  - { from: 调研, to: 评审, on: error }  # 失败兜底路由
```

省略规则：`task` 空不写、`input` 空不写、`routes=all` 不写、
`max_activations=None` 不写、`on=success` 不写、`schedule` 为 manual 不写、
`max_activations` 为 100 不写、`description` 空不写。

## 3. 节点（智能体）

| 字段 | 类型 | 说明 |
|------|------|------|
| `task` | string | 完整任务指令（目标/范围/方法/规则/验收/回报格式） |
| `input` | string | 数据进模板：`{{steps.y.text}}` 引用上游节点结果，或字面量种子；运行时叠加上游投递 |
| `routes` | all \| one | 出边路由模式：all=全部投递；one=每次激活选一条 |
| `max_activations` | int | 节点级激活上限（覆写全局） |

节点只有一种：智能体。创建即有身份（id 即名字）与任务。审批等待、确定性调用
都是智能体节点内部的行为，不是图的节点类型。**输入归节点**：每个节点自带
input 字段即输入接口（外部运行传参直接编辑节点配置），图没有全局 inputs 声明。

## 4. 边

| 字段 | 说明 |
|------|------|
| `from` / `to` | 源/目标节点 id |
| `on` | success（缺省，正常完成投递）\| error（失败时投递，重试/兜底路由） |

边分类（运行语义，自动判定）：

- **wait 边**：DFS 树上的前向/交叉边且 on=success。每轮激活需每条 wait 边各一个到达。
- **kick 边**：回边（成环）或 on=error。不参与就绪判定；上游完成时投递到达，
  作为机会性触发把节点再次激活。

## 5. 激活语义

- 首激活：全部 wait 边各有一个到达（源节点等待集为空，立即可激活）。
- 再激活：任一入边有未消费到达（kick 回边到达即触发）。
- 消费：从每条有未消费到达的入边各取一个（FIFO），多余留给下轮。
- 上限：每节点激活次数受 `max_activations`（节点级覆写全局）约束，触顶即失败终态。
  循环边界显式、确定、可终止。
- one 路由：交互宿主（FlowCoordinator）由节点运行期 `report(next=…)` 挑后继；
  无人值守宿主（引擎）按静态顺序取第一条。

## 6. 与旧格式的关系

旧格式（控制流节点 + branches）与互译层已于 2026-08-16 移除：草案与执行
只认本投影格式。手写投影时注意 YAML 1.1 布尔陷阱——裸词 `on` 会被解析为
`True`（serde 已做兼容还原），但建议给含 `on` 的 flow map 加引号：
`{from: a, to: b, 'on': error}`。
