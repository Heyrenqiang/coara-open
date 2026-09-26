"""Shared kernel projection fixtures for workflow tests.

2026-08-16 内核化：测试用工作流文本一律是内核投影（nodes+edges）。
"""

from __future__ import annotations

from textwrap import dedent


def minimal_two_step_wdl(name: str = "demo", description: str = "desc") -> str:
    return dedent(
        f"""
        name: {name}
        description: {description}
        schedule:
          kind: manual
        inputs:
          goal:
            type: string
            default: x
        nodes:
          a:
            task: step a
            input: "{{{{inputs.goal}}}}"
          b:
            task: step b
            input: "{{{{steps.a.text}}}}"
        edges:
          - {{from: a, to: b}}
        """
    ).strip()


def two_run_wdl(name: str, node_ids: list[str], *, description: str = "test") -> str:
    """Build minimal projection with N nodes chained by edges."""
    lines = [
        f"name: {name}",
        f"description: {description}",
        "nodes:",
    ]
    for nid in node_ids:
        lines.append(f"  {nid}:")
        lines.append("    task: test")
    if len(node_ids) >= 2:
        lines.append("edges:")
        for idx in range(len(node_ids) - 1):
            lines.append(f"  - {{from: {node_ids[idx]}, to: {node_ids[idx + 1]}}}")
    return "\n".join(lines)


def retry_with_error_edge_wdl(*, fail_times: int = 2) -> str:
    """Projection with an error edge for failure routing."""
    return dedent(
        f"""
        name: retry-demo
        description: retry with error edge
        inputs:
          fail_times:
            type: integer
            default: {fail_times}
        nodes:
          fetch:
            task: fetch
            input: "{{{{inputs.fail_times}}}}"
          handle_fail:
            task: handle failure
          after:
            task: after
        edges:
          - {{from: fetch, to: after}}
          - {{from: fetch, to: handle_fail, on: error}}
          - {{from: handle_fail, to: after}}
        """
    ).strip()


def error_edge_fallback_wdl() -> str:
    """Projection where a failing node has an error fallback route."""
    return dedent(
        """
        name: error-fallback
        nodes:
          risky:
            task: risky
          fallback:
            task: fallback
          tail:
            task: tail
        edges:
          - {from: risky, to: tail}
          - {from: risky, to: fallback, on: error}
          - {from: fallback, to: tail}
        """
    ).strip()


def parallel_branches_wdl(
    name: str,
    parallel_id: str,
    branch_step_ids: list[str],
    *,
    description: str = "parallel test",
) -> str:
    """Projection with a fan-out node (parallel = 扇出拓扑)."""
    lines = [
        f"name: {name}",
        f"description: {description}",
        "nodes:",
        f"  {parallel_id}:",
        "    task: fan out",
    ]
    for nid in branch_step_ids:
        lines.append(f"  {nid}:")
        lines.append("    task: test")
    lines.append("edges:")
    for nid in branch_step_ids:
        lines.append(f"  - {{from: {parallel_id}, to: {nid}}}")
    return "\n".join(lines)


def retry_exhausted_no_error_edge_wdl() -> str:
    """Projection where a failing node has no error edge -> instance FAILED."""
    return dedent(
        """
        name: retry-fail
        nodes:
          fetch:
            task: always fail
          after:
            task: after
        edges:
          - {from: fetch, to: after}
        """
    ).strip()
