"""Per-turn wall-clock timing for latency breakdown."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class IterationTiming:
    iteration: int
    context_prep_ms: float = 0.0
    compression_ms: float = 0.0
    llm_ms: float = 0.0
    tools_ms: float = 0.0
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TurnTimingRecorder:
    """Accumulates stage durations for one user turn (perf_counter based)."""

    turn_id: str
    _started_at: float = field(default_factory=time.perf_counter, repr=False)
    _marks: dict[str, float] = field(default_factory=dict, repr=False)
    finished: bool = False
    input_prep_ms: float = 0.0
    iterations: list[IterationTiming] = field(default_factory=list)
    _current: IterationTiming | None = field(default=None, repr=False)
    _iter_mark: str | None = field(default=None, repr=False)

    def mark(self, name: str) -> None:
        self._marks[name] = time.perf_counter()

    def lap_ms(self, name: str, *, since: str) -> float:
        end = time.perf_counter()
        start = self._marks.get(since, self._started_at)
        delta_ms = max(0.0, (end - start) * 1000)
        self._marks[since] = end
        return delta_ms

    def record_input_prep(self) -> None:
        self.input_prep_ms = self.lap_ms("input_prep", since="turn_start")

    def begin_iteration(self, iteration: int) -> None:
        self._current = IterationTiming(iteration=iteration)
        self._iter_mark = f"iter{iteration}"
        self.mark(self._iter_mark)

    def record_context_prep(self) -> None:
        if self._current is None or self._iter_mark is None:
            return
        self._current.context_prep_ms = self.lap_ms(
            f"{self._iter_mark}_context",
            since=self._iter_mark,
        )

    def record_compression(self) -> None:
        if self._current is None or self._iter_mark is None:
            return
        mark = f"{self._iter_mark}_compression"
        self.mark(mark)

    def finish_compression(self) -> None:
        if self._current is None or self._iter_mark is None:
            return
        mark = f"{self._iter_mark}_compression"
        if mark not in self._marks:
            return
        self._current.compression_ms = self.lap_ms(
            f"{self._iter_mark}_compression_done",
            since=mark,
        )

    def begin_llm(self) -> None:
        if self._iter_mark is None:
            return
        self.mark(f"{self._iter_mark}_llm")

    def record_llm(self) -> None:
        if self._current is None or self._iter_mark is None:
            return
        self._current.llm_ms = self.lap_ms(
            f"{self._iter_mark}_llm_done",
            since=f"{self._iter_mark}_llm",
        )

    def begin_tools(self) -> None:
        if self._iter_mark is None:
            return
        self.mark(f"{self._iter_mark}_tools")

    def record_tools(self, executions: list[Any]) -> None:
        if self._current is None or self._iter_mark is None:
            return
        self._current.tools_ms = self.lap_ms(
            f"{self._iter_mark}_tools_done",
            since=f"{self._iter_mark}_tools",
        )
        for execution in executions:
            tool_call = getattr(execution, "tool_call", None)
            result = getattr(execution, "result", None)
            name = getattr(tool_call, "name", "") if tool_call else ""
            duration_ms = None
            if result is not None and getattr(result, "metadata", None):
                duration_ms = (result.metadata or {}).get("duration_ms")
            self._current.tools.append(
                {
                    "name": name,
                    "duration_ms": duration_ms,
                    "is_error": bool(getattr(result, "is_error", False)),
                }
            )
        self._finish_current_iteration()

    def finish_iteration(self) -> None:
        """Close the current iteration when the turn ends without a tool phase."""
        self._finish_current_iteration()

    def _finish_current_iteration(self) -> None:
        if self._current is None:
            return
        self.iterations.append(self._current)
        self._current = None
        self._iter_mark = None

    def total_ms(self) -> float:
        return max(0.0, (time.perf_counter() - self._started_at) * 1000)

    def attributed_ms(self) -> float:
        total = self.input_prep_ms
        for item in self.iterations:
            total += item.context_prep_ms + item.compression_ms + item.llm_ms + item.tools_ms
        return total

    def to_payload(self) -> dict[str, Any]:
        if self._current is not None:
            self._finish_current_iteration()
        total_ms = self.total_ms()
        attributed_ms = self.attributed_ms()
        overhead_ms = max(0.0, total_ms - attributed_ms)
        return {
            "turn_id": self.turn_id,
            "total_ms": round(total_ms, 2),
            "input_prep_ms": round(self.input_prep_ms, 2),
            "attributed_ms": round(attributed_ms, 2),
            "overhead_ms": round(overhead_ms, 2),
            "iterations": [
                {
                    "iteration": item.iteration,
                    "context_prep_ms": round(item.context_prep_ms, 2),
                    "compression_ms": round(item.compression_ms, 2),
                    "llm_ms": round(item.llm_ms, 2),
                    "tools_ms": round(item.tools_ms, 2),
                    "tools": item.tools,
                }
                for item in self.iterations
            ],
        }
