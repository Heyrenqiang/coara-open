# Web 搜索模块（`src/tools/builtin/web/`）

## 概述

本模块提供 Coara 的**外部信息获取能力**，包含两个工具

- **`web_search`** — 多 provider 网页搜索（单轮 raw + RRF 融合，默认路径）
- **`web_fetch`** — 单页面内容抓取，支持重试、HTML 转文本、GitHub raw 自动转换

> 架构原则：`web_search` 拆分为**执行引擎**（`raw_tool.py`）和**策略层**（`strategy_tool.py`），两者职责清晰分离。内部 LLM loop 深搜已移除；`strategy_tool.py` 仅负责 query 清洗与单轮/多 query 调度

---

## 文件结构

```
src/tools/builtin/web/
├── __init__.py              # 统一导出公共 API
├── date_extractor.py        # 日期提取与新鲜度评分
├── raw_tool.py              # WebSearchRawTool — 多 provider 执行引擎
├── strategy_tool.py         # WebSearchTool — 策略层（单轮 raw）
├── web_fetch.py             # WebFetchTool — 单页内容抓取
└── README.md                # 本说明
```

---

## 完整数据流

```
主 LLM 调用 web_search(query)
  │
  ▼ WebSearchTool._execute_search()
  │
  ├─ 1. _sanitize_query(query)     剥离旧年份 + 时效词
  └─ 2. 单轮 raw
         raw_tool._execute_search
         │
         ▼ raw_tool._execute_search / _execute_search_multi（仅 2+ query）
         │
         ├─ auto 模式：加权选 top-3 provider，stagger 并发，RRF 融合
         └─ 指定 provider：顺序回退 + freshness 排序
  │
  ▼ ToolResult.success(content, metadata)
```

---

## DateExtractor（`date_extractor.py`）

纯工具类，**零外部依赖**，可从搜索结果的标题/摘要/正文中提取发布时间

| 能力 | 说明 |
|------|------|
| 相对日期 | `"2 hours ago"`、`"3 天前"` → `datetime` + 0.85 置信度 |
| 绝对日期 | `"2026-05-09"`、`"2026年5月9日"` → `datetime` + 0.95 置信度 |
| 新鲜度评分 | `freshness_score(iso_date, window_days=30)` → `[0, 1]` |

> 可被其他工具（如 RSS、新闻聚合器）复用

---

## WebSearchRawTool（`raw_tool.py`）

**执行引擎**。输入一个 `query`，输出经过融合/排序的搜索结果

### 对外接口

- **工具名**：`web_search_raw`（内部使用，不对主 LLM 暴露）
- **参数**：`query: string`
- **执行入口**：`WebSearchRawToolInvocation.execute()`
  - `_execute_search(query)` — 单 query 搜索
  - `_execute_search_multi(queries)` — 2+ query 并发合并（`asyncio.gather`）

### Query 清洗链路

```
原始 query
  → _sanitize_query()
      ├─ _strip_recency_terms()     剥离 "latest/最新/recent/news" 等
      └─ _strip_standalone_year_tokens()  剥离独立年份 "2024"/"2025"
  → _append_current_date_variants()
      ├─ _strip_recency_terms()     二次剥离
      └─ 追加 "最新"(中文) 或 "latest"(英文) 后缀
  → 清洗后的 query
```

### 6 个搜索 Provider

| Provider | 环境变量 | 权重 | 特点 |
|----------|----------|------|------|
| `exa` | `EXA_API_KEY` | 14.39 | 高质量 AI 搜索 |
| `serper` | `SERPER_API_KEY` | 12.28 | Google SERP API |
| `linkup` | `LINKUP_API_KEY` | 8.5 | 结构化数据搜索 |
| `baidu` | 无需密钥 | 6.5 | 中文网页爬取 |
| `doubao` | `DOUBAO_API_KEY` | 10.0 | 豆包搜索 API（火山引擎） |

### 两种执行模式

#### auto 模式（默认）

