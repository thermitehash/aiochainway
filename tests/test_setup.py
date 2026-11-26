"""Guarded provisioning."""

from __future__ import annotations

import pytest

from aiochainway import (
    FrequencyRegion,
    ReaderError,
    UdpDestination,
    UnsafeOperationError,
    ValidationError,
    WorkMode,
    provision,
)
from aiochainway.setup import make_target, plan
from aiochainway.testing import FakeUR4


async def test_dry_run_sends_nothing(fake: FakeUR4) -> None:
    result = await provision(
        "127.0.0.1",
        port=fake.port,
        new_ip="192.168.99.50",
        mask="255.255.255.0",
        gateway="192.168.99.1",
        udp_dest=("192.168.99.100", 8888),
        region=FrequencyRegion.USA,
        antennas=[1, 2],
        power=25,
        dry_run=True,
    )
    assert fake.connections == 0 and fake.received == []
    assert result.dry_run and result.sent == []
    names = [s.name for s in result.steps]
    assert names == [
        "stop_inventory",
        "set_region",
        "set_antennas",
        "set_power_all",
        "set_udp_destination",
        "set_work_mode",
        "set_network_config",
    ]
    assert result.steps[0].frame_hex == "A5 5A 00 08 8C 84 0D 0A"


async def test_requires_confirm_for_network_steps(fake: FakeUR4) -> None:
    with pytest.raises(UnsafeOperationError):
        await provision("127.0.0.1", port=fake.port, udp_dest=("192.168.99.100", 8888))
    assert fake.connections == 0


async def test_provision_applies_in_order_and_skips_already_set(
    fake: FakeUR4,
) -> None:
    result = await provision(
        "127.0.0.1",
        port=fake.port,
        new_ip="192.168.99.50",
        mask="255.255.255.0",
        gateway="192.168.99.1",
        udp_dest=("192.168.99.77", 9000),
        region=FrequencyRegion.USA,  # already USA
        antennas=[1, 2],
        power=25,
        confirm=True,
        timeout=0.5,
    )
    outcomes = {s.name: s.outcome for s in result.steps}
    assert outcomes == {
        "stop_inventory": "sent",
        "set_region": "skipped",
        "set_antennas": "sent",
        "set_power_all": "sent",
        "set_udp_destination": "sent",
        "set_work_mode": "sent",
        "set_network_config": "sent",
    }
    sets = [n for n in fake.names() if n.startswith("set_") or n == "stop_inventory"]
    assert sets == [
        "stop_inventory",
        "set_antennas",
        "set_power",
        "set_udp_destination",
        "set_work_mode",
        "set_network_config",
    ]
    assert fake.names()[-1] == "set_network_config"
    s = fake.state
    assert (s.antenna_mask, s.power[1], s.power[2], s.work_mode) == (0x03, 25, 25, 1)
    assert (s.dest_ip, s.dest_port, s.ip) == ("192.168.99.77", 9000, "192.168.99.50")
    assert result.network_changed and "192.168.99.50" in result.message


async def test_provision_failure_stops_and_reports(fake: FakeUR4) -> None:
    fake.faults.fail = {"set_antennas"}
    with pytest.raises(ReaderError) as info:
        await provision(
            "127.0.0.1", port=fake.port, antennas=[1], power=20, timeout=0.5
        )
    result = info.value.provision_result  # type: ignore[attr-defined]  # attached by provision()
    assert [s.outcome for s in result.steps] == ["sent", "failed"]
    assert "set_power" not in fake.names()


async def test_provision_mode_only(fake: FakeUR4) -> None:
    result = await provision("127.0.0.1", port=fake.port, timeout=0.5)
    assert [s.name for s in result.sent] == ["stop_inventory", "set_work_mode"]
    assert result.message == "Done."
    result = await provision("127.0.0.1", port=fake.port, timeout=0.5)
    assert [s.outcome for s in result.steps] == ["sent", "skipped"]


async def test_provision_reads_failing_still_applies(fake: FakeUR4) -> None:
    fake.faults.drop = {"get_work_mode"}
    result = await provision(
        "127.0.0.1", port=fake.port, work_mode=WorkMode.AUTO, timeout=0.3
    )
    assert result.steps[-1].outcome == "sent"


def test_make_target_validation() -> None:
    with pytest.raises(ValidationError):
        make_target(new_ip="192.168.1.5")
    with pytest.raises(ValidationError):
        make_target(antennas=[])
    with pytest.raises(ValidationError):
        make_target(power=40)
    with pytest.raises(ValidationError):
        make_target(power={7: 20})
    t = make_target(power={1: 20, 2: 21}, udp_dest=UdpDestination("10.0.0.2", 1))
    assert t.power == {1: 20, 2: 21}
    t = make_target(power=10)
    assert t.power == {1: 10, 2: 10, 3: 10, 4: 10}
    assert [s.name for s in plan(make_target())] == ["stop_inventory"]
