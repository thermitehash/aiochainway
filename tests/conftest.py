"""Shared fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import gc
import os
import sys

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


#: A test still running after this many seconds is reported and cancelled.
HANG_TIMEOUT = float(os.environ.get("TEST_HANG_TIMEOUT", "60"))


@pytest.fixture(autouse=True)
async def hang_watchdog(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Turn a hung async test into a failure that shows where it is stuck.

    After ``HANG_TIMEOUT`` seconds, print the stack of every asyncio task, then
    cancel them all so the test fails instead of hanging the CI job.
    """
    loop = asyncio.get_running_loop()

    def dump() -> None:
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        print(
            f"\n=== {request.node.nodeid} still running after {HANG_TIMEOUT:g}s;"
            f" {len(tasks)} asyncio task(s):",
            file=sys.stderr,
        )
        for task in tasks:
            print(f"--- {task!r}", file=sys.stderr)
            task.print_stack(file=sys.stderr)
        for task in tasks:
            task.cancel()

    handle = loop.call_later(HANG_TIMEOUT, dump)
    yield
    handle.cancel()


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
