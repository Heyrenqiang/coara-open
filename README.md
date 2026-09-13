# coara

多智能体运行时，个人 AI 助手的内核。Python 3.11+ / asyncio。

## 它是什么

一个无头内核，里面跑多路工作空间、多路会话，经统一输出路由连接多个显示端，
端可以是终端、浏览器或 Matrix 客户端。智能体在内核上创建并组织子智能体，
带工具、技能、可追踪执行与录像带式的会话存档。

- 内核：回合编排、会话生命周期、输出路由、端注册表
- 端：CLI、Web UI、Matrix 客户端
- 能力：内置工具、技能加载、子智能体委派、工作流、事件源、宝箱加密存储

## 快速开始

```
pip install -e ".[dev]"

coara            # 拉起常驻内核并接入终端
coara status     # 看运行时状态
```

首次启动若无可用 API key，会进入交互式配置向导。配置样例见
`config.yaml.example`、`providers.yaml.example` 与 `.env.example`。

## 目录

- `src/` 内核、端、工具、技能、领域服务
- `tests/` pytest 测试，默认档为冒烟集
- `skills/` 内置技能包
- `docs/` 架构与设计文档，入口 `docs/README.md`
- `wdl/` 独立工作流引擎与画布工作台
- `gomatrix/` 轻量 Matrix homeserver，Go 实现

## 开发

```
pytest tests/ -q          # 默认测试
ruff check . && ruff format .
```

架构约定见 `docs/架构契约.md`，开发总览见 `AGENTS.md`。

## 许可

MIT，见 `LICENSE`。
