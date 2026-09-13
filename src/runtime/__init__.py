"""Runtime persistence helpers.

- ``tool_output_store`` — spill 落盘、batch budget、retention
- ``spill_policy`` — 分层阈值与 preview
- ``spill_read`` — spilled body 行分页读回
- ``usage_store`` / ``usage_collector`` / ``usage_query`` — token/工具用量事件聚合与查询
- ``usage_args`` — 用量记账的工具参数紧凑摘要（剔除大 payload）
"""
