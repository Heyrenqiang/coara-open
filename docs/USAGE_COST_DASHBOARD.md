# 用量费用统计与展示方案

> 定位：本文是「LLM 用量费用统计」的设计文档，承接 `TOKEN_COST_MODEL.md` 的定量模型，
> 把费用计算、价格配置、数据链路与三端展示（先 WebUI）落到可执行的方案。
> 状态：已实施（2026-08-14，PC 端 + WebUI 第一期；2026-09-12 手机端用量页落地：
> 日历热图定时间范围（点选日 / 整周 / 整月）+ 主维度（空间/智能体/模型）切换与行内交叉下钻，
> 数据走 `/api/usage/range` 任意日区间聚合，一次请求带回三组交叉拆分）。
> 2026-08-26 起展示口径**单源化**：金额/词元/命中率/费用状态统一由后端
> `src/runtime/usage_display.py` 算好（`*_display` 等字段），Web / Android / devtools
> 只渲染，不再各自用 TS/Kotlin/JS 实现格式化。

## 1. 背景与定位

录像带理念（借鉴 `ref-doc/deepseek-harness-借鉴.md` §事件溯源）：会话事件流是唯一事实源，
用量、费用、记忆、errorlog 都是从它抽取/投影的**派生视图**，且每条派生数据可溯源回原事件。

coara 的用量事件流是 `usage/events.jsonl`（`src/runtime/usage_collector.py` 从 EventBus 订阅
`llm_turn_complete` / `llm_turn_partial` / `tool_complete` 落盘）。费用统计不改事件流本身，
只在其上新增**派生计算**：每小轮（一次 LLM 调用）按价格表算出命中/非命中/输出三笔费用，
向上聚合成大轮、会话、智能体、模型、工作空间、天的消费视图。

目标：每个模型、每个会话、每个智能体、每一大轮、每一小轮的消费清清楚楚，
为「LLM 看自己的 log 算成本并自动优化」打底。

## 2. 数据基础（现状核验）

`usage/events.jsonl` 的 `llm_turn` 记录（`usage_collector.py::_record_llm_turn`）已有字段：

- 归属：`session_id`、`coara_id`、`coara_name`、`agent_kind`、`persona`、`workspace_id/name/dir`
- 模型：`provider`、`model`
- 小轮序号：`iteration`（一次大轮内第几次 LLM 调用）
- token 明细：`usage.input_tokens / output_tokens / total_tokens / cached_tokens /
  cache_read_input_tokens / cache_creation_input_tokens / reasoning_tokens`
- 状态：正常回合无 `status`，流式中途失败记 `status=partial`
- 大轮标识：`turn_id`（已由 `turn_orchestrator` 写入 `llm_turn_complete` / `llm_turn_partial`，collector 落盘）

历史事件若无 `turn_id`，明细聚合时并入 `turn_id=""` 的「历史记录」组，费用照常计算。

## 3. 费用口径（跨 provider 统一）

### 3.1 token 语义

- 有效输入 `total`：`src/llm/usage.py::total_prompt_tokens` 已实现——
  有 cache 字段时 `total = input_tokens + cache_read + cache_creation`；
  OpenAI/MiMo 风格 `input_tokens`（= prompt_tokens）已含全部输入，cache 字段为 0，`total = input_tokens`
- 命中量 `cached`：`src/llm/usage.py::cache_read_tokens` —— `cache_read_input_tokens` 优先，否则 `cached_tokens`
- 创建量 `created`：`cache_creation_input_tokens`（Anthropic/MiniMax 风格才有；OpenAI 风格为 0）。
  只参与有效输入口径，**不单独计价**——创建部分并入非命中按 `input` 价计
- 输出 `output`：`output_tokens`

### 3.2 价格表（元/百万 token）

| 价 | 字段 | 缺省 |
|---|---|---|
| 输入（含缓存创建） | `input` | 必填 |
| 缓存命中输入 | `cache_hit` | 必填 |
| 输出 | `output` | 必填 |

### 3.3 费用公式（每小轮）

```text
非命中费 = max(0, total − cached) × input
命中费   = cached × cache_hit
输出费   = output × output
小轮费   = 非命中费 + 命中费 + 输出费
```

对两种 provider 风格均成立：

- Anthropic/MiniMax：`total = input + cache_read + cache_creation` → 非命中部分 = `input + cache_creation`
  （原输入 + 创建，统一按 `input` 价），命中部分 = `cache_read` ✓
