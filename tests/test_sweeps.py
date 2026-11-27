"""v0.3.0: door-driven sweeps: lifecycle, SweepResult, keepalive, reconnect."""

from __future__ import annotations

import asyncio
import gc
import logging
import socket

import pytest

from aiochainway import (
    AntennaState,
    ConnectionLostError,
    InventoryStream,
    ReaderTimeout,
    SweepEndReason,
    SweepResult,
    UnsafeOperationError,
    UR4Client,
    ValidationError,
)
from aiochainway.testing import FakeUR4, ScriptedTag, tag_record

from .conftest import eventually


async def client_for(fake: FakeUR4, **kwargs: object) -> UR4Client:
    opts: dict[str, object] = {"timeout": 0.5, "stop_drain": 0.3} | kwargs
    client = UR4Client(fake.host, fake.port, **opts)  # type: ignore[arg-type]  # test passthrough
    await client.connect()
    return client


def is_running(sweep: InventoryStream) -> bool:
    return sweep.running  # via a call: mypy would narrow the property


def result_of(sweep: InventoryStream) -> SweepResult | None:
    return sweep.result


# Lifecycle and SweepResult --------------------------------------------------


async def test_completed_sweep_result(client: UR4Client, fake: FakeUR4) -> None:
    fake.tags = [tag_record("AA01", antenna=1), tag_record("AA02", antenna=3)]
    async with client.inventory(duration=0.2) as sweep:
        assert sweep.result is None and is_running(sweep)
        seen = [tag.epc async for tag in sweep]
    result = result_of(sweep)
    assert result is not None and not is_running(sweep)
    assert result.end_reason is SweepEndReason.DURATION_ELAPSED and result.completed
    assert result.unique_epcs == {"AA01", "AA02"} and result.unique_count == 2
    assert result.reports == len(seen) >= 4
    assert result.per_antenna_counts.keys() == {1, 3}
    assert sum(result.per_antenna_counts.values()) == result.reports
    assert result.stop_sent and result.stop_acknowledged
    assert result.requested_duration == 0.2
    assert result.elapsed is not None and 0.15 < result.elapsed < 1.0
    assert result.started_at is not None and result.ended_at >= result.started_at
    assert fake.names()[-1] == "stop_inventory"


async def test_exit_block_early_is_stopped(client: UR4Client, fake: FakeUR4) -> None:
    async with client.inventory(duration=30) as sweep:
        async for _ in sweep:
            break
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.STOPPED
    assert not sweep.result.completed and sweep.result.stop_acknowledged
    await fake.wait_for("stop_inventory")


async def test_exit_after_deadline_without_iterating_is_stopped(
    client: UR4Client,
) -> None:
    # Nothing was iterated, so a consumer has seen no tags: not complete.
    async with client.inventory(duration=0.05) as sweep:
        await asyncio.sleep(0.1)
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.STOPPED
    assert not sweep.result.completed
    assert sweep.result.reports > 0  # the reader did report; counted anyway


async def test_no_duration_exit_is_stopped(client: UR4Client) -> None:
    async with client.inventory() as sweep:
        await anext(sweep)
    assert (
        sweep.result is not None and sweep.result.end_reason is SweepEndReason.STOPPED
    )


async def test_error_in_body(client: UR4Client, fake: FakeUR4) -> None:
    with pytest.raises(KeyError):
        async with client.inventory(duration=30) as sweep:
            await anext(sweep)
            raise KeyError("boom")
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.ERROR
    assert sweep.result.error == "KeyError('boom')"
    await fake.wait_for("stop_inventory")


async def test_cancelled_sweep_is_stopped(client: UR4Client, fake: FakeUR4) -> None:
    holder: list[SweepResult | None] = []

    async def run() -> None:
        sweep = client.inventory(duration=30)
        try:
            async with sweep:
                async for _ in sweep:
                    pass
        finally:
            holder.append(sweep.result)

    task = asyncio.create_task(run())
    await fake.wait_for("start_inventory")
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert holder[0] is not None and holder[0].end_reason is SweepEndReason.STOPPED
    await fake.wait_for("stop_inventory")


