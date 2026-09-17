# coara

[![CI](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml/badge.svg)](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

[官网](https://coara.top) · [使用文档](https://coara.top/docs.html) · [贡献指南](CONTRIBUTING.md)

## coara 是什么

coara 由一个常驻内核构成，整套系统由三个核心机制支撑

- **录像带机制** — 所有端、所有空间、所有会话统一落带存档，只追加、不删除，支持随时回看
- **内核接多端** — 终端 CLI、浏览器 Web UI、手机 App 接入同一常驻内核，上下文共享，跨端接续对话
- **一切皆工作空间** — 消息审批、记录、日报、工作流、界面展示，均围绕工作空间构建

## 一切皆工作空间

工作空间 = 数据 + 投影 + 写入契约。界面是数据在不同视图下的投影，差异仅在于数据本身。

coara 包含三类工作空间，采用同一模型——普通的**对话工作空间**（项目目录）、**系统工作空间**（配置、用量、消息、记录等），以及**开发者工作空间**（如工作流画布一类的 agentic 软件）。简单空间与复杂空间在结构上同构。

每个工作空间内部持有一套**会话**，会话是工作空间与内核之间的连接通道——工作空间不直接与内核交互，而是经由会话接入。

围绕工作空间运转的各项机制，构成如下关系链

| 机制 | 在工作空间中的角色 |
|------|--------------------|
| 事件 | 事件发生于工作空间之内——文件变化、定时到点、webhook 调用、工作流运行，均产生事件 |
| 消息 | 事件发生即产生消息，消息汇入所属空间的动态列表 |
| 记录 | 记录基于工作空间的内容——空间内发生的事务、对话与改动，均记录于该空间名下 |
| 消息审批 | 消息可经 agentic 处理，由用户审批、跟踪与查看，形成完整闭环 |
| 工作流 | 工作流是工作空间的一种运行方式：在空间内编排、触发与执行，运行过程亦产生事件、进而产生消息 |
| 数据投影 | 工作空间的数据经投影呈现于 CLI / Web / 手机，切换空间即切换视图 |

事件 → 消息 → 审批构成闭环；工作流既是事件的消费者（被事件触发），也是事件的生产者（运行产生事件），全程在同一工作空间内完成。

## 主要特点

- **接续对话** — 回复进行中可继续输入，消息排队依次处理，不丢失、不打断
- **多工作空间** — 支持登记多个项目空间，各自持有独立的会话与动态，可随时切换
- **记录与日报** — 跨空间记录自动汇总，每日生成日报，重要事项过目留痕
- **有记录，无记忆** — 不采用黑盒式记忆机制，全部内容留痕可查、可回溯、可掌控
- **工具与技能构建** — 支持按需创建自定义工具包与技能，扩展内核能力

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