- OpenAI/MiMo：`total = prompt_tokens`（含缓存），`cached = cached_tokens` → 非命中部分 = `prompt − cached` ✓

未配价格表的模型：只记 token、费用记 0，页面标注「未配置价格」。

## 4. 价格配置（providers.yaml）

价格按**模型**配置（不同模型价格不同），挂在 `providers.<name>.models.available[]` 条目：

```yaml
providers:
  deepseek:
    models:
      available:
        - id: "deepseek-flash"
          name: "DeepSeek V4.1 Flash"
          function_calling: true
          pricing:                     # 元/百万 token
            input: 1.5
            cache_hit: 0.03
            output: 6.0
```

- 模板 `deploy/gitee/templates/providers.yaml` 给 DeepSeek Flash 填文档依据价
  （峰谷均值），其余 provider 不预填，避免误导
- 读取实现：新增 `src/runtime/usage_pricing.py`，从 `config_manager` 构建
  `{provider/model → ModelPricing}` 映射（与聚合的 `model_key = f"{provider}/{model}"` 对齐），
  每次聚合时现读现算（价格是配置、随时可改，不做持久化快照）

## 5. 数据链路改动

| 位置 | 改动 |
|---|---|
| `turn_orchestrator.py` | `llm_turn_complete` / `llm_turn_partial` payload 加 `turn_id` |
| `usage_collector.py` | `_record_llm_turn` / `_record_llm_partial` 的 record 加 `turn_id` |
| `usage_pricing.py` | 价格表读取 + `compute_turn_cost(usage, pricing)` |
| `usage_query.py` | `_TokenBucket` 加费用累计；`_add_llm_turn` 按 provider/model 查价计费；`_token_dict` 输出费用字段并注入展示字段；`summarize_usage_dashboard` 返回 `totals.cost_*` 与各分组行 `cost_*` + `*_display` |
| `usage_display.py` | **单源展示层**：`format_usage_money` / `format_token_count`（词元一律 M）/ `format_hit_rate` / `cost_state` / `cache_hit_level` / `format_ts_display`；`decorate_token_totals` 给 bucket 行统一追加 `*_display`、`cache_hit_pct`、`has_cost_breakdown` |
| `web_server.py` | 新增 `GET /api/usage/detail?days=&workspace_id=`（明细，见下） |

### 5.1 明细端点 `GET /api/usage/detail`

返回最近 N 天的消费明细，层级：会话 → 大轮 → 小轮（数据量大，逐层收敛）：

```json
{
  "days": 7,
  "sessions": [
    {
      "session_id": "…",
      "coara_name": "root" ,
      "agent_label": "主会话",
      "llm_turns": 12,
      "cost_total": 0.0123,
      "turns": [
        {
          "turn_id": "…",
          "ts": "…",
          "cost_total": 0.0012,
          "iterations": [
            {
              "iteration": 1,
              "model": "deepseek-flash",
              "provider": "deepseek",
              "model_label": "deepseek·deepseek-flash",
              "ts": "…",
              "ts_display": "2026-08-08 10:00",
              "cost_miss": 0.001,
              "cost_hit": 0.0001,
              "cost_out": 0.0001,
              "cost_total": 0.0012,
              "cost_total_display": "¥0.0012"
            }
          ]
        }
      ]
    }
  ],
  "totals": { "sessions": 3, "turns": 5, "llm_turns": 12, "…": "含 *_display 展示字段" }
}
```

- 无 `turn_id` 的历史记录：并入 `turn_id=""` 的「历史记录」组（大轮层退化），费用照常算
- 限制：默认按 ts 倒序取最近 500 个小轮，防止明细无限膨胀
- 会话 / 大轮 / 小轮每层都带 `*_display` 展示字段；`totals` 为本次返回明细的合计
  （只覆盖返回的 limit 条，供 devtools 头部行直渲）

## 6. WebUI 展示（第一期）

`src/ui/web/src/views/UsageView.tsx`（`fetchUsageDashboard` 返回结构 `api.ts` 的
`UsageDashboardResponse` 同步扩展）。设计原则：**钱是第一关注点，一眼看到花了多少、花在哪**。

### 6.1 总览卡

- 布局：左侧保留缓存命中率仪表盘；右侧改为「总费用」大数字居中（¥ 前缀、大字号、主色），
  下方小字展示三笔拆分（非命中 / 命中 / 输出），与左侧仪表盘视觉等高对齐
