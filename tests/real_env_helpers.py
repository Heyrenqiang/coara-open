"""Shared helpers for real-environment test setup and teardown."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from src.coara.root import RootCoara
from src.core.config import config_manager
from src.llm.registry import initialize_providers, provider_registry
from tests.helpers import FakeProvider

_TEST_ROOT_PROVIDER = "test-root-env"


async def load_real_environment() -> None:
    """Load runtime configuration and initialize providers once per test flow."""
    provider_registry.register(_TEST_ROOT_PROVIDER, FakeProvider([]))
    await config_manager.load()
    await initialize_providers(config_manager)


@asynccontextmanager
async def managed_initialized_root(workspace_dir: Path) -> AsyncIterator[RootCoara]:
    """Create an initialized root with guaranteed shutdown."""
    await load_real_environment()
    root = RootCoara(workspace_dir=workspace_dir, provider_name=_TEST_ROOT_PROVIDER)
    await root.initialize()
    try:
        yield root
    finally:
        await root.shutdown()
