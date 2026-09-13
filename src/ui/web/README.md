# coara Web UI

统一的 Web 界面：对话、文件/工具查看器、记录、消息、用量、配置。设计契约见 [`docs/Web设计体系.md`](../../../docs/Web设计体系.md)。

## 架构

```
coara --web (单进程)
├── RootCoara (内嵌)
│   ├── EventBus / TraceStore (直接订阅)
│   ├── WebRemoteInteractionChannel (WS 交互通道)
│   └── Commands Service (src/coara/commands)
│
├── aiohttp server
│   ├── GET /          → SPA index.html
│   ├── /static/*      → Vite 构建产物
│   ├── /ws            → 类型化 WS (chat/cmd/interaction/trace)
│   └── /api/*         → REST (state/trace/files/upload/config/...)
│
└── React SPA (Ant Design 5)
    ├── 对话 (流式 + Markdown + 工具卡片 + 右侧状态栏)
    ├── 文件/工具查看器 (目录树 + 内容预览 + 工具全量输出)
    ├── 记录 (笔记 / 收藏 / 最近)
    ├── 消息 (系统动态过目)
    ├── 用量 (Token 与费用)
    └── 配置管理
```

## 功能模块

### 对话 (`/chat`)
- 实时流式对话
- Markdown 渲染（代码高亮、表格、列表）
- 工具调用折叠卡片（参数 + 结果）
- 斜杠命令支持（`/` 自动补全）
- `@` 工作空间名称提及补全
- 图片/文件上传（Vision）
- 会话历史恢复（刷新不丢失）
- 中断/停止按钮

### ☰ 文件显示页 (`/file?path=...`)
- 聊天输出与工具行里的文件路径可点击直达（无导航菜单入口）
- 文本/代码高亮预览（分页「加载更多」；Markdown 可切换渲染/源码）
- 图片 base64 直显、Office 转 Markdown 渲染
- PDF 内嵌、音视频直链播放、其余二进制下载
- 完整路径一键复制、强制下载按钮

### 记录 (`/records`)
- 三个页签：笔记（janitor / daily 写入）· 收藏 · 最近
- 左侧列表 + 右侧详情，按日期分组折叠
- 搜索、类型筛选、含归档开关

### 消息 (`/review`)
- 系统动态过目（提醒 / 文件变动 / Webhook / 工作流 / 动态）
- 显著性 / 工作空间 / 状态筛选
- 单条标记已读、归档、转处置

### ☰ 用量 (`/usage`)
- Token 用量与估算费用（命中率 / 输入 / 输出 / 按模型 / 按日 / 按工作空间）
- 模型价格可编辑；时间窗与工作空间可切

### ⚙ 配置管理 (`/config`)
- Provider 与模型、通用、技能、压缩、事件源、Matrix、安全等分页签
- 配置助手浮窗：用自然语言改配置

### ☰ 工具调用详情 (`/file?view=tool&call_id=...`)
- 状态栏「最近工具调用」双击直达
- 摘要 / 参数 / 变更（diff）/ 输出分区；大输出分页加载

## 开发

```bash
# 安装依赖
cd src/ui/web && npm install

# 开发模式（需要先启动后端：coara --web）
npm run dev

# 构建
npm run build
```

构建产物输出到 `src/ui/static/dist/`，aiohttp 直接 serve。

## 技术栈

- React 18 + TypeScript
- Ant Design 5（UI 框架）
- React Router v6（路由）
- Zustand（状态管理）
- react-markdown + rehype-highlight（Markdown 渲染 + 代码高亮）
- Vite 5（构建工具）

## WS 协议

单 WebSocket 连接，消息按 `type` 路由。

### Client → Server
- `chat` — 发送对话消息
- `interrupt` — 中断当前回合
- `command` — 执行斜杠命令
- `approval_reply` — 审批回复

### Server → Client
- `turn_start` / `chunk` / `turn_end` — 对话流
- `tool_call` / `tool_result` — 工具调用事件
- `command_result` — 命令结果
- `approval` — 交互弹窗
- `state` — 状态快照
- `error` — 错误
- `info` — 信息

## REST API

### 文件系统
- `GET /api/workspace/list` — 工作空间列表
- `GET /api/workspace/file?path=...&offset=...&limit=...` — 文件/目录内容（text/image/office/binary/directory）
- `GET /api/workspace/file-raw?path=...[&download=1]` — 文件直出（inline / 强制下载；目录不可用）
- `POST /api/workspace/switch` — 切换工作空间

### 上传
- `POST /api/upload` — 文件上传（multipart）

### 会话
- `GET /api/session/messages` — 消息历史（权威 hydrate；详见 [`docs/WEB_DISPLAY.md`](../../../docs/WEB_DISPLAY.md)）
- `POST /api/session/new` — 开始新会话（顶栏「新会话」；Web 斜杠 `/new` 已拦）

### 自动补全
- `GET /api/commands` — 斜杠命令列表 + CLI 同款二级 pickers（`/ws` `/model` …）

### 复用 Dashboard
- `GET /api/state` — 运行状态
- `GET /api/trace-detail` — Trace 详情
- `GET /api/v1/config` — 配置
- `GET /api/v1/providers` — Provider 列表

### WDL 草案（生产侧；执行归独立 WDL 软件 wdl/）
- `GET /api/workflow-drafts` — 草案列表
- `POST /api/workflow-wdl/parse` / `emit` — WDL 投影解析/生成

后端入口：`src/ui/web_server.py`
