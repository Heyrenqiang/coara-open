# coara

[![CI](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml/badge.svg)](https://github.com/Heyrenqiang/coara-open/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

[官网](https://coara.top) · [使用文档](https://coara.top/docs.html) · [贡献指南](CONTRIBUTING.md)

## coara 是什么

一个常驻内核，录像带式地把所有对话、所有操作、所有会话存档下来。多路显示端——**终端 CLI**、**浏览器 Web UI**、**手机 App**——连接同一个内核，接续对话、连续对话，换端不换脑。内核里一切皆工作空间，消息审批、记录、日报、工作流、UI 展示，全部围绕工作空间生长。

整套系统的核心是三件事

- **录像带机制** — 所有端、所有空间、所有会话一律落带存档，只追加永不删，随时回看
- **内核接多端** — CLI、Web、手机 App 连同一个常驻内核，接续对话、连续对话，不丢话不打断
- **一切皆工作空间** — 消息审批、记录、日报、工作流、UI 展示，都从工作空间这个根上长出来

## 一切皆工作空间

空间 = 数据 + 投影 + 写入契约。界面是数据在不同视图下的投影，差异只在数据本身。

coara 里有三种工作空间，同一模型、不分两种东西——普通的**对话工作空间**（项目目录），**系统工作空间**（配置、用量、消息、记录等），以及**开发者工作空间**（像工作流画布这样的 agentic 软件）。简单空间与复杂空间是同构的。

每个工作空间内部住着一套**会话**，会话是空间与内核之间的连线——空间不直接对话，靠会话接入内核。

围绕空间运转的几套机制，关系是一条链

| 机制 | 在工作空间里的角色 |
|------|--------------------|
| 事件 | 事件发生在空间里——文件变化、定时到点、webhook、工作流运行，都会产生事件 |
| 消息 | 事件发生即产生消息，消息汇入空间的动态列表 |
| 消息审批 | 消息可经 agentic 处理，由用户审批、跟踪、查看，形成闭环 |
| 工作流 | 工作流是工作空间的一种运行方式：在空间内编排、触发、执行，运行又产生事件，进而产生消息 |
| 数据投影 | 空间的数据经投影展示到 CLI / Web / 手机，切换空间即切换视图 |

事件 → 消息 → 审批闭环，工作流既是事件的消费者（被事件触发）也是事件的生产者（运行产生事件），都在同一个空间里完成。

## 主要特点

- **录像带机制** — 所有端、所有空间、所有会话一律落带存档，只追加永不删，随时回看
- **一个内核多端对话** — CLI、Web、手机 App 连同一个常驻内核，上下文共享，换端不换脑
- **接续对话** — 回复进行中可以继续输入，消息排队依次处理，不丢话、不打断
- **工作空间机制** — 登记多个项目空间，各自独立会话与动态，随时切换
- **记录与日报** — 跨空间记录自动汇总，每日生成日报，重要事项过目留痕
- **有记录，无记忆** — 不推崇黑盒式记忆，一切留痕可查、可回溯、可掌控
- **消息审批** — 统一消息路由，破坏性操作弹窗确认，审批送到拥有者
- **工具与技能构建** — 内置全套文件 / 命令 / 网页工具，可按需创建自己的工具包与技能
- **工作空间运行工作流** — 工作流是工作空间的一种工具：在空间内编排、触发、执行，为空间服务（独立引擎 `wdl/`）

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