async def test_connection_drop_mid_sweep(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.disconnect_after_s = 0.1
    with pytest.raises(ConnectionLostError):
        async with client.inventory(duration=30) as sweep:
            async for _ in sweep:
                pass
    result = sweep.result
    assert result is not None
    assert result.end_reason is SweepEndReason.CONNECTION_LOST and not result.completed
    assert result.reports > 0 and not result.stop_sent and not result.stop_acknowledged


async def test_connection_drop_while_not_iterating(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.disconnect_after_s = 0.05
    async with client.inventory(duration=0.3) as sweep:
        await eventually(lambda: not client.connected)
        await asyncio.sleep(0.3)  # deadline passes too; the drop still wins
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.CONNECTION_LOST


async def test_client_close_mid_sweep(client: UR4Client, fake: FakeUR4) -> None:
    sweep = client.inventory(duration=30)
    async with sweep:
        await anext(sweep)
        await client.close()
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.CONNECTION_LOST
    assert sweep.result.stop_sent
    await fake.wait_for("stop_inventory")


async def test_refused_second_sweep_has_error_result(client: UR4Client) -> None:
    async with client.inventory() as first:
        second = client.inventory()
        with pytest.raises(UnsafeOperationError):
            async with second:
                pass
        assert second.result is not None
        assert second.result.end_reason is SweepEndReason.ERROR
        assert second.result.started_at is None and second.result.elapsed is None
        assert not second.result.stop_sent
        assert first.running


async def test_start_on_disconnected_client(fake: FakeUR4) -> None:
    client = UR4Client(fake.host, fake.port, timeout=0.3)
    sweep = client.inventory(duration=1)
    with pytest.raises(ConnectionLostError):
        async with sweep:
            pass
    assert sweep.result is not None and sweep.result.end_reason is SweepEndReason.ERROR


async def test_stop_ack_delay_within_and_beyond_drain(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.stop_ack_delay = 0.1
    async with client.inventory(duration=0.05) as sweep:
        async for _ in sweep:
            pass
    assert sweep.result is not None and sweep.result.stop_acknowledged
    fake.faults.stop_ack_delay = 1.0  # longer than stop_drain=0.2
    loop = asyncio.get_running_loop()
    start = loop.time()
    async with client.inventory(duration=0.05) as sweep:
        async for _ in sweep:
            pass
    assert loop.time() - start < 0.05 + 0.2 + 0.3  # bounded by stop_drain
    assert sweep.result is not None
    assert sweep.result.stop_sent and not sweep.result.stop_acknowledged
    assert sweep.result.completed  # a missing ack doesn't abort the sweep


async def test_missing_stop_ack(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.no_stop_ack = True
    async with client.inventory(duration=0.05) as sweep:
        async for _ in sweep:
            pass
    assert sweep.result is not None and not sweep.result.stop_acknowledged


async def test_abandoned_stream_logs_warning(
    client: UR4Client, fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aiochainway"):
        async for _ in client.inventory():
            break
        gc.collect()
    await fake.wait_for("stop_inventory")
    assert "abandoned without being closed" in caplog.text


async def test_async_with_does_not_warn(
    client: UR4Client, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="aiochainway"):
        async with client.inventory() as sweep:
            async for _ in sweep:
                break
        del sweep
        gc.collect()
    assert "abandoned" not in caplog.text


async def test_dropped_reports_still_counted(fake: FakeUR4) -> None:
    client = await client_for(fake, tag_queue_size=2)
    fake.tag_interval = 0
    async with client.inventory(duration=0.1) as sweep:
        await asyncio.sleep(0.12)
    result = sweep.result
    assert result is not None and result.dropped > 0 and result.reports > result.dropped
    await client.close()


def test_sweep_result_defaults() -> None:
    r = SweepResult(SweepEndReason.STOPPED, started_at=None, ended_at=1.0)
    assert (r.reports, r.unique_count, r.elapsed, r.completed) == (0, 0, None, False)
    assert SweepEndReason("duration_elapsed") is SweepEndReason.DURATION_ELAPSED


# FakeUR4 scenarios ------------------------------------------------------------


async def test_scenario_presence_over_time(client: UR4Client, fake: FakeUR4) -> None:
    fake.scenario = [
        ScriptedTag("AA01"),  # always there
        ScriptedTag("AA02", present_until=0.3),  # taken out
        ScriptedTag("AA03", present_from=0.3),  # put in
    ]
    async with client.inventory(duration=0.15) as first:
        async for _ in first:
            pass
    await eventually(lambda: fake.elapsed() > 0.35)
    async with client.inventory(duration=0.15) as second:
        async for _ in second:
            pass
    assert first.result is not None and second.result is not None
    assert first.result.unique_epcs == {"AA01", "AA02"}
    assert second.result.unique_epcs == {"AA01", "AA03"}


async def test_scenario_antennas_and_disabled_ports(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.scenario = [
        ScriptedTag("AA01", antennas=(1, 2)),
        ScriptedTag("AA02", antennas=(4,)),
    ]
    async with client.inventory(duration=0.1) as sweep:
        async for _ in sweep:
            pass
    assert sweep.result is not None
    assert sweep.result.per_antenna_counts.keys() == {1, 2, 4}
    await client.set_antennas(AntennaState.of(1, 2))  # port 4 off
    async with client.inventory(duration=0.1) as sweep:
        async for _ in sweep:
            pass
    assert sweep.result is not None
    assert sweep.result.unique_epcs == {"AA01"}
    assert sweep.result.per_antenna_counts.keys() == {1, 2}


async def test_scenario_flicker_is_seeded_and_repeatable() -> None:
    async def run(seed: int) -> tuple[int, int]:
        async with FakeUR4(seed=seed) as fake:
            fake.tag_interval = 0.002
            fake.scenario = [
                ScriptedTag("AA01"),
                ScriptedTag("AA02", read_probability=0.3),
            ]
            fake.faults.disconnect_after_tags = 200
            client = await client_for(fake)
            with pytest.raises(ConnectionLostError):
                async with client.inventory(duration=10) as sweep:
                    async for _ in sweep:
                        pass
            await client.close()
            assert sweep.result is not None
            counts = {}
            # Count per EPC from the fake side is not exposed; use the result.
            counts["reports"] = sweep.result.reports
            return sweep.result.reports, fake.reports_sent

    a, b = await run(7), await run(7)
    assert a == b  # same seed, same flicker
    reports, sent = a
    assert reports == sent == 200


async def test_scenario_flicker_rate() -> None:
    async with FakeUR4(seed=3) as fake:
        fake.scenario = [
            ScriptedTag("AA01"),
            ScriptedTag("AA02", read_probability=0.25),
        ]
        conn_records = [fake.scenario[1].present(0)]
        assert conn_records == [True]
        # Drive rounds directly for a deterministic rate check.
        from aiochainway.testing import _Conn

        conn = _Conn(fake, writer=None)  # type: ignore[arg-type]  # rounds don't write
        flicker = sum(
            1
            for _ in range(4000)
            for r in conn._scenario_round(fake.scenario)
            if b"\xaa\x02" in r
        )
    assert 800 < flicker < 1200  # ~25 % of 4000 rounds


@pytest.mark.parametrize(
    "kwargs",
    [
        {"read_probability": 1.5},
        {"antennas": ()},
        {"antennas": (0,)},
        {"present_from": 5.0, "present_until": 1.0},
        {"epc": "XYZ"},
    ],
)
def test_scripted_tag_validation(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"epc": "AA01"} | kwargs
    with pytest.raises(ValidationError):
        ScriptedTag(**args)  # type: ignore[arg-type]  # test passthrough


# ping, keepalive, idle drop ------------------------------------------------------


async def test_ping(client: UR4Client, fake: FakeUR4) -> None:
    latency = await client.ping()
    assert 0 <= latency < 0.5
    assert fake.names() == ["get_version"]
    assert client.idle_seconds < 0.5
    fake.faults.drop = {"get_version"}
    with pytest.raises(ReaderTimeout):
        await client.ping()


async def test_idle_connection_dropped_without_keepalive(fake: FakeUR4) -> None:
    lost: list[BaseException | None] = []
    fake.faults.idle_timeout_s = 0.2
    client = await client_for(fake, on_disconnect=lost.append)
    await eventually(lambda: bool(lost))
    assert fake.idle_drops == 1 and not client.connected
    await client.close()


async def test_keepalive_keeps_idle_connection_alive(fake: FakeUR4) -> None:
    fake.faults.idle_timeout_s = 0.3
    client = await client_for(fake, keepalive_interval_s=0.1)
    await asyncio.sleep(0.8)
    assert client.connected and fake.idle_drops == 0
    assert client.keepalive_pings >= 3 and client.keepalive_failures == 0
    assert "get_version" in fake.names()
    await client.close()


async def test_keepalive_pauses_during_sweep(fake: FakeUR4) -> None:
    client = await client_for(fake, keepalive_interval_s=0.05)
    async with client.inventory(duration=0.3):
        pass
    # Tag traffic counts as activity, so no ping was needed during the sweep.
    names = fake.names()
    start, stop = names.index("start_inventory"), names.index("stop_inventory")
    assert "get_version" not in names[start:stop]
    await client.close()


async def test_keepalive_timeout_drops_connection(fake: FakeUR4) -> None:
    lost: list[BaseException | None] = []
    client = await client_for(
        fake, keepalive_interval_s=0.1, timeout=0.2, on_disconnect=lost.append
    )
    fake.faults.drop = {"get_version"}  # reader alive at TCP level but silent
    await eventually(lambda: bool(lost))
    assert client.keepalive_failures == 1 and not client.connected
    await client.close()


async def test_keepalive_reader_error_is_logged_not_fatal(
    fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    fake.state.version = (1, 2, 3)
    client = await client_for(fake, keepalive_interval_s=0.05)
    fake.faults.wrong_sub = set()
    with caplog.at_level(logging.WARNING, logger="aiochainway"):
        # A malformed version reply (too short) is a protocol error, not a drop.
        fake.state.version = (1,)  # type: ignore[assignment]  # malformed on purpose
        await eventually(lambda: client.keepalive_failures >= 1)
    assert client.connected and "Keepalive ping failed" in caplog.text
    await client.close()


def test_keepalive_validation() -> None:
    with pytest.raises(ValidationError):
        UR4Client("h", keepalive_interval_s=0)


# Reconnect, retries, stop_on_connect ----------------------------------------------


async def test_auto_reconnect_after_drop(fake: FakeUR4) -> None:
    reconnected = asyncio.Event()
    client = await client_for(
        fake,
        auto_reconnect=True,
        reconnect_base_delay=0.01,
        reconnect_max_delay=0.05,
        on_reconnect=reconnected.set,
    )
    await fake.drop_connections()
    await asyncio.wait_for(reconnected.wait(), 3)
    await fake.wait_for_connections(2)  # accept may lag connect() on macOS
    assert client.connected and fake.connections == 2
    assert await client.get_version() == "V1.2.3"
    await client.close()


async def test_auto_reconnect_retries_until_reader_returns() -> None:
    fake = FakeUR4()
    await fake.start()
    port = fake.port
    reconnected = asyncio.Event()
    client = await client_for(
        fake,
        auto_reconnect=True,
        reconnect_base_delay=0.02,
        reconnect_max_delay=0.05,
        on_reconnect=reconnected.set,
    )
    await fake.stop()
    await eventually(lambda: not client.connected)
    await asyncio.sleep(0.2)  # several failed attempts
    assert not reconnected.is_set()
    fake2 = FakeUR4()
    await fake2.start(port=port)
    await asyncio.wait_for(reconnected.wait(), 3)
    assert await client.get_version() == "V1.2.3"
    await client.close()
    await fake2.stop()


async def test_close_cancels_auto_reconnect() -> None:
    fake = FakeUR4()
    await fake.start()
    client = await client_for(fake, auto_reconnect=True, reconnect_base_delay=0.05)
    await fake.stop()
    await eventually(lambda: client._reconnect_task is not None)
    await client.close()
    assert client._reconnect_task is None


async def test_on_reconnect_exception_logged(
    fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> None:
        raise RuntimeError("cb")

    client = await client_for(
        fake, auto_reconnect=True, reconnect_base_delay=0.01, on_reconnect=boom
    )
    with caplog.at_level(logging.ERROR, logger="aiochainway"):
        await fake.drop_connections()
        await eventually(lambda: "on_reconnect callback raised" in caplog.text)
    await client.close()


async def test_auto_reconnect_gives_up_on_bad_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with FakeUR4() as fake:
        client = await client_for(fake, auto_reconnect=True, reconnect_base_delay=0.01)
        client.host = "a..b"  # becomes invalid
        with caplog.at_level(logging.ERROR, logger="aiochainway"):
            await fake.drop_connections()
            await eventually(lambda: "gave up" in caplog.text)
        await client.close()


async def test_connect_retries() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = UR4Client(
        "127.0.0.1",
        port,
        timeout=0.3,
        reconnect_base_delay=0.01,
        reconnect_max_delay=0.02,
    )
    with pytest.raises(ConnectionLostError, match="giving up after 3"):
        await client.connect(retries=2)

    fake = FakeUR4()

    async def late_start() -> None:
        await asyncio.sleep(0.15)
        await fake.start(port=port)

    starter = asyncio.create_task(late_start())
    client = UR4Client(
        "127.0.0.1",
        port,
        timeout=0.3,
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.1,
    )
    await client.connect(retries=20)
    await starter
    assert client.connected
    await client.connect(retries=3)  # no-op when connected
    await client.close()
    await fake.stop()


async def test_stop_on_connect(fake: FakeUR4) -> None:
    client = await client_for(
        fake, stop_on_connect=True, auto_reconnect=True, reconnect_base_delay=0.01
    )
    assert fake.names() == ["stop_inventory"]
    reconnected = asyncio.Event()
    client.on_reconnect = reconnected.set
    await fake.drop_connections()
    await asyncio.wait_for(reconnected.wait(), 3)
    assert fake.names() == ["stop_inventory", "stop_inventory"]
    await client.close()


async def test_stop_on_connect_clears_running_reader(fake: FakeUR4) -> None:
    # A previous process left the reader inventorying, then died.
    old = await client_for(fake)
    stream = old.inventory()
    await stream.__anext__()
    old._writer.transport.abort()  # type: ignore[union-attr]  # simulate a crash; no stop sent
    stream._started = False  # don't let __del__ try either
    await old.close()
    new = await client_for(fake, stop_on_connect=True)
    assert fake.names()[-1] == "stop_inventory"
    await new.close()


async def test_stop_on_connect_failure_closes(fake: FakeUR4) -> None:
    fake.faults.disconnect_on = {"stop_inventory"}
    client = UR4Client(
        fake.host, fake.port, timeout=0.3, stop_on_connect=True, stop_drain=1.0
    )
    with pytest.raises(ConnectionLostError):
        await client.connect()
    assert not client.connected
    await client.close()


# Regressions from the v0.3.0 hostile review --------------------------------------


async def test_keepalive_never_pings_during_a_sweep(fake: FakeUR4) -> None:
    fake.scenario = []  # empty field: no traffic at all during the sweep
    fake.faults.drop = {"get_version"}  # a ping would time out and kill the link
    client = await client_for(fake, keepalive_interval_s=0.1, timeout=0.2)
    async with client.inventory(duration=0.6) as sweep:
        async for _ in sweep:
            pass
    assert sweep.result is not None and sweep.result.completed
    assert client.keepalive_pings == 0 and client.connected
    names = fake.names()
    assert (
        "get_version"
        not in names[names.index("start_inventory") : names.index("stop_inventory")]
    )
    await client.close()


async def test_lost_stream_markers_never_evict_tags_or_inflate_dropped() -> None:
    fake = FakeUR4()
    await fake.start()
    client = await client_for(
        fake,
        tag_queue_size=5,
        auto_reconnect=True,
        reconnect_base_delay=0.01,
        reconnect_max_delay=0.02,
    )
    fake.faults.disconnect_after_tags = 5
    sweep = client.inventory(duration=30)
    async with sweep:
        await eventually(lambda: sweep._connection_lost)
        await fake.stop()  # reader gone: many reconnect attempts follow
        await asyncio.sleep(0.2)
        got = []
        with pytest.raises(ConnectionLostError):
            async for tag in sweep:
                got.append(tag)
    assert len(got) == 5 and sweep.dropped == 0
    assert sweep.result is not None
    assert (sweep.result.reports, sweep.result.dropped) == (5, 0)
    assert sweep.result.end_reason is SweepEndReason.CONNECTION_LOST
    await client.close()


async def test_new_sweep_allowed_after_reconnect_and_old_one_not_stopped_there(
    fake: FakeUR4,
) -> None:
    reconnected = asyncio.Event()
    client = await client_for(
        fake,
        auto_reconnect=True,
        reconnect_base_delay=0.01,
        on_reconnect=reconnected.set,
    )
    old = client.inventory(duration=30)
    await old.__aenter__()
    await anext(old)
    await fake.drop_connections()
    await asyncio.wait_for(reconnected.wait(), 3)
    async with client.inventory(duration=0.1) as new:  # not blocked by the dead sweep
        async for _ in new:
            pass
    assert new.result is not None and new.result.completed
    stops_before = fake.names().count("stop_inventory")
    await old.__aexit__(None, None, None)  # consumer finally leaves the old block
    assert (
        old.result is not None
        and old.result.end_reason is SweepEndReason.CONNECTION_LOST
    )
    assert not old.result.stop_sent
    assert (
        fake.names().count("stop_inventory") == stops_before
    )  # nothing sent on the new link
    await client.close()


async def test_break_then_leave_before_deadline_is_stopped(client: UR4Client) -> None:
    async with client.inventory(duration=5) as sweep:
        async for _ in sweep:
            break
        await asyncio.sleep(0.05)
    assert (
        sweep.result is not None and sweep.result.end_reason is SweepEndReason.STOPPED
    )


async def test_break_before_deadline_then_late_exit_is_stopped(
    client: UR4Client,
) -> None:
    # Decision: any early break is stopped, however late the block is left.
    # The consumer iterated one tag, so presence from the loop under-reports.
    loop = asyncio.get_running_loop()
    async with client.inventory(duration=0.1) as sweep:
        start = loop.time()
        async for _ in sweep:
            break
        assert loop.time() - start < 0.1  # the break happened before t=duration
        await asyncio.sleep(0.2)  # ... and the block is left after it
    assert sweep.result is not None
    assert sweep.result.end_reason is SweepEndReason.STOPPED
    assert not sweep.result.completed
    assert sweep.result.reports > 1


async def test_cancel_after_deadline_is_stopped(
    client: UR4Client, fake: FakeUR4
) -> None:
    holder: list[SweepResult | None] = []

    async def run() -> None:
        sweep = client.inventory(duration=0.05)
        try:
            async with sweep:
                await asyncio.sleep(10)
        finally:
            holder.append(sweep.result)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert holder[0] is not None
    assert holder[0].end_reason is SweepEndReason.STOPPED
    assert not holder[0].completed


async def test_no_connection_after_close_while_reconnecting() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = UR4Client(
        "127.0.0.1",
        port,
        timeout=0.3,
        reconnect_base_delay=0.05,
        reconnect_max_delay=0.05,
    )
    task = asyncio.create_task(client.connect(retries=100))
    await asyncio.sleep(0.12)
    await client.close()
    fake = FakeUR4()
    await fake.start(port=port)  # the reader comes back after close()
    with pytest.raises(ConnectionLostError, match="closed during reconnect"):
        await task
    await asyncio.sleep(0.1)
    assert not client.connected and fake.connections == 0
    await fake.stop()


async def test_manual_reconnect_replaces_auto_reconnect() -> None:
    fake = FakeUR4()
    await fake.start()
    port = fake.port
    client = await client_for(fake, auto_reconnect=True, reconnect_base_delay=0.05)
    await fake.stop()
    await eventually(lambda: client._reconnect_task is not None)
    auto = client._reconnect_task
    fake2 = FakeUR4()
    await fake2.start(port=port)
    await client.reconnect(attempts=5, base_delay=0.01)
    assert auto is not None and auto.done() and client._reconnect_task is None
    await fake2.wait_for_connections(1)
    assert client.connected and fake2.connections == 1
    await client.close()
    await fake2.stop()


async def test_all_pending_stop_acks_resolve(client: UR4Client) -> None:
    from aiochainway.protocol.frame import Frame

    loop = asyncio.get_running_loop()
    a, b = loop.create_future(), loop.create_future()
    client._stop_acks.extend([a, b])
    client._dispatch(Frame(0x8D, b"\x01"))
    assert a.done() and b.done() and not client._stop_acks


async def test_completed_sweep_delivers_every_counted_report(fake: FakeUR4) -> None:
    # Presence is derived from iterated tags, so a completed sweep must hand
    # out everything it counted, including tags queued at the deadline.
    client = await client_for(fake)
    fake.tag_interval = 0  # flood: the queue is non-empty at the deadline
    fake.scenario = [ScriptedTag("AA01"), ScriptedTag("AA02", antennas=(2,))]
    delivered = 0
    async with client.inventory(duration=0.1) as sweep:
        async for _ in sweep:
            delivered += 1
    result = sweep.result
    assert result is not None and result.completed and result.dropped == 0
    assert delivered == result.reports > 0
    await client.close()


async def test_iterating_past_completion_ends_cleanly(client: UR4Client) -> None:
    async with client.inventory(duration=0.05) as sweep:
        async for _ in sweep:
            pass
        assert [t async for t in sweep] == []
    assert sweep.result is not None and sweep.result.completed
