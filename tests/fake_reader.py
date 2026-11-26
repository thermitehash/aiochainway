"""An in-process fake UR4 for tests, with fault injection.

Replies are written out by hand from ``docs/protocol.md`` (not produced with
the library's builders), so a library/spec disagreement shows up as a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import ipaddress

from aiochainway.protocol.frame import Frame, FrameDecoder, encode


def tag_record(
    epc: str,
    *,
    rssi_dbm: float | None = -60.0,
    antenna: int = 1,
    tid: str | None = None,
    user: str | None = None,
) -> bytes:
    """Build a ``0x83`` payload: PC | EPC | [TID | [USER]] | RSSI | ANT."""
    epc_b = bytes.fromhex(epc)
    assert len(epc_b) % 2 == 0 and len(epc_b) <= 62
    pc = bytes(((len(epc_b) // 2) << 3, 0x00))
    raw = 0x0000 if rssi_dbm is None else 0xFFFF - round(-rssi_dbm * 10)
    body = pc + epc_b
    if tid is not None:
        body += bytes.fromhex(tid)
        if user is not None:
            body += bytes.fromhex(user)
    return body + raw.to_bytes(2, "big") + bytes((antenna,))


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


class FakeReader:
    """TCP fake of the UR4 command port."""

    def __init__(self) -> None:
        self.state = ReaderState()
        self.faults = Faults()
        self.tags: list[bytes] = [tag_record("E28011700000020A1B2C3D4E")]
        self.tag_interval = 0.005
        self.received: list[Frame] = []
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    # Lifecycle -----------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
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
                await asyncio.sleep(0.005)
        for writer in list(self._writers):
            writer.close()
        await asyncio.sleep(0.01)

    def names(self) -> list[str]:
        """Names of every command received, in order."""
        return [
            NAMES.get((f.cmd, f.sub if f.cmd == 0xA1 else None), f"0x{f.cmd:02X}")
            for f in self.received
        ]

    async def wait_for(self, name: str, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while name not in self.names():
                await asyncio.sleep(0.005)

    # Connection handling -------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._writers.add(writer)
        decoder = FrameDecoder()
        conn = _Conn(self, writer)
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for frame in decoder.feed(data):
                    self.received.append(frame)
                    await conn.on_frame(frame)
                    if writer.is_closing():
                        return
        except ConnectionError, asyncio.CancelledError:
            pass
        finally:
            await conn.stop_inventory_task()
            self._writers.discard(writer)
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class _Conn:
    def __init__(self, fake: FakeReader, writer: asyncio.StreamWriter) -> None:
        self.fake = fake
        self.writer = writer
        self.inventory_task: asyncio.Task[None] | None = None
        self.tags_sent = 0

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
                    self.inventory_task = asyncio.create_task(self._emit_tags())
            case (0x8C, None):
                await self.stop_inventory_task()
                if not self.fake.faults.no_stop_ack:
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
        i = 0
        while True:
            record = self.fake.tags[i % len(self.fake.tags)]
            i += 1
            await self.write_frame(encode(0x83, record))
            self.tags_sent += 1
            if (
                f.disconnect_after_tags is not None
                and self.tags_sent >= f.disconnect_after_tags
            ):
                self.writer.close()
                return
            await asyncio.sleep(self.fake.tag_interval)
