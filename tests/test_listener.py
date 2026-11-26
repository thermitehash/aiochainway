"""UR4UdpListener and discovery."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

from aiochainway import TagRead, UR4UdpListener, ValidationError, discover
from aiochainway.protocol.frame import encode

from .conftest import eventually
from .fake_reader import tag_record

EPC = "E28011700000020A1B2C3D4E"


def send(port: int, *datagrams: bytes, src_port: int = 0) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", src_port))
        for d in datagrams:
            sock.sendto(d, ("127.0.0.1", port))


async def settle(listener: UR4UdpListener, datagrams: int) -> None:
    async with asyncio.timeout(2):
        while listener.stats.datagrams < datagrams:
            await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)


async def start(**kwargs: object) -> tuple[UR4UdpListener, int]:
    listener = await UR4UdpListener.start(("127.0.0.1", 0), **kwargs)  # type: ignore[arg-type]  # test passthrough
    assert listener.local_address is not None
    return listener, listener.local_address[1]


async def test_multi_frame_datagram_and_callback() -> None:
    got: list[TagRead] = []
    listener, port = await start(callback=got.append)
    frames = encode(0x83, tag_record(EPC, antenna=1)) + encode(
        0x83, tag_record("AABB", antenna=4)
    )
    send(port, frames)
    await settle(listener, 1)
    assert [(t.epc, t.antenna) for t in got] == [(EPC, 1), ("AABB", 4)]
    assert got[0].source.startswith("udp:127.0.0.1:")
    assert (listener.stats.frames, listener.stats.tags) == (2, 2)
    await listener.stop()


async def test_frame_split_across_datagrams() -> None:
    got: list[TagRead] = []
    listener, port = await start(callback=got.append)
    raw = encode(0x83, tag_record(EPC))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.sendto(raw[:7], ("127.0.0.1", port))
        sock.sendto(raw[7:], ("127.0.0.1", port))
    await settle(listener, 2)
    assert [t.epc for t in got] == [EPC]
    await listener.stop()


async def test_bad_frames_counted() -> None:
    got: list[TagRead] = []
    listener, port = await start(callback=got.append)
    good = encode(0x83, tag_record(EPC))
    bad_xor = good[:-3] + bytes((good[-3] ^ 1,)) + good[-2:]
    bad_record = encode(0x83, tag_record(EPC, antenna=7))
    other = encode(0x03, b"\x01\x02\x03")
    send(port, bad_xor + good, bad_record, other)
    await settle(listener, 3)
    assert len(got) == 1
    stats = listener.stats
    assert stats.bad_frames == 2
    assert stats.other_frames == 1
    assert stats.frames == 3
    await listener.stop()


async def test_callback_raising_does_not_stop_listener(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def bad(tag: TagRead) -> None:
        calls.append(tag.epc)
        raise RuntimeError("boom")

    listener, port = await start(callback=bad)
    with caplog.at_level(logging.ERROR, logger="aiochainway"):
        send(port, encode(0x83, tag_record(EPC)))
        await settle(listener, 1)
        send(port, encode(0x83, tag_record("AABB")))
        await settle(listener, 2)
    assert calls == [EPC, "AABB"]
    assert listener.stats.callback_errors == 2
    assert "Tag callback raised" in caplog.text
    assert listener.running
    await listener.stop()


async def test_async_callback_and_its_errors(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[str] = []

    async def cb(tag: TagRead) -> None:
        await asyncio.sleep(0)
        seen.append(tag.epc)
        if tag.epc == "AABB":
            raise ValueError("async boom")

    listener, port = await start(callback=cb)
    with caplog.at_level(logging.ERROR, logger="aiochainway"):
        send(port, encode(0x83, tag_record(EPC)), encode(0x83, tag_record("AABB")))
        await settle(listener, 2)
        await eventually(lambda: listener.stats.callback_errors == 1)
    assert seen == [EPC, "AABB"]
    assert listener.stats.callback_errors == 1
    await listener.stop()


async def test_stop_cancels_slow_async_callbacks() -> None:
    started = asyncio.Event()

    async def slow(tag: TagRead) -> None:
        started.set()
        await asyncio.sleep(10)

    listener, port = await start(callback=slow)
    send(port, encode(0x83, tag_record(EPC)))
    await asyncio.wait_for(started.wait(), 2)
    await listener.stop()
    assert listener.stats.callback_errors == 0


async def test_source_filter() -> None:
    got: list[TagRead] = []
    listener, port = await start(callback=got.append, allowed_sources=["10.9.9.9"])
    send(port, encode(0x83, tag_record(EPC)))
    await settle(listener, 1)
    assert got == []
    assert listener.stats.dropped_sources == 1
    await listener.stop()
    listener, port = await start(callback=got.append, allowed_sources=["127.0.0.1"])
    send(port, encode(0x83, tag_record(EPC)))
    await settle(listener, 1)
    assert len(got) == 1
    await listener.stop()


def test_bad_allowed_source() -> None:
    with pytest.raises(ValidationError):
        UR4UdpListener(allowed_sources=["not-an-ip"])


async def test_stop_idempotent_and_ignores_late_datagrams() -> None:
    listener, _ = await start()
    await listener.stop()
    await listener.stop()
    assert not listener.running
    listener._on_datagram(encode(0x83, tag_record(EPC)), ("127.0.0.1", 1))
    assert listener.stats.datagrams == 0


async def test_async_iteration_and_context_manager() -> None:
    listener, port = await start()
    async with listener:
        it = aiter(listener)
        send(port, encode(0x83, tag_record(EPC)) + encode(0x83, tag_record("AABB")))
        first = await asyncio.wait_for(anext(it), 2)
        second = await asyncio.wait_for(anext(it), 2)
        assert [first.epc, second.epc] == [EPC, "AABB"]
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), 1)


async def test_iterate_after_stop_ends_immediately() -> None:
    listener, _ = await start()
    await listener.stop()
    assert [t async for t in listener] == []


async def test_queue_overflow_drops_oldest() -> None:
    listener, port = await start(queue_size=2)
    aiter(listener)
    send(port, *(encode(0x83, tag_record(f"{i:04X}")) for i in range(5)))
    await settle(listener, 5)
    assert listener.stats.queue_dropped == 3
    tags = [await anext(listener), await anext(listener)]
    assert [t.epc for t in tags] == ["0003", "0004"]
    await listener.stop()


async def test_decoder_per_source_is_bounded() -> None:
    listener = UR4UdpListener()
    listener.max_sources = 2
    raw = encode(0x83, tag_record(EPC))
    for port in (1, 2, 3, 1):
        listener._on_datagram(raw[:5], ("127.0.0.1", port))
    assert len(listener._decoders) == 2
    assert listener.local_address is None


async def test_discover() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    async def announce() -> None:
        await asyncio.sleep(0.05)
        send(
            port,
            bytes.fromhex("A1B2C3D4E5F6C0A863CA22B8"),
            b"short",
            bytes.fromhex("A1B2C3D4E5F6C0A863CA22B8"),
        )

    task = asyncio.create_task(announce())
    found = await discover(0.3, bind=("127.0.0.1", port))
    await task
    assert [(r.ip, r.port, r.mac) for r in found] == [
        ("192.168.99.202", 8888, "A1:B2:C3:D4:E5:F6")
    ]
