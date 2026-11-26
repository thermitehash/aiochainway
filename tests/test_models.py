"""Model validation."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aiochainway.models import (
    AntennaPower,
    AntennaState,
    Gen2Settings,
    InventoryModeConfig,
    ReaderNetworkConfig,
    TagRead,
    TriggerConfig,
    UdpDestination,
    check_antenna,
)
from aiochainway.protocol.errors import ChainwayError, ValidationError


def test_good_models() -> None:
    ReaderNetworkConfig("192.168.99.202", "255.255.255.0", "192.168.99.1", 8888)
    ReaderNetworkConfig("10.0.5.20", "255.255.0.0", "10.0.0.1", 1)
    UdpDestination("192.168.99.100", 65535)
    AntennaPower(4, 30)
    AntennaPower(1, 1, write_power=15.5)
    AntennaState.of(1, 4)
    TriggerConfig(gpi=2, work_time=0xFFFF, interval=0)
    TagRead(epc="AB", pc=0x0800, antenna=1)
    Gen2Settings(link_frequency=7, query_m=3)
    InventoryModeConfig(user_offset=255, user_length=255)


@pytest.mark.parametrize(
    "build",
    [
        lambda: ReaderNetworkConfig("300.1.1.1", "255.255.255.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "255.0.255.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "255.255.255.0", "192.168.2.1"),
        lambda: ReaderNetworkConfig("192.168.1.0", "255.255.255.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.255", "255.255.255.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "0.0.0.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "255.255.255.254", "192.168.1.11"),
        lambda: ReaderNetworkConfig("127.0.0.5", "255.0.0.0", "127.0.0.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "255.255.255.0", "192.168.1.1", 0),
        lambda: ReaderNetworkConfig(
            "192.168.1.10", "255.255.255.0", "192.168.1.1", 65536
        ),
        lambda: ReaderNetworkConfig(
            "192.168.1.10", "255.255.255.0", "192.168.1.1", True
        ),
        lambda: ReaderNetworkConfig("fe80::1", "255.255.255.0", "192.168.1.1"),
        lambda: ReaderNetworkConfig("192.168.1.10", "nope", "192.168.1.1"),
        lambda: UdpDestination("0.0.0.0", 8888),
        lambda: UdpDestination("239.1.1.1", 8888),
        lambda: UdpDestination("192.168.1.1", 0),
        lambda: UdpDestination("192.168.1", 8888),
        lambda: AntennaPower(0, 20),
        lambda: AntennaPower(5, 20),
        lambda: AntennaPower(1, 0),
        lambda: AntennaPower(1, 31),
        lambda: AntennaPower(1, 20, write_power=40),
        lambda: AntennaPower(1, True),
        lambda: AntennaState.of(0),
        lambda: AntennaState.of(1, 5),
        lambda: TriggerConfig(gpi=0),
        lambda: TriggerConfig(gpi=3),
        lambda: TriggerConfig(work_time=-1),
        lambda: TriggerConfig(interval=0x10000),
        lambda: TriggerConfig(report=1),  # type: ignore[arg-type]  # deliberately wrong type
        lambda: TagRead(epc="AB", pc=0, antenna=0),
        lambda: TagRead(epc="ab", pc=0, antenna=1),
        lambda: TagRead(epc="XYZ", pc=0, antenna=1),
        lambda: TagRead(epc="AB", pc=0x10000, antenna=1),
        lambda: TagRead(epc="AB", pc=0, antenna=1, tid="1"),
        lambda: Gen2Settings(q=2),
        lambda: Gen2Settings(link_frequency=8),
        lambda: Gen2Settings(start_q=-1),
        lambda: InventoryModeConfig(user_length=256),
        lambda: check_antenna(True),
    ],
)
def test_bad_models(build: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        build()


def test_validation_error_is_value_error() -> None:
    with pytest.raises(ValueError):
        UdpDestination("x", 1)
    assert issubclass(ValidationError, ChainwayError)


def test_antenna_state_is_enabled() -> None:
    state = AntennaState.of(1, 3)
    assert state.is_enabled(1)
    assert not state.is_enabled(2)
    with pytest.raises(ValidationError):
        state.is_enabled(9)
