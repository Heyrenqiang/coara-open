# coara

多智能体运行时，个人 AI 助手的内核。Python 3.11+ / asyncio。

## 它是什么

一个无头内核，里面跑多路工作空间、多路会话，经统一输出路由连接多个显示端，
端可以是终端、浏览器或 Matrix 客户端。智能体在内核上创建并组织子智能体，
带工具、技能、可追踪执行与录像带式的会话存档。

- 内核：回合编排、会话生命周期、输出路由、端注册表
- 端：CLI、Web UI、Matrix 客户端
- 能力：内置工具、技能加载、子智能体委派、工作流、事件源、宝箱加密存储

## 安装

```
sh install.sh                      # 建虚拟环境并装好依赖
```

或手动：

```
python -m venv .venv
. .venv/bin/activate               # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Web UI 需要先构建前端（Node 18+）：

```
cd src/ui/web
npm install
npm run build                      # 产物落到 src/ui/static/dist
```

## 跑起来

```
coara                 # 拉起常驻内核并接入终端
coara status          # 看运行时状态
coara tray            # 无头内核 + 系统托盘
```

首次启动若没有可用 API key，会进交互式配置向导。配置样例见
`config.yaml.example`、`providers.yaml.example` 与 `.env.example`。

## 开发模式

内核按可编辑模式安装，改 Python 代码即时生效，不用重装：

```
python -m src.coara                # 直接跑源码，改完重启进程即可
```

Web 前端用 Vite 开发服务器联调，热更新、断点调试都在前端一侧：

```
cd src/ui/web
npm run dev                        # http://127.0.0.1:5173，代理 /ws /api 到内核 8080
```

先起内核再起前端，浏览器开 5173 即可，改前端代码页面自动热更新。

测试：

```
pytest tests/ -q                   # 默认档，冒烟集
pytest tests/ -m extended -q       # 边界与集成档
pytest tests/ --override-ini="addopts=" -q   # 全量
```

## 目录

- `src/` 内核、端、工具、技能、领域服务
- `tests/` pytest 测试，按子系统分目录
- `skills/` 内置技能包
- `docs/` 架构与设计文档，入口 `docs/README.md`
- `wdl/` 独立工作流引擎与画布工作台
- `gomatrix/` 轻量 Matrix homeserver，Go 实现

## 参与

见 `CONTRIBUTING.md`。架构约定见 `docs/架构契约.md`，开发总览见 `AGENTS.md`。

## 许可

MIT，见 `LICENSE`。
