"""In-process fake UR4 for tests, with fault injection.

Import it from the installed, pinned version so a downstream project (such as
a Home Assistant integration) always tests against a fake that speaks the same
protocol as the client it ships with::

    from aiochainway import UR4Client
    from aiochainway.testing import FakeUR4, tag_record

    async with FakeUR4() as fake:
        fake.tags = [tag_record("E2801170000002AA", antenna=2)]
        async with UR4Client(fake.host, fake.port) as reader:
            assert await reader.get_version() == "V1.2.3"
            fake.faults.drop = {"get_region"}  # next get_region times out

The fake's replies are written out by hand from ``docs/protocol.md`` rather
than produced with the library's own builders, so a disagreement between the
client and the documented protocol shows up as a test failure. The test suite
also checks that the fake answers every command in
:data:`aiochainway.protocol.commands.COMMANDS` that the UR4 supports.

It needs only the standard library and opens sockets on ``127.0.0.1`` only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import ipaddress
import random
import time
from types import TracebackType
from typing import Self

from .protocol.commands import CMD_TAG_REPORT
from .protocol.errors import ValidationError
from .protocol.frame import Frame, FrameDecoder, encode

__all__ = [
    "NAMES",
    "FakeUR4",
    "Faults",
    "ReaderState",
    "ScriptedTag",
    "tag_frames",
    "tag_record",
]


def tag_record(
    epc: str,
    *,
    rssi_dbm: float | None = -60.0,
    antenna: int = 1,
    tid: str | None = None,
    user: str | None = None,
) -> bytes:
    """Build a ``0x83`` tag-report payload: PC | EPC | [TID | [USER]] | RSSI | ANT.

    ``antenna`` is not range-checked, so tests can build invalid records (e.g.
    antenna 9). ``rssi_dbm=None`` encodes a value the parser reports as ``None``.

    Raises:
        ValidationError: ``epc``/``tid``/``user`` are not hex, the EPC is not a
            whole number of 16-bit words up to 62 bytes, or ``antenna`` is not
            a byte.

    """
    try:
        epc_b = bytes.fromhex(epc)
        tid_b = bytes.fromhex(tid) if tid is not None else None
        user_b = bytes.fromhex(user) if user is not None else None
    except ValueError as err:
        raise ValidationError(f"not hex: {err}") from err
    if len(epc_b) % 2 or len(epc_b) > 62:
        raise ValidationError("EPC must be an even number of bytes, at most 62")
    if not 0 <= antenna <= 0xFF:
        raise ValidationError(f"antenna must fit in a byte, got {antenna}")
    pc = bytes(((len(epc_b) // 2) << 3, 0x00))
    raw = 0x0000 if rssi_dbm is None else 0xFFFF - round(-rssi_dbm * 10)
    body = pc + epc_b
    if tid_b is not None:
        body += tid_b
        if user_b is not None:
            body += user_b
    return body + raw.to_bytes(2, "big") + bytes((antenna,))


def tag_frames(*records: bytes) -> bytes:
    """Return complete ``0x83`` frames for ``records``, back to back.

    Send the result as one UDP datagram to exercise a listener the way a
    reader in auto mode does.
    """
    return b"".join(encode(CMD_TAG_REPORT, record) for record in records)


@dataclass(frozen=True)
class ScriptedTag:
    """A tag in a scripted scenario (:attr:`FakeUR4.scenario`).

    Times are seconds on the fake's scenario clock (:meth:`FakeUR4.elapsed`,
    zero when the fake starts or :meth:`FakeUR4.reset_clock` is called), so a
    scenario can span several sweeps: a tag with ``present_until=10`` is seen by
    a sweep at t=0 and missing from a sweep at t=20.

    In every read round (each ``tag_interval`` during an inventory) a present
    tag is reported with probability ``read_probability`` (below 1 simulates
    edge-of-field flicker), on each of its ``antennas`` that is enabled in
    :attr:`ReaderState.antenna_mask`.
    """

    epc: str
    antennas: tuple[int, ...] = (1,)
    present_from: float = 0.0
    present_until: float | None = None
    read_probability: float = 1.0
    rssi_dbm: float | None = -60.0
    tid: str | None = None
    user: str | None = None

    def __post_init__(self) -> None:
        """Validate fields."""
        if not 0.0 <= self.read_probability <= 1.0:
            raise ValidationError("read_probability must be within 0..1")
        if not self.antennas or any(not 1 <= a <= 16 for a in self.antennas):
            raise ValidationError("antennas must be a non-empty tuple of 1..16")
        if self.present_until is not None and self.present_until < self.present_from:
            raise ValidationError("present_until must not be before present_from")
        tag_record(self.epc, tid=self.tid, user=self.user)  # validates hex/length

    def present(self, t: float) -> bool:
        """Whether the tag is in the field at scenario time ``t``."""
        return t >= self.present_from and (
            self.present_until is None or t < self.present_until
        )

    def record(self, antenna: int) -> bytes:
        """Return the ``0x83`` payload for a read on ``antenna``."""
        return tag_record(
            self.epc,
            rssi_dbm=self.rssi_dbm,
            antenna=antenna,
            tid=self.tid,
            user=self.user,
        )


@dataclass
class Faults:
    """Fault-injection switches. All default to "behave"."""

    delay: float = 0.0
    #: Command names (see NAMES) that never get a reply.
    drop: set[str] = field(default_factory=set)
    #: Drop the next N replies (any command).
    drop_next: int = 0
    #: Write each reply one byte at a time.
    split_bytes: bool = False
    #: Write replies in chunks of this many bytes, with a pause in between.
    chunk: int | None = None
    #: Corrupt the XOR byte of the next N replies.
    corrupt_xor_next: int = 0
    #: Send every reply twice.
    duplicate: bool = False
    #: Send this many unsolicited 0x83 frames before each reply.
    unsolicited_tags: int = 0
    #: Close the connection after this many inventory tag frames.
    disconnect_after_tags: int | None = None
    #: Command names answered with a failure status.
    fail: set[str] = field(default_factory=set)
    #: Never acknowledge stop (0x8D).
    no_stop_ack: bool = False
    #: Close the connection when this command name arrives.
    disconnect_on: set[str] = field(default_factory=set)
    #: Send a reply with the wrong sub-command echo for these names.
    wrong_sub: set[str] = field(default_factory=set)
    #: Close the connection this many seconds after an inventory starts.
    disconnect_after_s: float | None = None
    #: Seconds to wait before acknowledging stop (0x8D).
    stop_ack_delay: float = 0.0
    #: Close a connection that received nothing for this many seconds while no
    #: inventory is running (some readers drop idle TCP sessions; unknown for
    #: the UR4, see docs/hardware-validation.md).
    idle_timeout_s: float | None = None


NAMES: dict[tuple[int, int | None], str] = {
    (0x02, None): "get_version",
    (0x34, None): "get_temperature",
    (0x82, None): "start_inventory",
    (0x8C, None): "stop_inventory",
    (0xA1, 0x01): "set_network_config",
    (0xA1, 0x02): "get_network_config",
    (0xA1, 0x03): "set_udp_destination",
    (0xA1, 0x04): "get_udp_destination",
    (0xA1, 0x05): "set_work_mode",
    (0xA1, 0x06): "get_work_mode",
    (0xA1, 0x07): "set_beep",
    (0xA1, 0x08): "get_beep",
    (0xA1, 0x09): "set_gpo",
    (0xA1, 0x0A): "get_gpi",
    (0xA1, 0x0B): "set_trigger_config",
    (0xA1, 0x0C): "get_trigger_config",
    (0x28, None): "set_antennas",
    (0x2A, None): "get_antennas",
    (0x10, None): "set_power",
    (0x12, None): "get_power",
    (0x2C, None): "set_region",
    (0x2E, None): "get_region",
    (0x52, None): "set_rf_link",
    (0x54, None): "get_rf_link",
    (0x20, None): "set_gen2",
    (0x22, None): "get_gen2",
    (0x70, None): "set_inventory_mode",
    (0x72, None): "get_inventory_mode",
    (0x4A, None): "set_antenna_work_time",
    (0x4C, None): "get_antenna_work_time",
    (0x74, None): "factory_reset",
}


@dataclass
class ReaderState:
    """Settings the fake remembers."""

    version: tuple[int, int, int] = (1, 2, 3)
    temperature_raw: int = 2650  # 26.50 °C
    work_mode: int = 0
    ip: str = "192.168.99.202"
    port: int = 8888
    mask: str = "255.255.255.0"
    gateway: str = "192.168.99.1"
    dest_ip: str = "192.168.99.100"
    dest_port: int = 8888
    antenna_mask: int = 0x0F
    power: dict[int, int] = field(default_factory=lambda: {1: 30, 2: 30, 3: 30, 4: 30})
    region: int = 0x08
    gpi: tuple[int, int] = (0, 1)
    beep: int = 1
    trigger: bytes = bytes((0x00, 0x00, 0x64, 0x00, 0x00, 0x01))
    rf_link: int = 1
    gen2: bytes = bytes((0x01, 0x40, 0xF1, 0x00))
    inventory_mode: tuple[int, int, int] = (0, 0, 0)
    work_time: dict[int, int] = field(
        default_factory=lambda: {1: 200, 2: 200, 3: 200, 4: 200}
    )


class FakeUR4:
    """TCP fake of the UR4 command port.

    Attributes you may change at any time: ``state`` (the settings it reports),
    ``faults`` (fault injection), ``tags`` (records sent during an inventory,
    cycled), ``scenario`` (scripted tags; when set it replaces ``tags``) and
    ``tag_interval`` (seconds between tag frames, or between read rounds of a
    scenario). ``received`` holds every frame received; :meth:`names` gives
    their command names. ``rng`` (seeded) decides scenario read probabilities,
    so runs are repeatable; ``clock`` drives the scenario clock.
    """

    def __init__(
        self, *, seed: int = 0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Create a stopped fake; call :meth:`start` or use ``async with``."""
        self.state = ReaderState()
        self.faults = Faults()
        self.tags: list[bytes] = [tag_record("E28011700000020A1B2C3D4E")]
        self.tag_interval = 0.005
        self.scenario: list[ScriptedTag] | None = None
        self.rng = random.Random(seed)
        self.clock = clock
        self._t0 = clock()
        #: Inventories started, and tag reports sent, over the fake's lifetime.
        self.inventories = 0
        self.reports_sent = 0
        #: Connections closed by ``Faults.idle_timeout_s``.
        self.idle_drops = 0
        self.received: list[Frame] = []
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.host = "127.0.0.1"
        self.port = 0
        # Replaced (after being set) on every new connection or frame.
        self._activity = asyncio.Event()

    # Lifecycle -----------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Listen on ``host:port`` (port 0 picks a free one; see ``self.port``)."""
        self._server = await asyncio.start_server(self._handle, host, port)
        self.reset_clock()
        self.host = host
        self.port = self._server.sockets[0].getsockname()[1]

    async def __aenter__(self) -> Self:
        """Start on a free port on 127.0.0.1."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop."""
        await self.stop()

    async def stop(self) -> None:
        """Close every connection and stop listening. Safe to call twice."""
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    async def drop_connections(self, timeout: float = 3.0) -> None:
        """Close every client connection.

        A client's connect() returns once the kernel finishes the handshake,
        which can be before this server's accept handler runs (seen on macOS),
        so wait for at least one registered connection first.
        """
        async with asyncio.timeout(timeout):
            while not self._writers:
                await self._activity.wait()
        for writer in list(self._writers):
            writer.close()
        await asyncio.sleep(0.01)

    def reset_clock(self) -> None:
        """Restart the scenario clock at zero."""
        self._t0 = self.clock()

    def elapsed(self) -> float:
        """Seconds on the scenario clock."""
        return self.clock() - self._t0

    def _notify(self) -> None:
        self._activity.set()
        self._activity = asyncio.Event()

    def names(self) -> list[str]:
        """Names of every command received, in order."""
        return [
            NAMES.get((f.cmd, f.sub if f.cmd == 0xA1 else None), f"0x{f.cmd:02X}")
            for f in self.received
        ]

    async def wait_for(self, name: str, timeout: float = 1.0) -> None:
        """Wait until a command called ``name`` has been received."""
        async with asyncio.timeout(timeout):
            while name not in self.names():
                await self._activity.wait()

    # Connection handling -------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.add(writer)
        self._notify()
        decoder = FrameDecoder()
        conn = _Conn(self, writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        watchdog = asyncio.create_task(conn.idle_watchdog())
        self._tasks.add(watchdog)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                conn.last_rx = time.monotonic()
                for frame in decoder.feed(data):
                    self.received.append(frame)
                    self._notify()
                    await conn.on_frame(frame)
                    if writer.is_closing():
                        return
        except ConnectionError:
            pass  # the client went away; clean up below
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            self._tasks.discard(watchdog)
            await conn.stop_inventory_task()
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()


class _Conn:
    def __init__(self, fake: FakeUR4, writer: asyncio.StreamWriter) -> None:
        self.fake = fake
        self.writer = writer
        self.inventory_task: asyncio.Task[None] | None = None
        self.tags_sent = 0
        self.last_rx = time.monotonic()

    async def idle_watchdog(self) -> None:
        """Close the connection after ``Faults.idle_timeout_s`` without traffic."""
        while not self.writer.is_closing():
            limit = self.fake.faults.idle_timeout_s
            if limit is None:
                await asyncio.sleep(0.05)
                continue
            if self.inventory_task is not None:
                self.last_rx = time.monotonic()  # an inventory is traffic
            idle = time.monotonic() - self.last_rx
            if idle >= limit:
                self.fake.idle_drops += 1
                self.writer.close()
                return
            await asyncio.sleep(min(limit - idle, 0.05))

    async def stop_inventory_task(self) -> None:
        task, self.inventory_task = self.inventory_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def write_frame(self, raw: bytes) -> None:
        f = self.fake.faults
        if self.writer.is_closing():
            return
        if f.split_bytes:
            for i in range(len(raw)):
                self.writer.write(raw[i : i + 1])
                await self.writer.drain()
                await asyncio.sleep(0)
        elif f.chunk:
            for i in range(0, len(raw), f.chunk):
                self.writer.write(raw[i : i + f.chunk])
                await self.writer.drain()
                await asyncio.sleep(0.001)
        else:
            self.writer.write(raw)
            await self.writer.drain()

    async def reply(self, name: str, cmd: int, data: bytes) -> None:
        f = self.fake.faults
        if name in f.disconnect_on:
            self.writer.close()
            return
        for _ in range(f.unsolicited_tags):
            await self.write_frame(encode(0x83, self.fake.tags[0]))
        if name in f.drop:
            return
        if f.drop_next > 0:
            f.drop_next -= 1
            return
        if f.delay:
            await asyncio.sleep(f.delay)
        if name in f.wrong_sub and data:
            data = bytes(((data[0] + 1) & 0xFF,)) + data[1:]
        raw = encode(cmd, data)
        if f.corrupt_xor_next > 0:
            f.corrupt_xor_next -= 1
            raw = raw[:-3] + bytes((raw[-3] ^ 0xFF,)) + raw[-2:]
        await self.write_frame(raw)
        if f.duplicate:
            await self.write_frame(raw)

    def status(self, name: str, ok: int = 0x01) -> bytes:
        return (
            bytes((0x00 if ok == 0x01 else 0x01,))
            if name in self.fake.faults.fail
            else bytes((ok,))
        )

    async def on_frame(self, frame: Frame) -> None:
        s = self.fake.state
        sub = frame.sub if frame.cmd == 0xA1 else None
        name = NAMES.get((frame.cmd, sub), "?")
        d = frame.data
        reply_cmd = frame.cmd + 1
        ok = self.status(name)
        match (frame.cmd, sub):
            case (0x02, None):
                await self.reply(name, 0x03, bytes(s.version))
            case (0x34, None):
                await self.reply(name, 0x35, ok + s.temperature_raw.to_bytes(2, "big"))
            case (0x82, None):
                if self.inventory_task is None:
                    self.fake.inventories += 1
                    self.inventory_task = asyncio.create_task(self._emit_tags())
            case (0x8C, None):
                await self.stop_inventory_task()
                self.last_rx = time.monotonic()
                if not self.fake.faults.no_stop_ack:
                    if self.fake.faults.stop_ack_delay:
                        await asyncio.sleep(self.fake.faults.stop_ack_delay)
                    await self.reply(name, 0x8D, ok)
            case (0xA1, 0x01):
                s.ip = str(ipaddress.IPv4Address(d[1:5]))
                s.port = int.from_bytes(d[5:7], "big")
                s.mask = str(ipaddress.IPv4Address(d[7:11]))
                s.gateway = str(ipaddress.IPv4Address(d[11:15]))
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x02):
                payload = (
                    b"\x02"
                    + ipaddress.IPv4Address(s.ip).packed
                    + s.port.to_bytes(2, "big")
                    + ipaddress.IPv4Address(s.mask).packed
                    + ipaddress.IPv4Address(s.gateway).packed
                )
                await self.reply(name, 0xA2, payload)
            case (0xA1, 0x03):
                s.dest_ip = str(ipaddress.IPv4Address(d[1:5]))
                s.dest_port = int.from_bytes(d[5:7], "big")
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x04):
                payload = (
                    b"\x04"
                    + ipaddress.IPv4Address(s.dest_ip).packed
                    + s.dest_port.to_bytes(2, "big")
                )
                await self.reply(name, 0xA2, payload)
            case (0xA1, 0x05):
                s.work_mode = d[1]
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x06):
                await self.reply(name, 0xA2, bytes((0x06, s.work_mode)))
            case (0xA1, 0x07):
                s.beep = d[1]
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x08):
                await self.reply(name, 0xA2, bytes((0x08, s.beep)))
            case (0xA1, 0x09):
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x0A):
                await self.reply(name, 0xA2, bytes((0x0A, *s.gpi)))
            case (0xA1, 0x0B):
                s.trigger = bytes(d[1:7])
                await self.reply(name, 0xA2, ok)
            case (0xA1, 0x0C):
                await self.reply(name, 0xA2, b"\x0c" + s.trigger)
            case (0x28, None):
                s.antenna_mask = (d[1] << 8) | d[2]
                await self.reply(name, 0x29, ok)
            case (0x2A, None):
                await self.reply(name, 0x2B, s.antenna_mask.to_bytes(2, "big"))
            case (0x10, None):
                for i in range(1, len(d) - 4, 5):
                    s.power[d[i]] = int.from_bytes(d[i + 1 : i + 3], "big") // 100
                await self.reply(name, 0x11, ok)
            case (0x12, None):
                payload = self.status(name, ok=0x00)
                for ant, dbm in sorted(s.power.items()):
                    payload += bytes((ant,)) + (dbm * 100).to_bytes(2, "big") * 2
                await self.reply(name, 0x13, payload)
            case (0x2C, None):
                s.region = d[1]
                await self.reply(name, 0x2D, ok)
            case (0x2E, None):
                await self.reply(name, 0x2F, ok + bytes((s.region,)))
            case (0x52, None):
                s.rf_link = d[2]
                await self.reply(name, 0x53, ok)
            case (0x54, None):
                await self.reply(name, 0x55, ok + bytes((0x01, s.rf_link)))
            case (0x20, None):
                s.gen2 = bytes(d[:4])
                await self.reply(name, 0x21, ok)
            case (0x22, None):
                await self.reply(name, 0x23, s.gen2)
            case (0x70, None):
                s.inventory_mode = (d[1], d[2], d[3])
                await self.reply(name, 0x71, ok)
            case (0x72, None):
                await self.reply(name, 0x73, ok + bytes(s.inventory_mode))
            case (0x4A, None):
                s.work_time[d[0]] = int.from_bytes(d[1:3], "big")
                await self.reply(name, 0x4B, ok)
            case (0x4C, None):
                await self.reply(
                    name,
                    0x4D,
                    ok + bytes((d[0],)) + s.work_time[d[0]].to_bytes(2, "big"),
                )
            case (0x74, None):
                await self.reply(name, 0x75, ok)
            case _:
                await self.reply("?", reply_cmd, b"\x00")

    async def _emit_tags(self) -> None:
        f = self.fake.faults
        started = time.monotonic()
        i = 0
        while True:
            scenario = self.fake.scenario
            if scenario is not None:
                records = self._scenario_round(scenario)
            else:
                records = [self.fake.tags[i % len(self.fake.tags)]]
            i += 1
            for record in records:
                await self.write_frame(encode(0x83, record))
                self.tags_sent += 1
                self.fake.reports_sent += 1
                if (
                    f.disconnect_after_tags is not None
                    and self.tags_sent >= f.disconnect_after_tags
                ):
                    self.writer.close()
                    return
            if (
                f.disconnect_after_s is not None
                and time.monotonic() - started >= f.disconnect_after_s
            ):
                self.writer.close()
                return
            await asyncio.sleep(self.fake.tag_interval)

    def _scenario_round(self, scenario: list[ScriptedTag]) -> list[bytes]:
        """Tag records for one read round of the scripted scenario."""
        fake = self.fake
        now = fake.elapsed()
        enabled = fake.state.antenna_mask
        out: list[bytes] = []
        for tag in scenario:
            if not tag.present(now):
                continue
            for antenna in tag.antennas:
                if not enabled & (1 << (antenna - 1)):
                    continue
                if (
                    tag.read_probability >= 1.0
                    or fake.rng.random() < tag.read_probability
                ):
                    out.append(tag.record(antenna))
        return out
