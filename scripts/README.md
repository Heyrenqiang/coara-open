# scripts/ — 仓库运维脚本

与 `skills/*/scripts/`、`android-app/scripts/` **分开**。均在**仓库根目录**执行。

```text
scripts/
├── dev/            排障 · coara_home 清理 · prompt 标点维护
├── examples/       示例项目安装
├── soft-copyright/ 软著材料（离线）
└── tunnels/        Windows webhook 公网隧道
```

## 常用

| 做什么 | 命令 |
|--------|------|
| 轮次耗时（trace） | `python scripts/dev/analyze_turn_timing.py` |
| Token / 工具用量 | 聊天 `/usage` 或 `coara usage summary` |
| LLM 延迟对比 | `python scripts/dev/benchmark_llm_latency.py` |
| 规范化 prompt 标点 | `python scripts/dev/normalize_prompt_punctuation.py` |
| 清理临时工作空间 | `python scripts/dev/prune_coara_workspaces.py --coara-home D:/coara --dry-run` |
| 安装示例项目 | `coara examples install stocks-watch` |
| Webhook 隧道 | `powershell -File scripts/tunnels/Start-AllTunnels.ps1` |

参数细节 → 各子目录 README 或 `python scripts/dev/<脚本>.py --help`

## 子目录

| 目录 | 文档 |
|------|------|
| [`dev/`](dev/) | [`dev/README.md`](dev/README.md) |
| [`examples/`](examples/) | [`examples/README.md`](examples/README.md) |
| [`soft-copyright/`](soft-copyright/) | [`soft-copyright/README.md`](soft-copyright/README.md) |
