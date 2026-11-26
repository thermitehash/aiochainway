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
    assert set(NAMES.values()) | {"set_power_all"} >= {
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
