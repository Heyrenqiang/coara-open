# 参与开发

欢迎提交 issue 与 pull request。

## 起步

1. 克隆仓库，`pip install -e ".[dev,office]"`
2. 跑 `pytest tests/ -q` 确认基线全绿
3. 改代码，保持 `ruff check .` 与 `ruff format .` 干净
4. 提交前跑一遍默认测试

## 约定

- 架构分层见 `docs/架构契约.md`，依赖只能向下
- 代码风格与命名见 `AGENTS.md`，行宽 120，模块以 `from __future__ import annotations` 开头
- 面向用户的文案中文优先、朴素、有信息量
- 新增或改动行为请补测试，测试放 `tests/` 对应子系统目录

## 提交信息

用简短的祈使句，能加范围前缀就加，例如 `fix(view) ...`、`feat(tools) ...`。
