"""UR4Client against the fake reader, including fault injection."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging

import pytest

from aiochainway import (
    AntennaPower,
    AntennaState,
    ConnectionLostError,
    FrequencyRegion,
    Gen2Settings,
    GpiState,
    GpoState,
    InventoryMode,
    InventoryModeConfig,
    ReaderError,
    ReaderNetworkConfig,
    ReaderTimeout,
    RfLink,
    TriggerConfig,
    TriggerReport,
    UdpDestination,
    UnsafeOperationError,
    UnsupportedCommandError,
    UR4Client,
    ValidationError,
    WorkMode,
    wait_for_reader,
)
from aiochainway.client import check_guard
from aiochainway.protocol import commands as c
from aiochainway.protocol.frame import Frame
from aiochainway.testing import FakeUR4, tag_record

from .conftest import eventually

EPC = "E28011700000020A1B2C3D4E"


def is_connected(client: UR4Client) -> bool:
    return client.connected


# Reads and writes ----------------------------------------------------------------


async def test_reads(client: UR4Client) -> None:
    assert client.connected
    assert await client.get_version() == "V1.2.3"
    assert await client.get_temperature() == 26.5
    info = await client.get_info()
    assert (info.version, info.temperature) == ("V1.2.3", 26.5)
    assert await client.get_work_mode() is WorkMode.COMMAND
    assert await client.get_network_config() == ReaderNetworkConfig(
        "192.168.99.202", "255.255.255.0", "192.168.99.1", 8888
    )
    assert await client.get_udp_destination() == UdpDestination("192.168.99.100", 8888)
    assert await client.get_antennas() == AntennaState.of(1, 2, 3, 4)
    assert await client.get_power_all() == [
        AntennaPower(a, 30, 30) for a in (1, 2, 3, 4)
    ]
    assert await client.get_region() is FrequencyRegion.USA
    assert await client.get_gpi() == GpiState(False, True)
    assert await client.get_trigger_config() == TriggerConfig(
        1, 100, 0, TriggerReport.UDP
    )
    assert await client.get_beep() is True
    assert await client.get_rf_link() is RfLink.PR_ASK_MILLER4_250K
    assert await client.get_gen2() == c.parse_get_gen2(
        Frame(0x23, bytes((0x01, 0x40, 0xF1, 0x00)))
    )
    assert await client.get_inventory_mode() == InventoryModeConfig()
    assert await client.get_antenna_work_time(2) == 200


async def test_writes_round_trip(client: UR4Client, fake: FakeUR4) -> None:
    await client.set_work_mode(WorkMode.AUTO)
    assert await client.get_work_mode() is WorkMode.AUTO
    await client.set_antennas([1, 3])
    assert await client.get_antennas() == AntennaState.of(1, 3)
    await client.set_antennas(AntennaState.of(2))
    assert fake.state.antenna_mask == 0x0002
    await client.set_power(2, 21)
    await client.set_power_all({3: 15, 4: 16})
    assert fake.state.power == {1: 30, 2: 21, 3: 15, 4: 16}
    await client.set_region(FrequencyRegion.EUROPE)
    assert await client.get_region() is FrequencyRegion.EUROPE
    await client.set_gpo(relay=True)
    await client.set_gpo(GpoState(output1=True))
    await client.set_trigger_config(TriggerConfig(2, 50, 10, TriggerReport.SERIAL))
    assert await client.get_trigger_config() == TriggerConfig(
        2, 50, 10, TriggerReport.SERIAL
    )
    await client.set_beep(False)
    assert await client.get_beep() is False
    await client.set_rf_link(RfLink.DSB_ASK_FM0_400K)
    assert await client.get_rf_link() is RfLink.DSB_ASK_FM0_400K
    await client.set_gen2(Gen2Settings(query_session=1))
    assert (await client.get_gen2()).query_session == 1
    await client.set_inventory_mode(
        InventoryModeConfig(InventoryMode.EPC_TID_USER, 0, 4)
    )
    assert await client.get_inventory_mode() == InventoryModeConfig(
        InventoryMode.EPC_TID_USER, 0, 4
    )
    await client.set_antenna_work_time(1, 500)
    assert await client.get_antenna_work_time(1) == 500


async def test_failure_status_raises_reader_error(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.fail = {"set_region", "get_temperature"}
    with pytest.raises(ReaderError, match="set_region") as info:
        await client.set_region(FrequencyRegion.USA)
    assert info.value.status == 0x00
    info2 = await client.get_info()
    assert info2.temperature is None


async def test_validation_before_sending(client: UR4Client, fake: FakeUR4) -> None:
    before = len(fake.received)
    with pytest.raises(ValidationError):
        await client.set_power(1, 99)
    with pytest.raises(ValidationError):
        await client.set_antennas([])
    assert len(fake.received) == before


async def test_unsupported(client: UR4Client, fake: FakeUR4) -> None:
    for call in (
        client.set_fastid(True),
        client.set_tagfocus(True),
        client.read_tag_buffer(),
    ):
        with pytest.raises(UnsupportedCommandError):
            await call
    assert fake.received == []


# Guards ----------------------------------------------------------------------------


async def test_guarded_commands_refuse_without_confirm(
    client: UR4Client, fake: FakeUR4
) -> None:
    net = ReaderNetworkConfig("192.168.99.50", "255.255.255.0", "192.168.99.1", 8888)
    with pytest.raises(UnsafeOperationError):
        await client.set_network_config(net)
    with pytest.raises(UnsafeOperationError):
        await client.set_udp_destination(UdpDestination("192.168.99.9", 9000))
    with pytest.raises(UnsafeOperationError):
        await client.factory_reset()
    assert fake.received == []


async def test_guarded_commands_with_confirm(client: UR4Client, fake: FakeUR4) -> None:
    net = ReaderNetworkConfig("192.168.99.50", "255.255.255.0", "192.168.99.1", 9999)
    result = await client.set_network_config(net, confirm=True)
    assert result.accepted and "verify" in result.message
    assert (fake.state.ip, fake.state.port) == ("192.168.99.50", 9999)
    await client.set_udp_destination(UdpDestination("192.168.99.9", 9000), confirm=True)
    assert (fake.state.dest_ip, fake.state.dest_port) == ("192.168.99.9", 9000)
    await client.factory_reset(confirm=True)
    assert fake.names()[-1] == "factory_reset"


def test_check_guard_inferred_needs_allow_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = c.COMMANDS["set_udp_destination"]
    monkeypatch.setitem(
        c.COMMANDS,
        "set_udp_destination",
        c.CommandInfo(
            info.name,
            info.cmd,
            info.sub,
            info.reply_cmd,
            c.SpecStatus.INFERRED,
            info.sdk_method,
            info.summary,
            guarded=True,
        ),
    )
    with pytest.raises(UnsafeOperationError, match="inferred"):
        check_guard("set_udp_destination", confirm=True, allow_unverified=False)
    check_guard("set_udp_destination", confirm=True, allow_unverified=True)


# Fault injection -------------------------------------------------------------------


async def test_timeout_names_command_and_releases_lock(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.drop = {"get_version"}
    with pytest.raises(ReaderTimeout, match="get_version") as info:
        await client.get_version()
    assert info.value.command == "get_version"
    assert isinstance(info.value, TimeoutError)
    assert not client._lock.locked()
    assert await client.get_work_mode() is WorkMode.COMMAND


async def test_delayed_reply_beyond_timeout_is_not_misassigned(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.delay = 0.7  # > client timeout 0.5
    with pytest.raises(ReaderTimeout):
        await client.get_version()
    fake.faults.delay = 0.0
    # The late 0x03 reply lands during the quarantine and is dropped; the
    # retry gets its own reply.
    assert await client.get_version() == "V1.2.3"
    await eventually(lambda: client.unmatched_frames >= 1)
    assert await client.get_work_mode() is WorkMode.COMMAND


async def test_slow_but_in_time(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.delay = 0.2
    assert await client.get_version() == "V1.2.3"


@pytest.mark.parametrize("mode", ["split", "chunk"])
async def test_partial_writes(client: UR4Client, fake: FakeUR4, mode: str) -> None:
    if mode == "split":
        fake.faults.split_bytes = True
    else:
        fake.faults.chunk = 3
    assert await client.get_network_config() == ReaderNetworkConfig(
        "192.168.99.202", "255.255.255.0", "192.168.99.1", 8888
    )
    assert await client.get_power_all() == [
        AntennaPower(a, 30, 30) for a in (1, 2, 3, 4)
    ]


async def test_corrupted_xor_times_out_then_recovers(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.corrupt_xor_next = 1
    with pytest.raises(ReaderTimeout):
        await client.get_version()
    assert client._decoder.bad_frames == 1
    assert await client.get_version() == "V1.2.3"


async def test_duplicate_replies_are_dropped(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.duplicate = True
    assert await client.get_version() == "V1.2.3"
    assert await client.get_work_mode() is WorkMode.COMMAND
    assert await client.get_region() is FrequencyRegion.USA
    await eventually(lambda: client.unmatched_frames == 3)
    await asyncio.sleep(0.05)
    assert client.unmatched_frames == 3


async def test_wrong_sub_command_not_accepted(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.wrong_sub = {"get_work_mode"}
    with pytest.raises(ReaderTimeout):
        await client.get_work_mode()


async def test_unsolicited_tags_between_replies(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.unsolicited_tags = 3
    assert await client.get_version() == "V1.2.3"
    assert await client.get_beep() is True
    assert client.unmatched_frames == 0  # tag frames are dropped separately


async def test_refused_connection() -> None:
    fake = FakeUR4()
    await fake.start()
    port = fake.port
    await fake.stop()
    client = UR4Client("127.0.0.1", port, timeout=0.5)
    with pytest.raises(ConnectionLostError, match="cannot connect"):
        await client.connect()
    assert not client.connected
    with pytest.raises(ConnectionLostError, match="giving up after 3"):
        await client.reconnect(attempts=3, base_delay=0.01, max_delay=0.02)
    await client.close()


async def test_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hang(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio, "open_connection", hang)
    client = UR4Client("127.0.0.1", 1, timeout=0.05)
    with pytest.raises(ReaderTimeout, match="connect"):
        await client.connect()


async def test_not_connected_raises() -> None:
    client = UR4Client("127.0.0.1", 9, timeout=0.1)
    with pytest.raises(ConnectionLostError, match="not connected"):
        await client.get_version()
    with pytest.raises(ConnectionLostError):
        async for _ in client.inventory(duration=0.1):
            pass


async def test_disconnect_fails_pending_and_calls_callback(fake: FakeUR4) -> None:
    lost: list[BaseException | None] = []
    client = UR4Client("127.0.0.1", fake.port, timeout=1.0, on_disconnect=lost.append)
    await client.connect()
    fake.faults.disconnect_on = {"get_version"}
    with pytest.raises(ConnectionLostError):
        await client.get_version()
    await eventually(lambda: len(lost) == 1)
    assert not client.connected
    fake.faults.disconnect_on = set()
    await client.reconnect(attempts=3, base_delay=0.01)
    assert is_connected(client)  # helper: mypy narrowed .connected above
    assert await client.get_version() == "V1.2.3"
    await fake.wait_for_connections(2)
    assert fake.connections == 2
    await client.close()
    await client.close()  # idempotent
    assert len(lost) == 1  # close() is not a disconnect


async def test_callback_exception_is_logged(
    fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(_: BaseException | None) -> None:
        raise RuntimeError("callback bug")

    client = UR4Client("127.0.0.1", fake.port, timeout=0.5, on_disconnect=boom)
    await client.connect()
    with caplog.at_level(logging.ERROR, logger="aiochainway"):
        await fake.drop_connections()
        await eventually(lambda: "on_disconnect callback raised" in caplog.text)
    await client.close()


async def test_context_manager_and_debug_logging(
    fake: FakeUR4, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="aiochainway"):
        async with UR4Client("127.0.0.1", fake.port, timeout=0.5) as client:
            await client.get_version()
            await client.connect()  # no-op when connected
    assert ">> A5 5A 00 08 02 0A 0D 0A" in caplog.text
    assert "<< A5 5A 00 0B 03 01 02 03" in caplog.text
    assert not client.connected


def test_constructor_validation() -> None:
    with pytest.raises(ValidationError):
        UR4Client("h", timeout=0)
    with pytest.raises(ValidationError):
        UR4Client("h", port=0)


async def test_wait_for_reader(fake: FakeUR4) -> None:
    waited = await wait_for_reader("127.0.0.1", fake.port, timeout=2)
    assert waited < 2
    await fake.stop()
    with pytest.raises(ReaderTimeout):
        await wait_for_reader("127.0.0.1", fake.port, timeout=0.2)


# Inventory -------------------------------------------------------------------------


async def test_inventory_duration_sends_stop(client: UR4Client, fake: FakeUR4) -> None:
    fake.tags = [
        tag_record(EPC, antenna=1),
        tag_record("AABB", antenna=3, rssi_dbm=-71.2),
    ]
    tags = [t async for t in client.inventory(duration=0.2)]
    assert len(tags) >= 4
    assert {t.epc for t in tags} == {EPC, "AABB"}
    assert {t.antenna for t in tags} == {1, 3}
    assert all(t.source == f"tcp:127.0.0.1:{fake.port}" for t in tags)
    assert fake.names()[0] == "start_inventory"
    assert fake.names()[-1] == "stop_inventory"
    # Usable afterwards, and stray tags are drained.
    assert await client.get_version() == "V1.2.3"


async def test_inventory_break_sends_stop(client: UR4Client, fake: FakeUR4) -> None:
    async for tag in client.inventory():
        assert tag.epc == EPC
        break
    await fake.wait_for("stop_inventory")
    assert fake.names().count("stop_inventory") == 1
    gc.collect()
    assert fake.names().count("stop_inventory") == 1


async def test_inventory_exception_in_body_sends_stop(
    client: UR4Client, fake: FakeUR4
) -> None:
    stream = client.inventory()
    with pytest.raises(RuntimeError, match="consumer"):
        async with stream:
            async for _ in stream:
                raise RuntimeError("consumer bug")
    await fake.wait_for("stop_inventory")


async def test_inventory_cancelled_from_another_task_sends_stop(
    client: UR4Client, fake: FakeUR4
) -> None:
    got: list[str] = []

    async def consume() -> None:
        async for tag in client.inventory():
            got.append(tag.epc)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await fake.wait_for("stop_inventory")
    assert got
    assert client._inventory is None
    assert await client.get_version() == "V1.2.3"


async def test_inventory_abandoned_stream_sends_stop_on_gc(
    client: UR4Client, fake: FakeUR4
) -> None:
    stream = client.inventory()
    await stream.__anext__()
    del stream
    gc.collect()
    await fake.wait_for("stop_inventory")


async def test_inventory_without_stop_ack(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.no_stop_ack = True
    loop = asyncio.get_running_loop()
    start = loop.time()
    tags = [t async for t in client.inventory(duration=0.05)]
    assert tags
    assert loop.time() - start < 0.05 + client.stop_drain + 0.2
    assert fake.names()[-1] == "stop_inventory"


async def test_inventory_disconnect_mid_inventory(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.disconnect_after_tags = 5
    tags = []
    with pytest.raises(ConnectionLostError):
        async for tag in client.inventory(duration=5):
            tags.append(tag)
    assert len(tags) == 5
    assert not client.connected
    await client.reconnect(attempts=2, base_delay=0.01)
    assert await client.get_version() == "V1.2.3"


async def test_second_concurrent_inventory_refused(
    client: UR4Client, fake: FakeUR4
) -> None:
    async with client.inventory() as first:
        await first.__anext__()
        with pytest.raises(UnsafeOperationError):
            async for _ in client.inventory():
                pass
        # The refused stream must not have stopped the running one.
        assert "stop_inventory" not in fake.names()
        await first.__anext__()
    await fake.wait_for("stop_inventory")


async def test_inventory_commands_during_inventory(client: UR4Client) -> None:
    async for _ in client.inventory(duration=0.3):
        assert await client.get_version() == "V1.2.3"
        break


async def test_inventory_skips_bad_tag_records(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.tags = [tag_record(EPC, antenna=9), tag_record(EPC, antenna=2)]
    tags = [t async for t in client.inventory(duration=0.1)]
    assert tags and {t.antenna for t in tags} == {2}


async def test_inventory_queue_overflow_drops_oldest(fake: FakeUR4) -> None:
    client = UR4Client(
        "127.0.0.1", fake.port, timeout=0.5, tag_queue_size=3, stop_drain=0.1
    )
    await client.connect()
    fake.tag_interval = 0
    stream = client.inventory(duration=1)
    async with stream:
        await stream.__anext__()
        await eventually(lambda: stream.dropped > 0)
    await client.close()


async def test_inventory_invalid_duration(client: UR4Client) -> None:
    with pytest.raises(ValidationError):
        client.inventory(duration=0)


async def test_stream_iterates_after_finish_raises_stop(client: UR4Client) -> None:
    stream = client.inventory(duration=0.05)
    _ = [t async for t in stream]
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    await stream.aclose()  # idempotent


async def test_stop_inventory_standalone(client: UR4Client, fake: FakeUR4) -> None:
    await client.stop_inventory()
    assert fake.names() == ["stop_inventory"]
    fake.faults.no_stop_ack = True
    await client.stop_inventory()
    async with client.inventory() as stream:
        await stream.__anext__()
        await client.stop_inventory()  # stops the running stream
    assert fake.names().count("stop_inventory") == 3


# Concurrency -----------------------------------------------------------------------


async def test_fifty_concurrent_requests_serialize(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.chunk = 2
    calls = [
        client.get_version() if i % 2 else client.get_work_mode() for i in range(50)
    ]
    results = await asyncio.gather(*calls)
    assert results == ["V1.2.3" if i % 2 else WorkMode.COMMAND for i in range(50)]
    assert len(fake.received) == 50


async def test_cancel_during_request_leaves_client_usable(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.delay = 0.2
    task = asyncio.create_task(client.get_version())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client._lock.locked()
    assert client._pending is None
    fake.faults.delay = 0
    await eventually(lambda: client.unmatched_frames == 1)  # late reply dropped
    assert await client.get_version() == "V1.2.3"
    assert client.unmatched_frames == 1


async def test_close_while_request_pending(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.drop = {"get_version"}
    task = asyncio.create_task(client.get_version())
    await asyncio.sleep(0.05)
    await client.close()
    with pytest.raises(ConnectionLostError):
        await task


async def test_close_during_inventory(client: UR4Client) -> None:
    async def consume() -> None:
        async for _ in client.inventory():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await client.close()
    with pytest.raises(ConnectionLostError):
        await task


async def test_write_failure_maps_to_connection_lost(client: UR4Client) -> None:
    assert client._writer is not None
    client._writer.transport.abort()
    await eventually(lambda: not client.connected)
    with contextlib.suppress(ConnectionLostError):
        await client.get_version()
    assert not client.connected
