"""Regression tests for defects found in the hostile self-review."""

from __future__ import annotations

import asyncio
import io
import logging
import socket

import pytest

from aiochainway import (
    ConnectionLostError,
    FrequencyRegion,
    ReaderTimeout,
    TagRead,
    UR4Client,
    UR4UdpListener,
    ValidationError,
    WorkMode,
    provision,
)
from aiochainway.cli import async_main
from aiochainway.protocol import commands as c
from aiochainway.protocol.frame import Frame, encode

from .conftest import eventually
from .fake_reader import FakeReader, tag_record


async def test_cancelled_request_late_reply_goes_nowhere(
    client: UR4Client, fake: FakeReader
) -> None:
    fake.faults.delay = 0.3
    task = asyncio.create_task(client.get_version())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.faults.delay = 0
    fake.state.version = (2, 2, 2)
    # Without the stale record, the first request's V1.2.3 would be returned.
    assert await client.get_version() == "V2.2.2"


async def test_slow_late_reply_after_timeout_goes_nowhere(fake: FakeReader) -> None:
    client = UR4Client("127.0.0.1", fake.port, timeout=0.3)
    await client.connect()
    fake.faults.delay = 0.5  # later than min(timeout, 1.0) used to be safe for
    with pytest.raises(ReaderTimeout):
        await client.get_version()
    fake.faults.delay = 0
    fake.state.version = (2, 2, 2)
    assert await client.get_version() == "V2.2.2"
    await client.close()


async def test_stale_reply_that_never_comes_only_delays(fake: FakeReader) -> None:
    client = UR4Client("127.0.0.1", fake.port, timeout=0.2, stale_window=0.3)
    await client.connect()
    fake.faults.drop_next = 1
    with pytest.raises(ReaderTimeout):
        await client.get_version()
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await client.get_version() == "V1.2.3"
    assert 0.25 < loop.time() - start < 1.0
    await client.close()


def test_set_ack_never_matches_get_reply() -> None:
    sett = c.build_set_beep(True)
    assert sett.matches(Frame(0xA2, b"\x01"))
    assert sett.matches(Frame(0xA2, b"\x00"))
    for sub in c.CONFIG_GET_SUBS:
        assert not sett.matches(Frame(0xA2, bytes((sub, 1))))
    assert c.reply_key(Frame(0xA2, b"\x06\x01")) == (0xA2, 0x06)
    assert c.reply_key(Frame(0xA2, b"\x01")) == (0xA2, None)
    assert c.reply_key(Frame(0x03, b"\x06")) == (0x03, None)


async def test_close_during_inventory_sends_stop(
    client: UR4Client, fake: FakeReader
) -> None:
    stream = client.inventory()
    await stream.__anext__()
    await client.close()
    await fake.wait_for("stop_inventory")
    with pytest.raises(ConnectionLostError):
        await stream.__anext__()


async def test_stop_inventory_connection_lost_is_not_cancellation(
    fake: FakeReader,
) -> None:
    client = UR4Client("127.0.0.1", fake.port, timeout=0.5, stop_drain=2.0)
    await client.connect()
    fake.faults.no_stop_ack = True

    async def drop() -> None:
        await fake.wait_for("stop_inventory")
        await fake.drop_connections()

    dropper = asyncio.create_task(drop())
    with pytest.raises(ConnectionLostError):
        await client.stop_inventory()
    await dropper
    await client.close()


async def test_stop_inventory_still_cancellable(
    client: UR4Client, fake: FakeReader
) -> None:
    fake.faults.no_stop_ack = True
    client.stop_drain = 5
    task = asyncio.create_task(client.stop_inventory())
    await fake.wait_for("stop_inventory")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_provision_accepts_int_codes_and_rejects_bad_ones() -> None:
    result = await provision("h", region=0x08, work_mode=1, dry_run=True)  # type: ignore[arg-type]  # ints on purpose
    assert [s.description for s in result.steps][1:] == ["Region USA", "Work mode AUTO"]
    with pytest.raises(ValidationError):
        await provision("h", region=0x55, dry_run=True)  # type: ignore[arg-type]  # bad code on purpose
    with pytest.raises(ValidationError):
        await provision("h", work_mode=9, dry_run=True)  # type: ignore[arg-type]  # bad mode on purpose
    assert FrequencyRegion(8) is FrequencyRegion.USA and WorkMode(1) is WorkMode.AUTO


async def _cli(*args: str) -> int:
    return await async_main(list(args), stdout=io.StringIO())


async def test_cli_bad_host_and_ports_are_usage_errors() -> None:
    assert await _cli("--host", "a..b", "--timeout", "0.3", "version") == 2
    assert await _cli("--port", "70000", "version") == 2
    assert await _cli("listen", "--port", "70000") == 2
    assert await _cli("--port", "0", "version") == 2
    assert await _cli("provision", "--new-port", "x", "--dry-run") == 2


async def test_cli_guard_refuses_before_connecting() -> None:
    base = ["--host", "127.0.0.1", "--port", "1", "--timeout", "0.3"]
    assert await _cli(*base, "factory-reset") == 5
    assert (
        await _cli(
            *base, "network", "--set", "--ip", "10.0.0.5", "--gateway", "10.0.0.1"
        )
        == 5
    )
    assert await _cli(*base, "destination", "--set", "--ip", "10.0.0.5") == 5
    # With --confirm the guard passes and the unreachable host is reported.
    assert await _cli(*base, "factory-reset", "--confirm") == 3


async def test_client_rejects_invalid_host() -> None:
    client = UR4Client("a..b", 8888, timeout=0.3)
    with pytest.raises(ValidationError):
        await client.connect()


async def test_listener_callback_errors_are_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def bad(tag: TagRead) -> None:
        raise RuntimeError("boom")

    listener = await UR4UdpListener.start(("127.0.0.1", 0), callback=bad)
    assert listener.local_address is not None
    port = listener.local_address[1]
    with (
        caplog.at_level(logging.DEBUG, logger="aiochainway"),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s,
    ):
        for _ in range(5):
            s.sendto(encode(0x83, tag_record("AABB")), ("127.0.0.1", port))
        async with asyncio.timeout(2):
            while listener.stats.callback_errors < 5:
                await asyncio.sleep(0.005)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "1 callback error(s) so far" in errors[0].getMessage()
    await listener.stop()


async def test_nan_duration_rejected(client: UR4Client) -> None:
    with pytest.raises(ValidationError):
        client.inventory(duration=float("nan"))


async def test_listener_bounds_running_async_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiochainway.listener as lst

    monkeypatch.setattr(lst, "MAX_PENDING_CALLBACKS", 3)
    gate = asyncio.Event()

    async def slow(tag: TagRead) -> None:
        await gate.wait()

    listener = UR4UdpListener(callback=slow)
    raw = encode(0x83, tag_record("AABB"))
    for _ in range(5):
        listener._on_datagram(raw, ("127.0.0.1", 9))
    assert listener.stats.callbacks_dropped == 2
    assert len(listener._tasks) == 3
    gate.set()
    await eventually(lambda: not listener._tasks)
    await listener.stop()
