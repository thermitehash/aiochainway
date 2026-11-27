"""aiochainway.testing: the public fake must stay in sync with the protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import socket
from typing import Any

import pytest

from aiochainway import UR4Client, UR4UdpListener, ValidationError
from aiochainway.models import (
    AntennaState,
    FrequencyRegion,
    Gen2Settings,
    GpoState,
    InventoryModeConfig,
    ReaderNetworkConfig,
    RfLink,
    TriggerConfig,
    UdpDestination,
    WorkMode,
)
from aiochainway.protocol import commands as c
from aiochainway.protocol.frame import Frame, FrameDecoder
from aiochainway.protocol.tags import parse_tag_record
from aiochainway.testing import NAMES, FakeUR4, tag_frames, tag_record

# Arguments for builders that take any. Every supported command is listed in
# test_every_supported_command_has_a_sample, so a new command can't be missed.
SAMPLE_ARGS: dict[str, tuple[Any, ...]] = {
    "set_network_config": (ReaderNetworkConfig("192.168.99.50", "255.255.255.0", "192.168.99.1"),),
    "set_udp_destination": (UdpDestination("192.168.99.100", 9000),),
    "set_work_mode": (WorkMode.AUTO,),
    "set_beep": (False,),
    "set_gpo": (GpoState(relay=True),),
    "set_trigger_config": (TriggerConfig(),),
    "set_antennas": (AntennaState.of(1, 2),),
    "set_antennas_no_save": (AntennaState.of(1, 2),),
    "set_power": (1, 20),
    "set_power_all": ([(1, 20), (2, 21)],),
    "set_region": (FrequencyRegion.USA,),
    "set_rf_link": (RfLink.DSB_ASK_FM0_400K,),
    "set_gen2": (Gen2Settings(),),
    "set_inventory_mode": (InventoryModeConfig(),),
    "get_antenna_work_time": (2,),
    "set_antenna_work_time": (1, 300),
}  # fmt: skip

SUPPORTED_WITH_REPLY = sorted(
    name
    for name, info in c.COMMANDS.items()
    if info.supported and info.reply_cmd is not None
)


def test_names_match_the_command_registry() -> None:
    for (cmd, sub), name in NAMES.items():
        info = c.COMMANDS[name]
        assert (info.cmd, info.sub) == (cmd, sub), name
    # set_power_all shares CMD 0x10 with set_power; the fake labels both set_power.
    # set_antennas_no_save shares CMD 0x28 with set_antennas (flag byte 00).
    assert set(NAMES.values()) | {"set_power_all", "set_antennas_no_save"} >= {
        name for name, info in c.COMMANDS.items() if info.supported
    }


def test_every_supported_command_has_a_sample() -> None:
    import inspect

    for name in SUPPORTED_WITH_REPLY:
        params = inspect.signature(getattr(c, f"build_{name}")).parameters
        assert bool(params) == (name in SAMPLE_ARGS), name


@pytest.mark.parametrize("name", SUPPORTED_WITH_REPLY)
async def test_fake_answers_every_supported_command(name: str) -> None:
    build: Callable[..., c.Request] = getattr(c, f"build_{name}")
    parse: Callable[[Frame], object] = getattr(c, f"parse_{name}")
    request = build(*SAMPLE_ARGS.get(name, ()))
    async with FakeUR4() as fake:
        reader, writer = await asyncio.open_connection(fake.host, fake.port)
        try:
            writer.write(request.encode())
            await writer.drain()
            decoder = FrameDecoder()
            async with asyncio.timeout(2):
                while True:
                    frames = [
                        f
                        for f in decoder.feed(await reader.read(4096))
                        if request.matches(f)
                    ]
                    if frames:
                        break
            parse(frames[0])  # must not raise: the fake's reply is valid
        finally:
            writer.close()
            await writer.wait_closed()


async def test_public_api_round_trip() -> None:
    async with FakeUR4() as fake:
        fake.tags = [tag_record("AABB", antenna=2, rssi_dbm=-55.0)]
        async with UR4Client(fake.host, fake.port, timeout=1) as client:
            assert await client.get_version() == "V1.2.3"
            tags = [t async for t in client.inventory(duration=0.1)]
        assert tags and tags[0].epc == "AABB" and tags[0].rssi_dbm == -55.0
        assert fake.names()[0] == "get_version"
        await fake.wait_for("stop_inventory")
    await fake.stop()  # idempotent


async def test_tag_frames_feed_a_listener() -> None:
    listener = await UR4UdpListener.start(("127.0.0.1", 0))
    assert listener.local_address is not None
    it = aiter(listener)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(
            tag_frames(tag_record("AABB"), tag_record("CCDD", antenna=4)),
            listener.local_address,
        )
    got = [await asyncio.wait_for(anext(it), 2), await asyncio.wait_for(anext(it), 2)]
    assert [(t.epc, t.antenna) for t in got] == [("AABB", 1), ("CCDD", 4)]
    await listener.stop()


def test_tag_record_layouts_parse() -> None:
    tag = parse_tag_record(
        tag_record("AABB", tid="11" * 12, user="2233", antenna=3, rssi_dbm=None)
    )
    assert (tag.epc, tag.tid, tag.user, tag.antenna, tag.rssi_dbm) == (
        "AABB",
        "11" * 12,
        "2233",
        3,
        None,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"epc": "ABC"},
        {"epc": "AA"},
        {"epc": "AB" * 63},
        {"epc": "AABB", "tid": "zz"},
        {"epc": "AABB", "antenna": 256},
    ],
)
def test_tag_record_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        tag_record(**kwargs)


async def test_stop_with_a_connection_whose_handler_has_not_run() -> None:
    # On macOS connect() can return before the fake's accept handler runs.
    # stop() used to miss that connection and wait_closed() hung forever.
    fake = FakeUR4()
    original = fake._handle

    async def late(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(0.2)
        await original(reader, writer)

    fake._handle = late  # type: ignore[method-assign]  # simulate a late accept
    await fake.start()
    client = UR4Client(fake.host, fake.port, timeout=0.5)
    await client.connect()
    async with asyncio.timeout(3):
        await fake.stop()
    await asyncio.sleep(0.3)  # the late handler must refuse, not register
    assert fake.connections == 0
    await client.close()


# macOS (kqueue) accept-timing regressions ------------------------------------------
#
# On macOS a client's connect() routinely returns before FakeUR4's accept handler
# runs (kqueue reports the listening socket later than epoll does). FakeUR4 used
# to assume the handler had run: bug #19 (drop_connections closed nothing) and the
# v0.3.0 CI hang (stop() missed the connection; wait_closed() never returned).
# The tests below delay the handler on purpose so every OS sees macOS timing,
# plus one stress test that hits the real race on macOS runners.


def _with_late_accept(fake: FakeUR4, delay: float = 0.2) -> None:
    original = fake._handle

    async def late(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(delay)
        await original(reader, writer)

    fake._handle = late  # type: ignore[method-assign]  # simulate kqueue timing


async def test_late_accept_wait_for_connections() -> None:
    fake = FakeUR4()
    _with_late_accept(fake)
    await fake.start()
    client = UR4Client(fake.host, fake.port, timeout=0.5)
    await client.connect()
    assert fake.connections == 0  # connect() returned; the handler hasn't run
    await fake.wait_for_connections(1)
    assert fake.connections == 1
    await client.close()
    await fake.stop()


async def test_late_accept_drop_connections() -> None:
    fake = FakeUR4()
    _with_late_accept(fake)
    await fake.start()
    lost: list[BaseException | None] = []
    client = UR4Client(fake.host, fake.port, timeout=0.5, on_disconnect=lost.append)
    await client.connect()
    await fake.drop_connections()  # waits for the late handler, then closes
    async with asyncio.timeout(3):
        while not lost:
            await asyncio.sleep(0.01)
    await client.close()
    await fake.stop()


async def test_wait_for_connections_times_out() -> None:
    async with FakeUR4() as fake:
        with pytest.raises(TimeoutError):
            await fake.wait_for_connections(1, timeout=0.05)


async def test_stop_right_after_connect_stress() -> None:
    # No artificial delay: on macOS this exercises the real accept race.
    for _ in range(40):
        fake = FakeUR4()
        await fake.start()
        client = UR4Client(fake.host, fake.port, timeout=0.5)
        await client.connect()
        async with asyncio.timeout(3):
            await fake.stop()
        await client.close()
