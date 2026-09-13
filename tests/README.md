# tests/ — 测试布局

## 运行

```bash
pytest tests/ -q                  # 默认 core 套件（1603 例；排除 extended / e2e / real_env）
pytest tests/ -m extended -q      # extended 边界与集成（374 例）
pytest tests/ --override-ini="addopts=" -q   # 全量（1978 例；含 real_env）
pytest tests/test_coara/test_workflow_draft.py -v   # 单文件
```

数量经 `pytest --collect-only` 核实；默认排除规则写在 `pyproject.toml` 的 `addopts`（`-m 'not real_env and not e2e and not extended'`）。PowerShell 下 `-m ""` 易被吞掉参数，全量请用 `--override-ini="addopts="`。

## 标记

| 标记 | 含义 | 现状 |
|------|------|------|
| `extended` | 详细边界/集成测试，默认运行排除 | 由 `tests/conftest.py` 的 `_EXTENDED_TEST_PATHS`（26 个文件）在收集时自动打标，不用手写装饰器 |
| `real_env` | 依赖真实 provider / 网络 / 当前工作空间 | 目前仅 `test_llm/test_agnes_openai.py` 使用 |
| `e2e` | 跨多组件运行时流程 | 已在 `pyproject.toml` 注册，当前没有测试使用 |

三个标记都在 `pyproject.toml` 的 `[tool.pytest.ini_options] markers` 注册。

## conftest 与共享 fixture

| 文件 | 内容 |
|------|------|
| `tests/conftest.py` | `isolated_coara_home`（autouse：把 `COARA_HOME` 隔离到 `tmp_path`，`real_env` 测试跳过）、`_EXTENDED_TEST_PATHS` 自动打标、全局单例清理（provider registry / llm_service / skill_manager / config / todo store） |
| `tests/test_runtime/conftest.py` | spill 测试的 `ToolOutputStoreConfig` 工厂 |
| `tests/test_workflow/conftest.py` | `scheduler`（WorkflowScheduler + 临时库）与 `dummy_agent` fixture |
| `tests/helpers.py` | `FakeProvider` / `BlockingProvider` 罐装 LLM 响应、`make_test_coara(tmp_path, ...)` |
| `tests/real_env_helpers.py` | `managed_initialized_root` 异步上下文管理器，启动真实 `RootCoara` |
| `tests/workflow_wdl_fixtures.py` | WDL 样例 YAML 片段（主要被 `test_workflow/` 引用） |

## 目录

| 目录 | 内容 |
|------|------|
| `test_agent/` | 执行器与输出截断、tool policy、shell 失败连击（多为 extended） |
| `test_background/` | bash 后台任务执行器与任务存储 |
| `test_cli/` | CLI 入口、首跑向导、输入队列、远程同步显示、Matrix 连接、spinner |
| `test_coara/` | Root 运行时、delegate、scheduler、background 注入/提醒、turn streaming、环境注入、rules_glob、工作空间切换、workflow 草案 |
| `test_records_user/` | 用户收藏（CollectionStore 经 records 门面） |
| `test_context/` | 上下文窗口管理 |
| `test_core/` | 配置路径、错误日志、基础设施 |
| `test_event_sources/` | 事件源状态机 |
| `test_llm/` | Provider、call defaults、stream、message content、模型持久化、profile resolver、thinking 模式、缓存 |
| `test_matrix_client/` | 聊天命令、diff bridge、入站辅助、媒体入站、mention 路由、移动端同步、sync token、vault bridge |
| `test_records_agent/` | agent 记录存储、门禁、每日整理（curator） |
| `test_prompt/` | Agent registry、环境注入、YAML 加载器 |
| `test_records/` | records 门面与工具（facade + record/local_search 工具） |
| `test_reminders/` | 提醒服务 |
| `test_runtime/` | Spill 策略、tool output 存储、usage 存储 |
| `test_skills/` | Skill 策略与工具 |
| `test_todos/` | Todo 循环与可靠性 |
| `test_tools/` | 内置工具、文件安全、shell、sandbox、web、office 构建器、plan_mode、ws |
| `test_ui/` | Dashboard API、control plane、activity store、web upload、工作流审批/恢复、web 视图带（`test_view_seq_frames.py` 帧序号/快照/折叠区、`test_web_views.py` 序号单调/epoch/重置） |
| `test_vault/` | Vault 加解密 |
| `test_workflow/` | WDL parse/validate、scheduler（取消/并行/审批/恢复）、draft、DSL、node capabilities、crash recovery、delegate 池、QC、触发器 |
| `test_workspace/` | Updates store/ops/prune、registry vfs |

## 约定

- 新 **core** 测试：不要加进 `_EXTENDED_TEST_PATHS`，除非很慢或依赖完整工具链
- **优先合并**：同一契约用 1 个用例覆盖 happy / 跳过 / 关闭；边界用 `@parametrize`，避免「每个分支一个文件函数」
- **不要测实现琐事**：cancel task、纯 setter、仅换字符串的同类断言应并进主路径
- Mock 掉重活（git seed、整链 `initialize`）除非测试目标就是那条链路
- **real_env**：真实 provider / 网络，默认跳过；隔离 fixture 对它不生效
- 异步测试：`@pytest.mark.asyncio`（`asyncio_mode = auto`，fixture loop 作用域为 function）
- 文件系统操作用 `tmp_path`；mock 用 `monkeypatch` / `AsyncMock` / `SimpleNamespace`
- 实例化真实 LLM provider 的测试要关闭它们（或 mock client），避免 `ResourceWarning`
- 涉及 `WorkflowEngine` / `mp.Queue` 的测试必须显式关停引擎实例并 `close()` 队列（`mp.Queue` 不 close 会挂住解释器退出，曾致后台测试任务 45 秒跑完挂 10 分钟）
- 后台跑测试命令必带 `--timeout`，避免挂死的套件让后台任务永远 running
