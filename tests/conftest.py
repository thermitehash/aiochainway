"""Shared fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import gc
import os

from hypothesis import HealthCheck, settings
import pytest

from aiochainway import UR4Client
from aiochainway.testing import FakeUR4

settings.register_profile("dev", max_examples=200, deadline=None)
settings.register_profile(
    "ci",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    print_blob=True,
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
async def no_leaked_tasks() -> AsyncIterator[None]:
    """Fail a test that leaves background tasks running."""
    yield
    await asyncio.sleep(0)
    current = asyncio.current_task()
    leaked = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in leaked:
        task.cancel()
    if leaked:
        await asyncio.gather(*leaked, return_exceptions=True)
    gc.collect()
    assert not leaked, f"leaked tasks: {leaked}"


@pytest.fixture
async def fake() -> AsyncIterator[FakeUR4]:
    reader = FakeUR4()
    await reader.start()
    yield reader
    await reader.stop()


@pytest.fixture
async def client(fake: FakeUR4) -> AsyncIterator[UR4Client]:
    c = UR4Client("127.0.0.1", fake.port, timeout=0.5, stop_drain=0.2)
    await c.connect()
    yield c
    await c.close()


async def eventually(condition: Callable[[], object], timeout: float = 3.0) -> None:
    """Wait until ``condition()`` is truthy; fail after ``timeout`` seconds.

    Use instead of fixed sleeps when waiting for an asynchronous effect, which
    can take much longer on a busy CI runner (macOS especially).
    """
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)