- 未配价模型占比较高（有 token 无价格）时，费用数字旁显示黄色小角标「部分模型未配价」

### 6.2 表格费用列

- 四张表（按调用方 / 模型 / 日 / 工作空间）行尾加「费用」列：右对齐，格式见 §6.4 单源规范
- 表格按后端下发顺序渲染，费用列支持点击排序（前端 sorter 用原始 `cost_total`，非展示逻辑）
- 行费用状态由后端 `cost_state` 给出：`priced` 主色金额 / `unpriced` 置灰「未配价」标签 / `zero` 显示 ¥0

### 6.3 消费明细（Collapse 手风琴）

- 新增「消费明细」Card，antd `Collapse` 三级嵌套，默认只展开第一层：
  - 第一层 会话：`会话标签 · N 轮 · ¥合计`，副标题显示时间窗与智能体
  - 第二层 大轮：`时间 · ¥合计`，副标题 `M 个小轮`
  - 第三层 小轮：展开显示该轮 模型 / provider / 时间，下方三项费用成行
    （非命中 ¥x · 命中 ¥z · 输出 ¥w）与加总行，命中项绿色、非命中项中性色、
    输出项主色——三色一眼看出缓存省了多少
- 加载用骨架屏，空态提示「该时间范围内没有 LLM 用量」；明细最多 500 小轮，超出显示
  「仅显示最近 500 条」

### 6.4 数字规范（单源，2026-08-26 起）

所有展示文案在 `src/runtime/usage_display.py` 算好，前端（Web / Android / devtools）
只渲染 `*_display` 字段，不得再对原始数值做格式化：

- 金额 `format_usage_money`：`¥` + 千分位；≥100 整数，≥1 两位小数，≥0.0001 四位小数，
  极小 `<¥0.0001`，零/负 `¥0`
- 词元 `format_token_count`：一万以下千分位；一万及以上一律折算 M
  （`0.01M` 起，≥1M 一位小数去尾零，如 `1.2M` / `5M`），不再使用 k / 万 / 亿
- 命中率 `format_hit_rate`：0–100 截断一位小数（`64.8%`）；`cache_hit_pct` 数值供进度条宽度；
  `cache_hit_level`（≥50% good）驱动配色
- 费用单元格 `cost_state`：`priced`（蓝色金额）/ `unpriced`（有输入未配价，「未配价」标签）/ `zero`
- 时间 `format_ts_display`：本地 `YYYY-MM-DD HH:MM`；时间窗 `format_day_display`：`YYYY-MM-DD`
- 颜色语义：命中=绿（省钱）、非命中=中性、输出=主色，全页一致
- 所有费用均标注「按配置价格估算，以 provider 账单为准」

## 7. 边界与严谨性

- **费用是估算**：token 数来自 provider usage 响应，价格为配置值；实际扣费以 provider 账单为准。
  页面加注「按配置价格估算，以 provider 账单为准」
- **历史数据**：无 `turn_id` 的大轮层退化，费用照算（token 字段齐全）
- **partial 轮次**：`status=partial` 照常计费（provider 已计量部分 token），与正常回合同一口径
- **未配价模型**：费用 0，展示提示
- **cache_creation 差异**：OpenAI 风格为 0；Anthropic 风格的创建量并入非命中按 `input` 价计，
  不再单独配置/展示创建价（2026-08-27 起）
- **缓存失效**：`cache_read` 少则命中费低、非命中费高，费用视图自然反映缓存收益，
  与 `TOKEN_COST_MODEL.md` 的「缓存是第一杠杆」结论一致

## 8. 后续

- 手机端用量模块已落地（2026-09-12）：`/api/usage/range`（`summarize_usage_range`）按任意日区间
  聚合，除总览/按日/三维分组外还产出 `workspace_agents` / `workspace_models` / `agent_models`
  三组交叉，端上一次请求即可做主维度切换 + 行内下钻；会话→大轮→小轮明细仍走 `/api/usage/detail`
  （费用明细也可在 coara-devtools 的 LLM log 按小轮/回合查看）
- 滚动 N 天看板（`/api/usage/dashboard`）继续服务 Web；手机端不再消费它
- LLM 自省：把会话费用摘要注入上下文，让 agent 看到花费后自动优化（降轮次、护缓存）
- WebUI 配置页：模型价格可视化编辑（一期在 providers.yaml 手改）
