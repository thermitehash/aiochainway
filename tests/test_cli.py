"""CLI against the fake reader."""

from __future__ import annotations

import asyncio
import io
import json
import socket

import pytest

from aiochainway.cli import async_main, build_parser, main
from aiochainway.protocol.frame import encode
from aiochainway.testing import FakeUR4, tag_record


async def run(fake: FakeUR4 | None, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    base = [
        "--host",
        "127.0.0.1",
        "--port",
        str(fake.port if fake else 1),
        "--timeout",
        "0.5",
    ]
    code = await async_main([*base, *args], stdout=out)
    return code, out.getvalue()


async def test_version_and_json(fake: FakeUR4) -> None:
    assert await run(fake, "version") == (0, "V1.2.3\n")
    code, out = await run(fake, "--json", "version")
    assert code == 0 and json.loads(out) == {"version": "V1.2.3"}


async def test_info(fake: FakeUR4) -> None:
    code, out = await run(fake, "--json", "info")
    data = json.loads(out)
    assert code == 0
    assert data["version"] == "V1.2.3"
    assert data["temperature_c"] == 26.5
    assert data["work_mode"] == "command"
    assert data["region"] == "usa"
    assert data["antennas"] == {"enabled": [1, 2, 3, 4]}
    assert data["udp_destination"] == {"ip": "192.168.99.100", "port": 8888}
    fake.faults.drop = {"get_beep"}
    code, out = await run(fake, "info")
    assert code == 0 and "beep" in out and "error:" in out


async def test_mode(fake: FakeUR4) -> None:
    assert await run(fake, "mode") == (0, "command\n")
    assert await run(fake, "mode", "--set", "auto") == (0, "auto\n")
    assert fake.state.work_mode == 1


async def test_inventory(fake: FakeUR4, capsys: pytest.CaptureFixture[str]) -> None:
    fake.tags = [
        tag_record("AABB", antenna=2),
        tag_record("CCDD", tid="00" * 12, antenna=1),
    ]
    code, out = await run(fake, "inventory", "--seconds", "0.15")
    assert code == 0
    assert "AABB  ant2  -60.0 dBm" in out and "TID=" in out
    code, out = await run(fake, "--json", "inventory", "--seconds", "0.15", "--unique")
    lines = [json.loads(line) for line in out.splitlines()]
    assert sorted(line["epc"] for line in lines) == ["AABB", "CCDD"]
    assert "unique EPC" in capsys.readouterr().err
    assert fake.names()[-1] == "stop_inventory"


async def test_antennas_power_region_beep(fake: FakeUR4) -> None:
    assert await run(fake, "antennas") == (0, "1,2,3,4\n")
    assert await run(fake, "antennas", "--set", "1,3") == (0, "1,3\n")
    code, out = await run(fake, "power", "--set", "20")
    assert code == 0 and out.startswith("ant1=20 dBm")
    code, out = await run(fake, "power", "--set", "12", "--antenna", "2")
    assert "ant2=12 dBm" in out
    assert await run(fake, "region", "--set", "europe") == (0, "europe\n")
    fake.state.region = 0x99
    assert await run(fake, "region") == (0, "0x99\n")
    assert await run(fake, "beep", "--set", "off") == (0, "off\n")


async def test_gpi_gpo_trigger(fake: FakeUR4) -> None:
    assert await run(fake, "gpi") == (0, "input1=low input2=high\n")
    assert await run(fake, "gpo", "--relay", "1") == (0, "ok\n")
    assert fake.received[-1].data == b"\x09\x00\x00\x01"
    code, out = await run(
        fake,
        "trigger",
        "--set",
        "--gpi",
        "2",
        "--work-time",
        "50",
        "--report",
        "serial",
    )
    assert code == 0 and "gpi=2 work_time=50x10ms" in out and "report=serial" in out


async def test_network_and_destination_guard(fake: FakeUR4) -> None:
    code, out = await run(fake, "network")
    assert (code, out) == (
        0,
        "192.168.99.202/255.255.255.0 gw 192.168.99.1 port 8888\n",
    )
    code, _ = await run(
        fake, "network", "--set", "--ip", "192.168.99.50", "--gateway", "192.168.99.1"
    )
    assert code == 5
    assert "set_network_config" not in fake.names()
    code, out = await run(
        fake,
        "network",
        "--set",
        "--ip",
        "192.168.99.50",
        "--gateway",
        "192.168.99.1",
        "--confirm",
    )
    assert code == 0 and "verify" in out
    assert fake.state.ip == "192.168.99.50"
    assert (await run(fake, "destination", "--set", "--ip", "192.168.99.9"))[0] == 5
    assert await run(
        fake,
        "destination",
        "--set",
        "--ip",
        "192.168.99.9",
        "--dest-port",
        "9000",
        "--confirm",
    ) == (0, "192.168.99.9:9000\n")
    fake.state.dest_ip = "0.0.0.0"
    assert await run(fake, "destination") == (0, "not set\n")
    assert (await run(fake, "network", "--set", "--ip", "192.168.99.50"))[0] == 2
    assert (await run(fake, "destination", "--set"))[0] == 2


async def test_factory_reset_guard(fake: FakeUR4) -> None:
    assert (await run(fake, "factory-reset"))[0] == 5
    assert (await run(fake, "factory-reset", "--confirm"))[0] == 0
    assert fake.names() == ["factory_reset"]


async def test_provision_dry_run_and_confirm(fake: FakeUR4) -> None:
    args = [
        "provision",
        "--udp-dest",
        "192.168.99.100:8888",
        "--region",
        "usa",
        "--antennas",
        "1,2",
        "--power",
        "25",
    ]
    code, out = await run(fake, *args, "--dry-run")
    assert code == 0 and "Dry run" in out and "A5 5A" in out
    assert fake.connections == 0
    assert (await run(fake, *args))[0] == 5
    assert fake.connections == 0
    code, out = await run(fake, "--json", *args, "--confirm")
    assert code == 0
    steps = [json.loads(line) for line in out.splitlines()]
    assert steps[-1]["message"] == "Done."


async def test_exit_codes(fake: FakeUR4) -> None:
    fake.faults.drop = {"get_version"}
    assert (await run(fake, "version"))[0] == 3
    fake.faults.fail = {"set_region"}
    assert (await run(fake, "region", "--set", "usa"))[0] == 4
    assert (await run(fake, "power", "--set", "99"))[0] == 2
    assert (await run(fake, "region", "--set", "atlantis"))[0] == 2
    assert (await run(fake, "antennas", "--set", "x"))[0] == 2
    assert (await run(fake, "provision", "--udp-dest", "nope"))[0] == 2
    assert (await run(fake, "provision", "--udp-dest", "1.2.3.4:xx"))[0] == 2
    assert (await run(fake, "gpo", "--relay", "maybe"))[0] == 2
    assert (await run(None, "version"))[0] == 3  # nothing listening on port 1
    assert (await run(fake, "--help"))[0] == 0
    assert (await run(fake))[0] == 2


async def test_listen(capsys: pytest.CaptureFixture[str]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def sender() -> None:
        await asyncio.sleep(0.1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(encode(0x83, tag_record("AABB", antenna=3)), ("127.0.0.1", port))

    task = asyncio.create_task(sender())
    code, out = await run(
        None,
        "listen",
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--seconds",
        "0.4",
        "--source",
        "127.0.0.1",
    )
    await task
    assert code == 0 and "AABB  ant3" in out
    err = capsys.readouterr().err
    assert "macOS" in err and "1 tag(s)" in err


async def test_discover_none() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.close()
    code, _ = await run(None, "discover", "--seconds", "0.05")
    assert code in (0, 3)  # port 1111 may be unavailable in CI; never crashes


async def test_debug_hex_dump(
    fake: FakeUR4, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    logger = logging.getLogger("aiochainway")
    try:
        assert (await run(fake, "--debug", "version"))[0] == 0
        err = capsys.readouterr().err
        assert ">> A5 5A 00 08 02 0A 0D 0A" in err
        assert "<< A5 5A" in err
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)


async def test_encode_decode_offline() -> None:
    assert await run(None, "encode", "get_work_mode") == (
        0,
        "A5 5A 00 09 A1 06 AE 0D 0A\n",
    )
    assert await run(None, "encode", "set_work_mode", "01") == (
        0,
        "A5 5A 00 0A A1 05 01 AF 0D 0A\n",
    )
    assert await run(None, "encode", "0x8C") == (0, "A5 5A 00 08 8C 84 0D 0A\n")
    assert (await run(None, "encode", "bogus"))[0] == 2
    assert (await run(None, "encode", "0x02", "zz"))[0] == 2
    code, out = await run(None, "decode", "A55A00088C840D0A")
    assert code == 0 and "stop_inventory" in out
    tag = encode(0x83, tag_record("AABB", antenna=2)).hex()
    code, out = await run(None, "--json", "decode", tag)
    assert json.loads(out.splitlines()[0])["tag"]["epc"] == "AABB"
    bad_tag = encode(0x83, tag_record("AABB", antenna=9)).hex()
    code, out = await run(None, "decode", bad_tag, "A55A0009A106AE0D0A")
    assert "bad tag record" in out and "config" in out
    code, out = await run(None, "decode", "A55A00088C000D0A")
    assert code == 4 and "1 rejected" in out
    code, out = await run(None, "decode", "00A55A00088C840D0AA55A00088C000D0A")
    assert code == 0 and "1 candidate frame(s) rejected" in out


def test_parser_has_every_subcommand() -> None:
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) >= {  # type: ignore[arg-type]  # choices is a dict here
        "version", "info", "mode", "inventory", "listen", "antennas", "power", "region",
        "gpi", "gpo", "network", "destination", "provision", "decode", "encode",
    }  # fmt: skip


def test_main_entry_point(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["encode", "stop_inventory"]) == 0
    assert capsys.readouterr().out == "A5 5A 00 08 8C 84 0D 0A\n"


async def test_inventory_summary_and_stop_first(
    fake: FakeUR4, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = await run(fake, "inventory", "--seconds", "0.1", "--stop-first")
    assert code == 0
    assert fake.names()[0] == "stop_inventory"
    err = capsys.readouterr().err
    assert "duration_elapsed:" in err and "stop acked" in err


async def test_ping_cli(fake: FakeUR4) -> None:
    code, out = await run(fake, "ping", "--count", "2")
    assert code == 0 and out.count(" ms") == 2
    code, out = await run(fake, "--json", "ping")
    assert json.loads(out)["latency_s"] >= 0


async def test_sweep_cli_repeatability_summary(fake: FakeUR4) -> None:
    from aiochainway.testing import ScriptedTag

    fake.scenario = [ScriptedTag("AA01"), ScriptedTag("AA02", read_probability=0.5)]
    code, out = await run(
        fake, "--json", "sweep", "--seconds", "0.05", "--repeat", "3", "--pause", "0"
    )
    assert code == 0
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["sweep"] for line in lines[:3]] == [1, 2, 3]
    assert lines[0]["result"]["end_reason"] == "duration_elapsed"
    summary = lines[-1]["summary"]
    assert summary["sweeps"] == 3 and summary["epcs_seen_every"] >= 1
    assert summary["unique_min"] <= summary["unique_mean"] <= summary["unique_max"]
    code, out = await run(fake, "sweep", "--seconds", "0.05", "--repeat", "1")
    assert "summary: sweeps=1" in out and "unique_variance=0.0" in out


async def test_idle_cli(fake: FakeUR4) -> None:
    code, out = await run(fake, "idle", "--minutes", "0.003")
    assert code == 0 and "survived" in out
    fake.faults.idle_timeout_s = 0.1
    code, out = await run(fake, "--json", "idle", "--minutes", "0.05")
    data = json.loads(out)
    assert code == 0 and data["survived"] is False and data["dropped_after_s"] < 1
