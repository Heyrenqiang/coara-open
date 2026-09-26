"""接续 leftover 按 item.source 分派，不绑收尾端。"""

from __future__ import annotations

import pytest

from src.coara.continuation_leftover import (
    dispatch_leftover_item,
    is_cli_source,
    is_matrix_source,
    is_web_source,
    leftover_item_source,
)
from src.core.types import ContinuationInput


def test_leftover_item_source_prefers_item() -> None:
    item = ContinuationInput(text="hi", source="matrix")
    assert leftover_item_source(item, fallback="web") == "matrix"
    assert leftover_item_source(ContinuationInput(text="x"), fallback="web") == "web"
    assert leftover_item_source("bare", fallback="cli-attached") == "cli-attached"


def test_source_predicates() -> None:
    assert is_web_source("web")
    assert is_web_source("web-flow")
    assert not is_web_source("matrix")
    assert is_matrix_source("matrix")
    assert is_cli_source("cli-attached")
    assert is_cli_source("cli")


def test_dispatch_predicates_intentional_divergence() -> None:
    """分派门与族判定的有意差异钉成测试（防漂移，非行为变更）：

    - is_cli_source 不认 cli- 前缀：cli-* leftover 落「跟收尾端走」兜底分支
    - is_matrix_source 不含 event：event leftover 不能开成 matrix 回合
    - 空来源恒 False（分派门无 unknown 放行档）
    """
    assert not is_cli_source("cli-x")
    assert not is_matrix_source("event")
    assert not is_matrix_source("matrix-x")
    assert not is_web_source("")
    assert not is_matrix_source("")
    assert not is_cli_source("")


@pytest.mark.asyncio
async def test_dispatch_calls_native_by_source() -> None:
    seen: list[tuple[str, str]] = []

    async def run_web(text: str, images: list | None) -> None:
        seen.append(("web", text))

    async def run_matrix(text: str, images: list | None) -> None:
        seen.append(("matrix", text))

    root = object()
    coara = object()
    await dispatch_leftover_item(
        root,
        coara,
        ContinuationInput(text="phone", source="matrix"),
        finishing_source="web",
        run_web_turn=run_web,
        run_matrix_turn=run_matrix,
    )
    await dispatch_leftover_item(
        root,
        coara,
        ContinuationInput(text="browser", source="web"),
        finishing_source="matrix",
        run_web_turn=run_web,
        run_matrix_turn=run_matrix,
    )
    assert seen == [("matrix", "phone"), ("web", "browser")]
