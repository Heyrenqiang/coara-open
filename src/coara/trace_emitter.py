"""Trace emission bridge between Coara runtime and observability sinks.

Decouples trace formatting and emission from CoaraBase core logic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.core.events import TraceEvent
from src.core.logger import logger
from src.utils.message_content import message_content_to_text

TraceSink = Callable[[TraceEvent], None]


class TraceEmitter:
    """Emits trace events to a registered sink with enriched metadata."""

    def __init__(
        self,
        sink: TraceSink | None,
        coara_id: str,
        coara_name: str,
        session_id: str,
        *,
        origin_scope: str = "main_loop",
        workspace_dir: str = "",
    ):
        self.sink = sink
        self.coara_id = coara_id
        self.coara_name = coara_name
        self.session_id = session_id
        self.origin_scope = origin_scope
        self.workspace_dir = workspace_dir

    def emit(
        self,
        event_type: str,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        level: str = "info",
        origin_scope: str | None = None,
    ) -> None:
        if self.sink is None:
            return

        enriched_payload: dict[str, Any] = {
            "session_id": self.session_id,
            "coara_id": self.coara_id,
            "coara_name": self.coara_name,
            "origin_scope": origin_scope or self.origin_scope,
        }
        if self.workspace_dir:
            enriched_payload["workspace_dir"] = self.workspace_dir
        if payload:
            enriched_payload.update(payload)
            # Caller payload must not strip the binding workspace for routing.
            if self.workspace_dir and not str(enriched_payload.get("workspace_dir") or "").strip():
                enriched_payload["workspace_dir"] = self.workspace_dir

        self.sink(
            TraceEvent(
                coara_id=self.coara_id,
                coara_name=self.coara_name,
                event_type=event_type,
                message=message,
                level=level,
                payload=enriched_payload,
            )
        )

        # Bridge key trace events to the text logger for easier CLI debugging.
        self._bridge_to_logger(event_type, message, payload or {})

    def _bridge_to_logger(self, event_type: str, message: str, payload: dict[str, Any]) -> None:
        try:
            log_prefix = f"[{self.coara_name}]"

            if event_type == "llm_turn_complete":
                lo = payload.get("llm_output", {})
                content = lo.get("content") or ""
                tc = lo.get("tool_calls") or []
                finish_reason = lo.get("finish_reason") or "unknown"
                usage = lo.get("usage") or {}
                tc_summary = ", ".join(t.get("name", "") for t in tc) if tc else "none"
                content_preview = content[:200].replace("\n", " ") + ("..." if len(content) > 200 else "")
                usage_summary = ", ".join(
                    f"{key}={value}"
                    for key, value in (
                        ("input_tokens", usage.get("input_tokens")),
                        ("output_tokens", usage.get("output_tokens")),
                        ("total_tokens", usage.get("total_tokens")),
                    )
                    if value is not None
                )
                suffix = f" | Usage: {usage_summary}" if usage_summary else ""
                logger.debug(
                    f"{log_prefix} LLM Turn Complete | Finish: {finish_reason} | "
                    f"Tools: {tc_summary} | Content: {content_preview}{suffix}"
                )
                if finish_reason == "max_tokens":
                    logger.warning(
                        f"{log_prefix} Model output hit max_tokens limit | "
                        f"output_tokens={usage.get('output_tokens')} | tools={tc_summary}"
                    )

            elif event_type == "tool_call":
                tool_name = payload.get("tool_name", "")
                is_err = payload.get("is_error", False)
                out_raw = str(payload.get("tool_output", ""))
                out_preview = out_raw[:300].replace("\n", " ") + ("..." if len(out_raw) > 300 else "")
                status = "ERROR" if is_err else "OK"
                logger.debug(f"{log_prefix} Tool [{tool_name}] -> {status} | Output: {out_preview}")

            elif event_type == "final_response":
                content = payload.get("content", "")
                content_preview = content[:300].replace("\n", " ") + ("..." if len(content) > 300 else "")
                logger.debug(f"{log_prefix} Final Response: {content_preview}")

            elif event_type in ("llm_error", "context_blocked", "context_warning", "content_policy_recovery"):
                # LLM 已分类用户错误：不进用户 console（debug 才见）；详情仍在 trace 事件与文件
                logger.debug(f"{log_prefix} {event_type.upper()}: {message}")

            elif event_type == "output_truncation_recovery":
                notice = payload.get("user_notice") or message
                logger.warning(f"{log_prefix} {notice}")

        except Exception as exc:
            # Failsafe so logging doesn't crash the trace emission
            logger.debug(f"trace console logging failed: {exc}")


def serialize_messages_for_trace(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize messages for trace/dashboard consumption."""
    serialized: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {
            "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
            "content": message_content_to_text(msg.content),
            "tool_call_id": msg.tool_call_id,
        }
        if msg.tool_calls:
            entry["tool_calls"] = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls]
        serialized.append(entry)
    return serialized
