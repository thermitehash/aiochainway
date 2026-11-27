"""Command-line interface: ``aiochainway``.

Exit codes: 0 ok, 2 usage / invalid argument, 3 timeout or reader unreachable,
4 protocol error or reader-reported failure, 5 unsafe operation refused.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import contextlib
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
import logging
import os
import statistics
import sys
import time
from typing import Any, TextIO

from . import __version__
from .client import UR4Client, check_guard
from .listener import UR4UdpListener, discover
from .models import (
    AntennaPower,
    FrequencyRegion,
    GpoState,
    ReaderNetworkConfig,
    SweepResult,
    TagRead,
    TriggerConfig,
    TriggerReport,
    UdpDestination,
    WorkMode,
)
from .protocol import commands as c
from .protocol.errors import (
    ChainwayError,
    ConnectionLostError,
    FrameError,
    ProtocolError,
    ReaderError,
    ReaderTimeout,
    UnsafeOperationError,
    UnsupportedCommandError,
    ValidationError,
)
from .protocol.frame import FrameDecoder, encode
from .protocol.tags import parse_tag_record
from .setup import provision

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TIMEOUT = 3
EXIT_PROTOCOL = 4
EXIT_UNSAFE = 5

DEFAULT_HOST = os.environ.get("AIOCHAINWAY_HOST", "192.168.99.202")

FIREWALL_NOTE = (
    "macOS: the first time, the system may ask whether Python may accept incoming "
    "network connections. Click Allow, or UDP tag frames will be silently dropped "
    "(System Settings > Network > Firewall > Options)."
)


class UsageError(Exception):
    """Bad command-line arguments (exit 2)."""


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.name.lower()
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Output:
    """Write human or JSON output."""

    def __init__(self, stream: TextIO, as_json: bool) -> None:
        """Bind to a stream."""
        self.stream = stream
        self.as_json = as_json

    def emit(self, human: str, data: Any = None) -> None:
        """Print ``human`` text, or ``data`` as one JSON line with ``--json``."""
        if self.as_json:
            self.stream.write(
                json.dumps(_jsonable(data if data is not None else human)) + "\n"
            )
        else:
            self.stream.write(human + "\n")
        self.stream.flush()


def _enum_arg[E: Enum](enum: type[E]) -> Any:
    def parse(text: str) -> E:
        key = text.strip().upper().replace("-", "_")
        try:
            return enum[key]
        except KeyError:
            names = ", ".join(m.name.lower() for m in enum)
            raise argparse.ArgumentTypeError(f"choose from: {names}") from None

    parse.__name__ = enum.__name__
    return parse


def _antenna_list(text: str) -> list[int]:
    try:
        return sorted({int(part) for part in text.replace(" ", "").split(",") if part})
    except ValueError:
        raise argparse.ArgumentTypeError("antennas look like 1,2,3,4") from None


def _host_port(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError("expected IP:PORT, e.g. 192.168.99.100:8888")
    try:
        return host, int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad port in {text!r}") from None


def _port(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {value}")
    return value


def _bool01(text: str) -> bool:
    if text.lower() in {"1", "on", "high", "closed", "true"}:
        return True
    if text.lower() in {"0", "off", "low", "open", "false"}:
        return False
    raise argparse.ArgumentTypeError("use 0/1, on/off")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="aiochainway",
        description="Talk to a Chainway UR4 UHF RFID reader (unofficial).",
        epilog=f"Exit codes: 0 ok, 2 usage, 3 timeout/unreachable, 4 protocol error, 5 unsafe operation refused. {FIREWALL_NOTE}",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="reader IP (default %(default)s, env AIOCHAINWAY_HOST)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=c.DEFAULT_TCP_PORT,
        help="reader TCP port (default %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="seconds to wait for each reply (default %(default)s)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="log every frame in both directions as hex"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    parser.add_argument(
        "--version", action="version", version=f"aiochainway {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("version", help="firmware version")
    sub.add_parser("info", help="version, temperature and current settings")

    p = sub.add_parser("mode", help="show or set the work mode")
    p.add_argument(
        "--set",
        type=_enum_arg(WorkMode),
        metavar="MODE",
        help="command, auto or trigger",
    )

    p = sub.add_parser("inventory", help="read tags over TCP (command mode)")
    p.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="how long to read (default %(default)s)",
    )
    p.add_argument("--unique", action="store_true", help="print each EPC once")
    p.add_argument(
        "--stop-first",
        action="store_true",
        help="send stop inventory on connect (clears a reader left running)",
    )

    p = sub.add_parser("ping", help="firmware-version round trip; print latency")
    p.add_argument("--count", type=int, default=1)

    p = sub.add_parser(
        "sweep",
        help="repeat timed sweeps and summarise them (hardware validation §E)",
    )
    p.add_argument("--seconds", type=float, default=45.0)
    p.add_argument("--repeat", type=int, default=10)
    p.add_argument("--pause", type=float, default=5.0, help="seconds between sweeps")

    p = sub.add_parser(
        "idle",
        help="hold an idle connection, then ping (does the reader drop idle TCP?)",
    )
    p.add_argument("--minutes", type=float, default=60.0)

    p = sub.add_parser(
        "listen",
        help="receive auto/trigger-mode tags over UDP",
        description=FIREWALL_NOTE,
    )
    p.add_argument(
        "--port",
        dest="udp_port",
        type=_port,
        default=8888,
        help="UDP port to listen on (default %(default)s)",
    )
    p.add_argument(
        "--bind", default="0.0.0.0", help="local address to bind (default %(default)s)"
    )
    p.add_argument(
        "--source",
        action="append",
        metavar="IP",
        help="only accept datagrams from this IP (repeatable)",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="stop after this long (default: until Ctrl-C)",
    )

    p = sub.add_parser("antennas", help="show or set enabled antennas")
    p.add_argument("--set", type=_antenna_list, metavar="LIST", help="e.g. 1,2")

    p = sub.add_parser("power", help="show or set antenna power (dBm)")
    p.add_argument("--set", type=int, metavar="DBM", help="1-30")
    p.add_argument(
        "--antenna",
        type=int,
        action="append",
        help="only this antenna (repeatable; default all four)",
    )

    p = sub.add_parser("region", help="show or set the frequency region")
    p.add_argument(
        "--set",
        type=_enum_arg(FrequencyRegion),
        metavar="REGION",
        help="e.g. usa, europe",
    )

    sub.add_parser("gpi", help="read GPI inputs")

    p = sub.add_parser("gpo", help="set GPO outputs and relay (all three are written)")
    p.add_argument("--output1", type=_bool01, default=False)
    p.add_argument("--output2", type=_bool01, default=False)
    p.add_argument("--relay", type=_bool01, default=False)

    p = sub.add_parser("beep", help="show or set beep-on-read")
    p.add_argument("--set", type=_bool01, metavar="on|off")

    p = sub.add_parser("trigger", help="show or set trigger-mode parameters")
    p.add_argument("--set", action="store_true", help="write the values below")
    p.add_argument("--gpi", type=int, choices=(1, 2), default=1)
    p.add_argument(
        "--work-time",
        type=int,
        default=100,
        help="units of 10 ms (default %(default)s = 1 s)",
    )
    p.add_argument("--interval", type=int, default=0, help="units of 10 ms")
    p.add_argument("--report", type=_enum_arg(TriggerReport), default=TriggerReport.UDP)

    for name, helptext in (
        ("network", "show or set the reader's IP settings"),
        ("destination", "show or set the UDP destination"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument(
            "--set", action="store_true", help="write new settings (needs --confirm)"
        )
        p.add_argument("--ip")
        if name == "network":
            p.add_argument("--mask", default="255.255.255.0")
            p.add_argument("--gateway")
            p.add_argument("--tcp-port", type=_port, default=c.DEFAULT_TCP_PORT)
        else:
            p.add_argument("--dest-port", type=_port, default=8888)
        p.add_argument("--confirm", action="store_true", help="really change it")
        p.add_argument(
            "--allow-unverified",
            action="store_true",
            help="allow layouts not taken from the SDK",
        )

    p = sub.add_parser(
        "factory-reset", help="restore factory settings (needs --confirm)"
    )
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--allow-unverified", action="store_true")

    p = sub.add_parser(
        "provision",
        help="configure region, antennas, power, auto mode, UDP destination, then IP",
    )
    p.add_argument("--new-ip")
    p.add_argument("--mask")
    p.add_argument("--gateway")
    p.add_argument("--new-port", type=_port, default=c.DEFAULT_TCP_PORT)
    p.add_argument("--udp-dest", type=_host_port, metavar="IP:PORT")
    p.add_argument("--region", type=_enum_arg(FrequencyRegion))
    p.add_argument("--antennas", type=_antenna_list)
    p.add_argument("--power", type=int, metavar="DBM")
    p.add_argument("--mode", type=_enum_arg(WorkMode), default=WorkMode.AUTO)
    p.add_argument(
        "--dry-run", action="store_true", help="print the frames; send nothing"
    )
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--allow-unverified", action="store_true")

    p = sub.add_parser(
        "discover",
        help="listen for reader announcements on UDP 1111",
        description=FIREWALL_NOTE,
    )
    p.add_argument("--seconds", type=float, default=5.0)

    p = sub.add_parser("decode", help="decode frame hex (offline)")
    p.add_argument("hex", nargs="+")

    p = sub.add_parser("encode", help="build a frame (offline)")
    p.add_argument("cmd", help="command name (e.g. get_version) or byte (e.g. 0x02)")
    p.add_argument("data", nargs="*", help="data bytes as hex")
    return parser  # fmt: skip


def _hex_arg(parts: Sequence[str]) -> bytes:
    text = "".join(parts).replace(" ", "").replace(":", "").removeprefix("0x")
    try:
        return bytes.fromhex(text)
    except ValueError:
        raise UsageError(f"not hex: {' '.join(parts)}") from None


def _tag_line(tag: TagRead) -> str:
    rssi = f"{tag.rssi_dbm:.1f} dBm" if tag.rssi_dbm is not None else "n/a"
    extra = f" TID={tag.tid}" if tag.tid else ""
    extra += f" USER={tag.user}" if tag.user else ""
    return f"{tag.epc}  ant{tag.antenna}  {rssi}{extra}"


def _power_line(powers: list[AntennaPower]) -> str:
    return ", ".join(f"ant{p.antenna}={p.power:g} dBm" for p in powers)


def _cmd_offline(args: argparse.Namespace, out: Output) -> int:
    if args.command == "encode":
        info = c.COMMANDS.get(args.cmd)
        if info is not None:
            cmd = info.cmd
            prefix = bytes((info.sub,)) if info.sub is not None else b""
        else:
            try:
                cmd = int(args.cmd, 0)
            except ValueError:
                raise UsageError(f"unknown command {args.cmd!r}") from None
            prefix = b""
        raw_frame = encode(cmd, prefix + _hex_arg(args.data) if args.data else prefix)
        out.emit(raw_frame.hex(" ").upper(), {"frame": raw_frame.hex().upper()})
        return EXIT_OK
    raw = _hex_arg(args.hex)
    decoder = FrameDecoder()
    frames = decoder.feed(raw)
    if not frames:
        out.emit(
            f"no valid frame ({decoder.bad_frames} rejected)",
            {"frames": [], "bad_frames": decoder.bad_frames},
        )
        return EXIT_PROTOCOL
    names = {
        i.reply_cmd: i.name
        for i in c.COMMANDS.values()
        if i.reply_cmd is not None and i.sub is None
    }
    names |= {i.cmd: i.name for i in c.COMMANDS.values() if i.sub is None}
    for frame in frames:
        label = (
            "tag_report"
            if frame.cmd == c.CMD_TAG_REPORT
            else names.get(frame.cmd, "config" if frame.cmd in (0xA1, 0xA2) else "?")
        )
        record: dict[str, Any] = {
            "cmd": f"0x{frame.cmd:02X}",
            "name": label,
            "data": frame.data.hex().upper(),
        }
        text = f"{frame}  ({label})"
        if frame.cmd == c.CMD_TAG_REPORT:
            try:
                tag = parse_tag_record(frame.data, received_at=0.0)
                record["tag"] = _jsonable(tag)
                text += "\n  " + _tag_line(tag)
            except FrameError as err:
                record["tag_error"] = str(err)
                text += f"\n  bad tag record: {err}"
        out.emit(text, record)
    if decoder.bad_frames:
        out.emit(
            f"{decoder.bad_frames} candidate frame(s) rejected",
            {"bad_frames": decoder.bad_frames},
        )
    return EXIT_OK  # fmt: skip


async def _cmd_listen(args: argparse.Namespace, out: Output) -> int:
    sys.stderr.write(f"Listening on UDP {args.bind}:{args.udp_port}. {FIREWALL_NOTE}\n")
    listener = await UR4UdpListener.start(
        (args.bind, args.udp_port), allowed_sources=args.source
    )
    try:
        async with asyncio.timeout(args.seconds):
            async for tag in listener:
                out.emit(_tag_line(tag), tag)
    except TimeoutError:
        pass
    finally:
        await listener.stop()
    stats = listener.stats
    sys.stderr.write(
        f"{stats.datagrams} datagram(s), {stats.tags} tag(s), {stats.bad_frames} bad frame(s), "
        f"{stats.dropped_sources} from other sources\n"
    )
    return EXIT_OK


async def _cmd_reader(args: argparse.Namespace, out: Output) -> int:
    cmd = args.command
    if cmd == "provision":
        result = await provision(
            args.host,
            port=args.port,
            new_ip=args.new_ip,
            mask=args.mask,
            gateway=args.gateway,
            new_port=args.new_port,
            udp_dest=args.udp_dest,
            region=args.region,
            antennas=args.antennas,
            power=args.power,
            work_mode=args.mode,
            confirm=args.confirm,
            allow_unverified=args.allow_unverified,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
        for step in result.steps:
            out.emit(
                f"[{step.outcome:>7}] {step.description}\n          {step.frame_hex}"
                + (f"  ({step.detail})" if step.detail else ""),
                {
                    "step": step.name,
                    "outcome": step.outcome,
                    "description": step.description,
                    "frame": step.frame_hex,
                    "detail": step.detail,
                },
            )
        out.emit(
            result.message,
            {"message": result.message, "network_changed": result.network_changed},
        )
        return EXIT_OK

    # Refuse guarded commands before touching the network, so a missing
    # --confirm always exits 5, even when the reader is unreachable.
    guarded = {"network": "set_network_config", "destination": "set_udp_destination"}
    if cmd == "network" and args.set and (not args.ip or not args.gateway):
        raise UsageError("network --set needs --ip and --gateway")
    if cmd == "destination" and args.set and not args.ip:
        raise UsageError("destination --set needs --ip")
    if cmd == "factory-reset" or (cmd in guarded and args.set):
        check_guard(
            guarded.get(cmd, "factory_reset"),
            confirm=args.confirm,
            allow_unverified=args.allow_unverified,
        )
    async with UR4Client(args.host, args.port, args.timeout) as reader:
        match cmd:
            case "version":
                version = await reader.get_version()
                out.emit(version, {"version": version})
            case "info":
                data: dict[str, Any] = {}
                info = await reader.get_info()
                data["version"] = info.version
                data["temperature_c"] = info.temperature
                for key, call in (
                    ("work_mode", reader.get_work_mode),
                    ("network", reader.get_network_config),
                    ("udp_destination", reader.get_udp_destination),
                    ("antennas", reader.get_antennas),
                    ("power", reader.get_power_all),
                    ("region", reader.get_region),
                    ("beep", reader.get_beep),
                ):
                    try:
                        data[key] = _jsonable(await call())
                    except (ReaderError, ProtocolError, ReaderTimeout) as err:
                        data[key] = f"error: {err}"
                human = "\n".join(f"{k:16} {v}" for k, v in data.items())
                out.emit(human, data)
            case "mode":
                if args.set is not None:
                    await reader.set_work_mode(args.set)
                mode = await reader.get_work_mode()
                out.emit(mode.name.lower(), {"work_mode": mode})
            case "inventory":
                if args.stop_first:
                    await reader.stop_inventory()
                seen: set[str] = set()
                async with reader.inventory(duration=args.seconds) as sweep:
                    async for tag in sweep:
                        if args.unique and tag.epc in seen:
                            continue
                        seen.add(tag.epc)
                        out.emit(_tag_line(tag), tag)
                if sweep.result is not None:
                    sys.stderr.write(_sweep_line(sweep.result) + "\n")
            case "ping":
                for _ in range(max(1, args.count)):
                    latency = await reader.ping()
                    out.emit(f"{latency * 1000:.1f} ms", {"latency_s": latency})
            case "sweep":
                await _cmd_sweeps(reader, args, out)
            case "idle":
                await _cmd_idle(reader, args, out)
            case "antennas":
                if args.set is not None:
                    await reader.set_antennas(args.set)
                state = await reader.get_antennas()
                out.emit(
                    ",".join(map(str, sorted(state.enabled))) or "none",
                    {"antennas": state.enabled},
                )
            case "power":
                if args.set is not None:
                    ants = args.antenna or [1, 2, 3, 4]
                    if len(ants) == 1:
                        await reader.set_power(ants[0], args.set)
                    else:
                        await reader.set_power_all(dict.fromkeys(ants, args.set))
                powers = await reader.get_power_all()
                out.emit(_power_line(powers), {"power": powers})
            case "region":
                if args.set is not None:
                    await reader.set_region(args.set)
                region = await reader.get_region()
                name = (
                    region.name.lower()
                    if isinstance(region, FrequencyRegion)
                    else f"0x{region:02X}"
                )
                out.emit(name, {"region": name})
            case "gpi":
                gpi = await reader.get_gpi()
                out.emit(
                    f"input1={'high' if gpi.input1 else 'low'} input2={'high' if gpi.input2 else 'low'}",
                    gpi,
                )
            case "gpo":
                state_out = GpoState(
                    output1=args.output1, output2=args.output2, relay=args.relay
                )
                await reader.set_gpo(state_out)
                out.emit("ok", state_out)
            case "beep":
                if args.set is not None:
                    await reader.set_beep(args.set)
                beep = await reader.get_beep()
                out.emit("on" if beep else "off", {"beep": beep})
            case "trigger":
                if args.set:
                    await reader.set_trigger_config(
                        TriggerConfig(
                            args.gpi, args.work_time, args.interval, args.report
                        )
                    )
                trig = await reader.get_trigger_config()
                out.emit(
                    f"gpi={trig.gpi} work_time={trig.work_time}x10ms interval={trig.interval}x10ms report={trig.report.name.lower()}",
                    trig,
                )
            case "network":
                if args.set:
                    if not args.ip or not args.gateway:
                        raise UsageError("network --set needs --ip and --gateway")
                    new = ReaderNetworkConfig(
                        args.ip, args.mask, args.gateway, args.tcp_port
                    )
                    change = await reader.set_network_config(
                        new,
                        confirm=args.confirm,
                        allow_unverified=args.allow_unverified,
                    )
                    out.emit(change.message, change)
                    return EXIT_OK
                net = await reader.get_network_config()
                out.emit(f"{net.ip}/{net.mask} gw {net.gateway} port {net.port}", net)
            case "destination":
                if args.set:
                    if not args.ip:
                        raise UsageError("destination --set needs --ip")
                    await reader.set_udp_destination(
                        UdpDestination(args.ip, args.dest_port),
                        confirm=args.confirm,
                        allow_unverified=args.allow_unverified,
                    )
                dest = await reader.get_udp_destination()
                out.emit(
                    f"{dest.ip}:{dest.port}" if dest else "not set",
                    dest or {"udp_destination": None},
                )
            case "factory-reset":
                await reader.factory_reset(
                    confirm=args.confirm, allow_unverified=args.allow_unverified
                )
                out.emit(
                    "factory reset acknowledged; the reader may return at 192.168.99.202",
                    {"factory_reset": True},
                )
            case _:  # pragma: no cover - argparse enforces the choices
                raise UsageError(f"unknown command {cmd}")
    return EXIT_OK  # fmt: skip


def _sweep_line(result: SweepResult) -> str:
    ants = " ".join(f"ant{a}={n}" for a, n in result.per_antenna_counts.items())
    elapsed = f"{result.elapsed:.1f}s" if result.elapsed is not None else "-"
    ack = "acked" if result.stop_acknowledged else "no-ack"
    return (
        f"{result.end_reason}: {result.unique_count} unique EPC(s), "
        f"{result.reports} report(s) in {elapsed} [{ants or 'no reads'}] stop {ack}"
    )


async def _cmd_sweeps(reader: UR4Client, args: argparse.Namespace, out: Output) -> None:
    """Run ``--repeat`` sweeps of ``--seconds`` and print unique-EPC variance."""
    counts: list[int] = []
    all_epcs: set[str] = set()
    every: set[str] | None = None
    for n in range(1, max(1, args.repeat) + 1):
        async with reader.inventory(duration=args.seconds) as sweep:
            async for _ in sweep:
                pass
        result = sweep.result
        if result is None:  # pragma: no cover - always set after the block
            continue
        counts.append(result.unique_count)
        all_epcs |= result.unique_epcs
        every = set(result.unique_epcs) if every is None else every & result.unique_epcs
        out.emit(f"sweep {n}: {_sweep_line(result)}", {"sweep": n, "result": result})
        if n < args.repeat:
            await asyncio.sleep(args.pause)
    mean = statistics.fmean(counts)
    var = statistics.pvariance(counts) if len(counts) > 1 else 0.0
    summary = {
        "sweeps": len(counts),
        "unique_min": min(counts),
        "unique_max": max(counts),
        "unique_mean": round(mean, 2),
        "unique_variance": round(var, 3),
        "epcs_seen_any": len(all_epcs),
        "epcs_seen_every": len(every or ()),
    }
    out.emit(
        "summary: " + ", ".join(f"{k}={v}" for k, v in summary.items()),
        {"summary": summary},
    )


async def _cmd_idle(reader: UR4Client, args: argparse.Namespace, out: Output) -> None:
    """Stay idle for ``--minutes``, then ping; report if/when the link dropped."""
    started = time.monotonic()
    dropped_after: list[float] = []
    dropped = asyncio.Event()

    def on_drop(_: BaseException | None) -> None:
        dropped_after.append(time.monotonic() - started)
        dropped.set()

    reader.on_disconnect = on_drop
    sys.stderr.write(f"Idle for {args.minutes:g} min; no traffic will be sent...\n")
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(dropped.wait(), args.minutes * 60)
    if dropped_after:
        out.emit(
            f"dropped by the reader after {dropped_after[0]:.0f} s idle",
            {"survived": False, "dropped_after_s": dropped_after[0]},
        )
        return
    latency = await reader.ping()
    out.emit(
        f"survived {args.minutes:g} min idle; ping {latency * 1000:.1f} ms",
        {"survived": True, "idle_s": args.minutes * 60, "latency_s": latency},
    )


def _configure_logging(debug: bool) -> None:
    logger = logging.getLogger("aiochainway")
    if debug:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


async def async_main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None
) -> int:
    """Run the CLI inside a running event loop; return the exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help/--version exit 0, errors exit 2
        return int(exc.code or 0)
    out = Output(stdout or sys.stdout, args.json)
    _configure_logging(args.debug)
    started = time.monotonic()
    try:
        if args.command in ("encode", "decode"):
            return _cmd_offline(args, out)
        if args.command == "listen":
            return await _cmd_listen(args, out)
        if args.command == "discover":
            readers = await discover(args.seconds)
            if not readers:
                out.emit("no readers heard", {"readers": []})
            for r in readers:
                out.emit(f"{r.ip}:{r.port}  {r.mac}", r)
            return EXIT_OK
        return await _cmd_reader(args, out)
    except (UsageError, ValidationError) as err:
        sys.stderr.write(f"error: {err}\n")
        return EXIT_USAGE
    except UnsafeOperationError as err:
        sys.stderr.write(f"refused: {err}\n")
        return EXIT_UNSAFE
    except (ReaderTimeout, ConnectionLostError) as err:
        sys.stderr.write(f"error: {err}\n")
        return EXIT_TIMEOUT
    except (ProtocolError, ReaderError, UnsupportedCommandError) as err:
        sys.stderr.write(f"error: {err}\n")
        return EXIT_PROTOCOL
    except ChainwayError as err:  # pragma: no cover - every subclass is mapped above
        sys.stderr.write(f"error: {err}\n")
        return EXIT_PROTOCOL
    except OSError as err:
        sys.stderr.write(f"error: {err}\n")
        return EXIT_TIMEOUT
    finally:
        logging.getLogger("aiochainway").debug(
            "Finished in %.2fs", time.monotonic() - started
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(async_main(argv))
    return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