```
_execute_search(query, provider="auto")
  → _execute_multi_search(query)
      ├── _weighted_provider_order() → 加权随机选 top-3 provider
      ├── staggered 并发（delay 0.5~2.0s 随机）
      ├── RRF 融合：rrf_score + 0.08 * freshness_score
      └── _deduplicate_by_title() + URL 去重
```

#### 指定 provider 模式

```
_execute_search(query, provider="serper")
  ├── 按权重排列 provider，指定 provider 优先
  ├── 顺序回退：serper → exa → ...
  ├── 每个走 _execute_with_query_variants()
  └── _sort_single_provider_results() 按 freshness 降序
```

### 排序公式差异

| 路径 | 排序公式 | 说明 |
|------|----------|------|
| auto 模式（`_execute_multi_search`） | `rrf + 0.08 * freshness` | 多 provider RRF 融合，freshness 微调 |
| 指定 provider（`_sort_single_provider_results`） | `freshness 降序 + 原始位置` | 单 provider 内按新鲜度排序 |

---

## WebSearchTool（`strategy_tool.py`）

**策略层**。组合 `WebSearchRawTool`，对主 LLM 暴露为 `web_search`

### 对外接口

- **工具名**：`web_search`
- **参数**
  - `query: string`（必填）— 搜索关键词

### 执行流程（单轮 raw）

```
1. _sanitize_query() 清洗 query
2. raw_tool._execute_search
```

### 时间注入

| 层级 | 机制 | 位置 |
|------|------|------|
| 工具描述 | 时效检索指引写在 `strategy_tool.py` 的 `_WEB_SEARCH_DESCRIPTION` | 类常量 |
| 代码层 | 强制清洗旧年份 + 追加正确时效后缀 | `_sanitize_query()` / `_append_current_date_variants()` |

---

## WebFetchTool（`web_fetch.py`）

单页面内容抓取工具。与搜索流程无耦合

### 能力

- **URL 规范化**：自动补全 scheme、处理 auth、端口
- **GitHub 自动转换**：`github.com/blob/...` → `raw.githubusercontent.com/...`
- **安全校验**
  - 只允许 `http`/`https`
  - 拒绝内网 IP、localhost、链路本地地址
- **重试机制**：2 次重试，延迟 0.5s / 1.0s
- **HTML 转文本**：去除脚本/样式，保留段落结构
- **内容截断**：默认 12,000 字符，最小 500 字符

---

## 测试

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/test_tools/test_web_fetch.py` | URL 规范化、HTML 转文本、重试、安全校验；WebSearch 回归需手动或 `real_env` |

### 运行测试

```bash
pytest tests/test_tools/test_web_fetch.py -v
```

---

## 配置

`config.yaml` → `web_search.freshness_rrf_weight`（默认 `0.08`）：auto 模式 RRF 排序的新鲜度加权；新闻类 query 可试 `0.15~0.2`

---

## 扩展指南

### 添加新 Provider

1. 在 `raw_tool.py` 的 `WebSearchRawTool` 中新增 `_search_<name>(self, query, count)` 方法
2. 在 `PROVIDER_WEIGHTS` 和 `PROVIDER_ENV_KEYS` 中注册
3. 返回 `list[dict[str, str]]`，每个元素含 `title`, `url`, `description`

### 调整排序权重

修改 `config.yaml` 中 `web_search.freshness_rrf_weight`，或 `raw_tool.py` 中 `_sort_single_provider_results()` 的新鲜度排序逻辑

---

## 注意事项

- `web_search_raw` 是**内部工具**，不对主 LLM 暴露；主 LLM 只能调用 `web_search`
- Provider 并发使用 **staggered delay**（0.5~2.0s 随机），避免触发速率限制
- `DateExtractor` 是**纯函数工具类**，不依赖任何 Coara 内部状态，可安全复用
- `WebSearchTool.description` 定义在 `strategy_tool.py` 的 `_WEB_SEARCH_DESCRIPTION` 常量
