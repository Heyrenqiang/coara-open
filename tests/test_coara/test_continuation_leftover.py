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
