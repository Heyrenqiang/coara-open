# coara

[![CI](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml/badge.svg)](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

[官网](https://coara.top) · [使用文档](https://coara.top/docs.html) · [贡献指南](CONTRIBUTING.md)

## coara 是什么

coara 是一个常驻的无头内核（headless kernel）：内核里跑着多路工作空间与多路会话，经统一输出路由连接多个显示端——端可以是**终端 CLI**、**浏览器 Web UI**，或 **Matrix 客户端**（含手机 App）。

主智能体可以在内核上**创建并组织子智能体**并行干活：工程改动、资料调研分头推进，结果自动回收。它带工具、技能、可追踪执行，以及录像带式的会话存档。

| 维度 | 说明 |
|------|------|
| 内核 | 回合编排、会话生命周期、输出路由、端注册表、子智能体委派 |
| 端 | CLI（终端）· Web UI（浏览器）· Matrix（手机 App / 桌面） |
| 能力 | 内置工具 · 技能加载 · 多智能体 · 工作流编排 · 事件源 |
| 数据 | 全在自己机器上，模型服务商自由切换 |

## 功能特性

- **三端协同** — 电脑上没干完的活，手机上接着问，上下文无缝衔接
- **多智能体并行** — 主助手按需创建子智能体分头推进，前台/后台委派
- **工作流编排** — 画布编排节点，定时、文件变化、webhook 触发执行（独立引擎 `wdl/`）
- **技能系统** — 面向模型的运行时指令包，按需发现、激活、组合
- **自托管** — 一键安装，不依赖任何云服务，数据不出本机

## 快速开始

```bash
git clone https://github.com/Heyrenqiang/coara-open.git
cd coara-open
pip install -e ".[dev]"

coara
```

一条 `coara` 命令拉起常驻内核并接入终端，gomatrix 手机接入层由内核自动托管拉起。首次启动若无可用 API key，会进入交互式配置向导。配置样例见 `config.yaml.example`、`providers.yaml.example` 与 `.env.example`。

Web UI 需要先构建前端（Node 18+）：

```bash
cd src/ui/web
npm install
npm run build    # 产物落到 src/ui/static/dist
```

## 手机连接（扫码配对）

gomatrix 由内核自动托管拉起，无需手动编译或启动。托盘右键「手机连接（二维码）」，手机 App 扫码即连，同局域网直接可连。

## 架构速览

- `src/` 内核、三端、工具、技能、领域服务（[架构契约](docs/架构契约.md)）
- `src/ui/web/` Web UI 前端（Vite + React）
- `wdl/` 独立工作流引擎与画布工作台
- `gomatrix/` 轻量 Matrix homeserver（Go）
- `skills/` 内置技能包
- `docs/` 架构与设计文档，入口 [docs/README.md](docs/README.md)

## 开发

```bash
pytest tests/ -q                    # 默认测试（冒烟集）
pytest tests/ -m extended -q        # 边界与集成档
ruff check . && ruff format .       # lint + 格式化
```

开发总览见 [AGENTS.md](AGENTS.md)，架构约定见 [docs/架构契约.md](docs/架构契约.md)。

## 参与贡献

欢迎提交 issue 与 pull request，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可

[MIT](LICENSE) © 2026 coara contributors
