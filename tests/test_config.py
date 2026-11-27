"""v0.4.0: ensure_config, FakeUR4 persistence, `config`/`antennas` CLI."""

from __future__ import annotations

import io
import json

import pytest

from aiochainway import (
    ConfigResult,
    ReaderConfig,
    ReaderError,
    UnsafeOperationError,
    UR4Client,
    ValidationError,
)
from aiochainway.cli import async_main
from aiochainway.testing import FakeUR4

SETS = {"set_antennas", "set_power"}


def writes_sent(fake: FakeUR4) -> list[str]:
    return [n for n in fake.names() if n in SETS]


async def test_no_op_when_already_configured(client: UR4Client, fake: FakeUR4) -> None:
    result = await client.ensure_config({1, 2, 3, 4}, 30)
    assert isinstance(result, ConfigResult)
    assert (result.changed, result.verified, result.writes) == (False, True, ())
    assert (
        result.before
        == result.after
        == ReaderConfig(frozenset({1, 2, 3, 4}), {1: 30, 2: 30, 3: 30, 4: 30})
    )
    assert writes_sent(fake) == []


async def test_writes_only_what_differs_and_verifies(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.state.power[2] = 27
    result = await client.ensure_config({1, 2}, 27)
    assert result.changed and result.verified and result.mismatches == ()
    assert [w.command for w in result.writes] == ["set_antennas", "set_power"]
    assert result.writes[0].description == "Enable antennas 1,2 (save)"
    assert result.writes[1].description == "Set antenna 1 power to 27 dBm"
    assert result.writes[0].frame_hex == "A5 5A 00 0B 28 01 00 03 21 0D 0A"
    assert writes_sent(fake) == ["set_antennas", "set_power"]  # antenna 2 already 27
    assert fake.state.antenna_mask == 0x03
    assert fake.state.power == {1: 27, 2: 27, 3: 30, 4: 30}  # 3, 4 disabled, untouched
    assert result.after is not None and result.after.antennas == {1, 2}
    assert result.before.antennas == {1, 2, 3, 4}


async def test_power_only_change(client: UR4Client, fake: FakeUR4) -> None:
    result = await client.ensure_config([1, 2, 3, 4], 20)
    assert [w.command for w in result.writes] == ["set_power"] * 4
    assert result.verified and fake.state.power == {1: 20, 2: 20, 3: 20, 4: 20}


async def test_missing_power_record_is_written(
    client: UR4Client, fake: FakeUR4
) -> None:
    del fake.state.power[3]
    result = await client.ensure_config({3}, 30)
    assert [w.command for w in result.writes] == ["set_antennas", "set_power"]
    assert result.verified


@pytest.mark.parametrize(
    ("antennas", "power"),
    [
        (set(), 20),
        ({0}, 20),
        ({5}, 20),
        ({1, 5}, 20),
        ({1}, 0),
        ({1}, 31),
        ({1}, True),
        ({1}, 20.0),
        (5, 20),
    ],
)
async def test_validation_before_anything_is_sent(
    client: UR4Client, fake: FakeUR4, antennas: object, power: object
) -> None:
    with pytest.raises(ValidationError):
        await client.ensure_config(antennas, power)  # type: ignore[arg-type]  # bad on purpose
    assert fake.received == []


async def test_dry_run_reads_but_writes_nothing(
    client: UR4Client, fake: FakeUR4
) -> None:
    result = await client.ensure_config({1}, 25, dry_run=True)
    assert (
        result.dry_run
        and result.changed
        and result.after is None
        and not result.verified
    )
    assert [w.command for w in result.writes] == ["set_antennas", "set_power"]
    assert writes_sent(fake) == [] and fake.names() == ["get_antennas", "get_power"]


async def test_save_false_is_guarded_and_not_persisted(
    client: UR4Client, fake: FakeUR4
) -> None:
    with pytest.raises(UnsafeOperationError, match="inferred"):
        await client.ensure_config({1}, 30, save=False)
    assert fake.received == []
    result = await client.ensure_config({1}, 30, save=False, allow_unverified=True)
    assert result.verified and result.writes[0].command == "set_antennas_no_save"
    assert result.writes[0].frame_hex.startswith("A5 5A 00 0B 28 00 ")
    await fake.power_cycle()
    assert fake.state.antenna_mask == 0x0F  # reverted: the write wasn't saved


async def test_saved_settings_survive_power_cycle(fake: FakeUR4) -> None:
    client = UR4Client(
        fake.host,
        fake.port,
        timeout=0.5,
        auto_reconnect=True,
        reconnect_base_delay=0.01,
    )
    await client.connect()
    await client.ensure_config({2, 4}, 18)
    await fake.power_cycle()
    await fake.wait_for_connections(2)
    cfg = await client.read_config()
    assert cfg.antennas == {2, 4} and cfg.power[2] == cfg.power[4] == 18
    await client.close()


async def test_unsaved_power_via_other_tools_reverts(fake: FakeUR4) -> None:
    fake.state.power[1] = 5  # changed without saving (e.g. another tool)
    await fake.power_cycle()
    assert fake.state.power[1] == 30


async def test_read_back_mismatch_is_reported(client: UR4Client, fake: FakeUR4) -> None:
    fake.faults.ignore_writes = {"set_antennas", "set_power"}
    result = await client.ensure_config({1}, 22)
    assert result.changed and not result.verified
    assert result.mismatches == (
        "antennas read back as [1, 2, 3, 4], expected [1]",
        "antenna 1 power read back as 30.0, expected 22",
    )


async def test_reader_rejecting_a_write_raises(
    client: UR4Client, fake: FakeUR4
) -> None:
    fake.faults.fail = {"set_power"}
    with pytest.raises(ReaderError, match="set_power"):
        await client.ensure_config({1}, 22)
    assert fake.state.antenna_mask == 0x01  # the earlier antenna write stays


async def test_factory_reset_restores_defaults(
    client: UR4Client, fake: FakeUR4
) -> None:
    await client.ensure_config({1}, 10)
    await client.factory_reset(confirm=True)
    assert fake.state.antenna_mask == 0x0F and fake.state.power == {
        1: 30,
        2: 30,
        3: 30,
        4: 30,
    }
    await fake.power_cycle()
    assert fake.state.antenna_mask == 0x0F


# CLI ------------------------------------------------------------------------------


async def cli(fake: FakeUR4 | None, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    base = [
        "--host",
        "127.0.0.1",
        "--port",
        str(fake.port if fake else 1),
        "--timeout",
        "0.5",
    ]
    return await async_main([*base, *args], stdout=out), out.getvalue()


async def test_cli_config_guard_and_dry_run(fake: FakeUR4) -> None:
    assert (await cli(fake, "config", "--antennas", "1,2", "--power", "27"))[0] == 5
    assert (await cli(None, "config", "--antennas", "1,2", "--power", "27"))[
        0
    ] == 5  # before connecting
    assert fake.received == []
    code, out = await cli(
        fake, "config", "--antennas", "1,2", "--power", "27", "--dry-run"
    )
    assert (
        code == 0
        and "would write: Enable antennas 1,2 (save)" in out
        and "nothing was written" in out
    )
    assert writes_sent(fake) == []


async def test_cli_config_confirm(fake: FakeUR4) -> None:
    code, out = await cli(
        fake, "config", "--antennas", "1,2", "--power", "27", "--confirm"
    )
    assert (
        code == 0
        and "wrote: Set antenna 2 power to 27 dBm" in out
        and out.rstrip().endswith("verified")
    )
    code, out = await cli(
        fake, "--json", "config", "--antennas", "1,2", "--power", "27", "--confirm"
    )
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0] == {"changed": False}
    assert lines[-1] == {"changed": False, "verified": True, "mismatches": []}


async def test_cli_config_mismatch_exit_4(fake: FakeUR4) -> None:
    fake.faults.ignore_writes = {"set_power"}
    code, out = await cli(
        fake, "config", "--antennas", "1", "--power", "9", "--confirm"
    )
    assert code == 4 and "NOT verified" in out


async def test_cli_config_usage_and_no_save(fake: FakeUR4) -> None:
    assert (await cli(None, "config", "--antennas", "0", "--power", "27", "--confirm"))[
        0
    ] == 2
    assert (await cli(None, "config", "--antennas", "1", "--power", "31", "--confirm"))[
        0
    ] == 2
    assert (
        await cli(
            fake, "config", "--antennas", "1", "--power", "27", "--confirm", "--no-save"
        )
    )[0] == 5
    code, _ = await cli(
        fake,
        "config",
        "--antennas",
        "1",
        "--power",
        "27",
        "--confirm",
        "--no-save",
        "--allow-unverified",
    )
    assert code == 0
    await fake.power_cycle()
    assert fake.state.antenna_mask == 0x0F


async def test_cli_antennas_json(fake: FakeUR4) -> None:
    code, out = await cli(fake, "--json", "antennas")
    assert code == 0
    assert json.loads(out) == {
        "antennas": [1, 2, 3, 4],
        "power": {"1": 30.0, "2": 30.0, "3": 30.0, "4": 30.0},
        "detection": "unsupported",
    }


async def test_refuses_during_a_sweep(client: UR4Client, fake: FakeUR4) -> None:
    async with client.inventory(duration=5):
        with pytest.raises(UnsafeOperationError, match="during an inventory"):
            await client.ensure_config({1}, 20)
    assert writes_sent(fake) == []
