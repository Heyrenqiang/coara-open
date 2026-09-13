"""coara 开发者工具集 — 独立于发布版的 debug 程序集合。

当前包含 LLM log 查看器：直接读各工作空间 ``.coara/llm/llm-calls.jsonl``
磁盘镜像，不依赖运行中的 coara 进程，启动即可看各智能体最后一轮调用全文
（system prompt / 工具 Schema / 对话 / 响应 全部解锁）。

本包不进发布 wheel（pyproject ``packages.find`` 已 exclude）。
"""
