"""Shared test helpers reused across multiple test modules."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.coara.base import CoaraBase
from src.core.types import CoaraPersona
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(name="fake", api_key="test", default_model="fake-model")
        self._responses = responses

    async def complete(self, *args, **kwargs) -> LLMResponse:
        return self._responses.pop(0)

    async def stream_complete(self, *args, **kwargs):
        yield StreamChunk(delta_content="")

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


class BlockingProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.started = asyncio.Event()

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


def make_test_coara(
    tmp_path: Path,
    *,
    name: str = "TestCoara",
    provider: LLMProvider | None = None,
    audit_session_id: str | None = None,
    user_facing: bool = True,
) -> CoaraBase:
    return CoaraBase(
        name=name,
        persona=CoaraPersona(name=name, role="tester"),
        workspace_dir=tmp_path,
        provider=provider or FakeProvider([]),
        audit_session_id=audit_session_id,
        user_facing=user_facing,
    )
