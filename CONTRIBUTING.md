# 贡献指南

感谢你愿意参与。这份指南说清三件事：怎么提问、怎么改代码、怎么提交。

## 一、动手前先读

- `README.md` 安装与运行
- `docs/README.md` 文档索引
- `docs/架构契约.md` 分层铁律，改代码前必读
- `AGENTS.md` 开发总览、目录导航与测试说明

## 二、行为准则

- 就事论事，尊重他人，不接受人身攻击与骚扰
- 技术分歧用证据说话，用最小可复现的例子讨论
- 不迎合也不空谈：说不清收益的改动，先开 issue 对齐

## 三、报告问题

先搜一遍 issue 看有没有重复。新建 issue 请给出

- 期望行为与实际行为
- 最小复现步骤
- 环境：操作系统、Python 版本、用的是哪个端（CLI / Web / Matrix）、provider 与模型
- 相关日志片段，错误日志在 `<coara_home>/logs/errors.jsonl`

不要贴 API key、令牌或任何凭据。

## 四、提交代码

1. Fork 仓库，从主分支切出话题分支：`fix/…`、`feat/…`、`docs/…`
2. 一个分支只做一件事，一个 PR 对应一个主题
3. 提交前自查：`ruff check .`、`ruff format .`、`pytest tests/ -q` 全绿
4. 行为有改动就补测试；修 bug 先写能复现的用例
5. 开 PR，写清动机、做法与验证方式

提交信息用祈使句，带范围前缀

```
fix(view) 回放帧按 view_seq 排序后再落定
feat(tools) 新增图像裁剪工具
docs(arch) 补输出路由一节
```

可用前缀：`feat` `fix` `docs` `test` `refactor` `perf` `chore` `style`。

## 五、开发环境

要求

- Python 3.11 或更高
- Node 18 或更高，只在构建 Web UI 时需要
- 可选 pandoc，只有生成 docx / pdf 时需要

装依赖

```
sh install.sh
```

或者手动

```
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"         # 加 ,office 可带 Office 文档支持
```

边调试边运行

```
python -m src.coara             # 源码直跑，改完重启进程
cd src/ui/web && npm run dev    # 前端热更新，5173 代理到内核 8080
```

测试

```
pytest tests/ -q                            # 默认档
pytest tests/ -m extended -q                # 边界与集成
pytest tests/ --override-ini="addopts=" -q  # 全量
```

## 六、代码规范

**分层**。依赖只能向下，铁律见 `docs/架构契约.md`：显示端到内核到执行到领域服务到基础原语，
支撑层与 `core` 不允许 import `coara` / `cli` / `agent` / `tools`。共享逻辑下沉为 core 原语，
上层只做薄适配。

**风格**。ruff 管到底：行宽 120，启用 `E F W I N UP B A C4 SIM`。每个模块以
`from __future__ import annotations` 开头，公开函数写全类型标注。数据结构优先 Pydantic
`BaseModel`，轻量内部状态容器用 `dataclass(slots=True)`。

**文案**。面向用户的文字中文优先，朴素、有信息量；不放内部 id 与调试字段；说明性 prose
句末不加句号。

**测试**。放在 `tests/` 对应子系统目录。异步用 `@pytest.mark.asyncio`，临时文件用 `tmp_path`，
打桩用 `monkeypatch`。长连接类资源（如 aiosqlite）必须显式关闭，否则进程退不出去。

**注释**。只在必要处写，解释为什么这么做，不解释代码在做什么。行内注释可用中文。

## 七、目录导航

| 目录 | 内容 |
|---|---|
| `src/coara/` | 内核：回合编排、会话、输出路由、端注册表 |
| `src/agent/` | 工具执行、循环、钩子 |
| `src/tools/` | 工具注册表与内置工具 |
| `src/llm/` | provider 抽象、流式、重试 |
| `src/ui/` | Web 服务、视图带、前端 SPA |
| `src/cli/` | 终端入口与交互 |
| `src/matrix_client/` | Matrix 客户端 |
| `src/skills/` `skills/` | 技能加载与内置技能包 |
| `src/runtime/` | 用量、工具输出、重启协议 |
| `docs/` | 架构与设计文档 |
| `tests/` | 测试 |

## 八、评审与合并

- PR 至少需要一条评审意见
- 架构级改动、公共接口改动、破坏性改动，先开 issue 讨论再动手
- 合并后删除话题分支

## 九、许可

本项目以 MIT 许可发布（见 `LICENSE`）。你的贡献同样以 MIT 许可提供。
