"""UDP listener for auto- and trigger-mode tag reports.

In auto/trigger mode the reader sends ``0x83`` tag frames to its configured UDP
destination. One datagram may carry several frames, and (as in the SDK, which
feeds every datagram into one stream splitter) a frame may span datagrams, so
each source address gets its own :class:`FrameDecoder`.

UDP is unauthenticated: anyone on the network can send fake tag frames. Keep the
reader on an isolated VLAN and pass ``allowed_sources``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import inspect
import ipaddress
import logging
from typing import Self

from .models import DiscoveredReader, TagRead, TagSource
from .protocol.commands import CMD_TAG_REPORT, DISCOVERY_PORT, parse_discovery
from .protocol.errors import FrameError, UnexpectedReplyError, ValidationError
from .protocol.frame import MAX_FRAME_LEN, FrameDecoder
from .protocol.tags import parse_tag_record

_LOGGER = logging.getLogger("aiochainway")

TagCallback = Callable[[TagRead], Awaitable[None] | None]
DEFAULT_QUEUE = 10_000
#: Per-source decoders kept (least recently used are evicted).
DEFAULT_MAX_SOURCES = 64
#: Async callbacks allowed to run at once; more are dropped (and counted).
MAX_PENDING_CALLBACKS = 1000


@dataclass(slots=True)
class ListenerStats:
    """Counters for a :class:`UR4UdpListener`."""

    datagrams: int = 0
    frames: int = 0
    tags: int = 0
    bad_frames: int = 0
    dropped_sources: int = 0
    other_frames: int = 0
    callback_errors: int = 0
    queue_dropped: int = 0
    #: Async callbacks not started because too many were still running.
    callbacks_dropped: int = 0


class _Stop:
    """Sentinel queued when the listener stops."""


_STOP = _Stop()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, listener: UR4UdpListener) -> None:
        self._listener = listener

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        try:
            self._listener._on_datagram(data, (str(addr[0]), int(str(addr[1]))))
        except Exception:  # boundary: one bad datagram must not reach asyncio
            self._listener.stats.bad_frames += 1
            _LOGGER.exception("Failed to handle datagram from %s", addr[0])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP error: %s", exc)


class UR4UdpListener:
    """Receive tag reports pushed by one or more readers over UDP.

    Example::

        listener = await UR4UdpListener.start(("0.0.0.0", 8888), callback=print)
        ...
        await listener.stop()

    or iterate::

        async for tag in listener:
            ...

    """

    def __init__(
        self,
        callback: TagCallback | None = None,
        allowed_sources: Iterable[str] | None = None,
        *,
        strict_tags: bool = True,
        queue_size: int = DEFAULT_QUEUE,
        max_sources: int = DEFAULT_MAX_SOURCES,
    ) -> None:
        """Configure; use :meth:`start` to bind."""
        self.callback = callback
        self.allowed_sources: frozenset[str] | None = None
        if allowed_sources is not None:
            try:
                self.allowed_sources = frozenset(
                    str(ipaddress.ip_address(ip)) for ip in allowed_sources
                )
            except ValueError as err:
                raise ValidationError(f"bad allowed source: {err}") from err
        self.strict_tags = strict_tags
        self.max_sources = max_sources
        self.stats = ListenerStats()
        self._queue_size = queue_size
        self._queue: asyncio.Queue[TagRead | _Stop] | None = None
        self._decoders: OrderedDict[tuple[str, int], FrameDecoder] = OrderedDict()
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Future[None]] = set()
        self._stopped = False

    @classmethod
    async def start(
        cls,
        bind: tuple[str, int] = ("0.0.0.0", 8888),
        callback: TagCallback | None = None,
        allowed_sources: Iterable[str] | None = None,
        *,
        strict_tags: bool = True,
        queue_size: int = DEFAULT_QUEUE,
    ) -> UR4UdpListener:
        """Bind a UDP socket and start receiving."""
        listener = cls(
            callback, allowed_sources, strict_tags=strict_tags, queue_size=queue_size
        )
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(listener), local_addr=bind
        )
        listener._transport = transport
        _LOGGER.debug("Listening for UDP tag frames on %s", listener.local_address)
        return listener

    @property
    def local_address(self) -> tuple[str, int] | None:
        """The bound ``(host, port)``, or ``None`` when not started."""
        if self._transport is None:
            return None
        host, port = self._transport.get_extra_info("sockname")[:2]
        return str(host), int(port)

    @property
    def running(self) -> bool:
        """Whether the socket is open."""
        return self._transport is not None and not self._stopped

    async def stop(self) -> None:
        """Close the socket and cancel callback tasks. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self._transport is not None:
            self._transport.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._queue is not None:
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(_STOP)

    async def __aenter__(self) -> Self:
        """Return self (already started)."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop."""
        await self.stop()

    def __aiter__(self) -> Self:
        """Start queueing tags for iteration."""
        if self._queue is None:
            self._queue = asyncio.Queue(self._queue_size)
            if self._stopped:
                self._queue.put_nowait(_STOP)
        return self

    async def __anext__(self) -> TagRead:
        """Return the next tag; stop iterating after :meth:`stop`."""
        queue = self.__aiter__()._queue
        assert queue is not None  # created by __aiter__
        item = await queue.get()
        if isinstance(item, _Stop):
            queue.put_nowait(_STOP)  # wake any other iterator too
            raise StopAsyncIteration
        return item

    # Internals ---------------------------------------------------------------

    def _decoder_for(self, addr: tuple[str, int]) -> FrameDecoder:
        decoder = self._decoders.get(addr)
        if decoder is None:
            decoder = FrameDecoder(max_frame_len=MAX_FRAME_LEN)
            self._decoders[addr] = decoder
            while len(self._decoders) > self.max_sources:
                self._decoders.popitem(last=False)
        else:
            self._decoders.move_to_end(addr)
        return decoder

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._stopped:
            return
        stats = self.stats
        stats.datagrams += 1
        if self.allowed_sources is not None and addr[0] not in self.allowed_sources:
            stats.dropped_sources += 1
            _LOGGER.debug("Dropped datagram from unlisted source %s:%s", *addr)
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("<< udp %s:%s %s", addr[0], addr[1], data.hex(" ").upper())
        decoder = self._decoder_for(addr)
        bad_before = decoder.bad_frames
        frames = decoder.feed(data)
        stats.bad_frames += decoder.bad_frames - bad_before
        source = f"{TagSource.UDP}:{addr[0]}:{addr[1]}"
        for frame in frames:
            stats.frames += 1
            if frame.cmd != CMD_TAG_REPORT:
                stats.other_frames += 1
                _LOGGER.debug("Ignored non-tag frame from %s: %s", source, frame)
                continue
            try:
                tag = parse_tag_record(
                    frame.data, strict=self.strict_tags, source=source
                )
            except FrameError as err:
                stats.bad_frames += 1
                _LOGGER.debug("Bad tag record from %s: %s", source, err)
                continue
            stats.tags += 1
            self._deliver(tag)

    def _deliver(self, tag: TagRead) -> None:
        if self._queue is not None:
            if self._queue.full():
                self._queue.get_nowait()
                self.stats.queue_dropped += 1
            self._queue.put_nowait(tag)
        if self.callback is None:
            return
        try:
            result = self.callback(tag)
        except Exception:  # noqa: BLE001 - documented boundary: a bad callback must not stop us
            self._log_callback_error("Tag callback raised for EPC %s", tag.epc)
            return
        if inspect.isawaitable(result):
            if len(self._tasks) >= MAX_PENDING_CALLBACKS:
                self.stats.callbacks_dropped += 1
                if inspect.iscoroutine(result):
                    result.close()  # never started; avoid "never awaited"
                if (
                    self.stats.callbacks_dropped in (1, 1000)
                    or not self.stats.callbacks_dropped % 10000
                ):
                    _LOGGER.warning(
                        "Async tag callback is too slow: %d call(s) dropped",
                        self.stats.callbacks_dropped,
                    )
                return
            task = asyncio.ensure_future(result)
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Future[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log_callback_error("Async tag callback raised: %r", exc, exc=exc)

    def _log_callback_error(
        self, msg: str, *args: object, exc: BaseException | None = None
    ) -> None:
        """Count a callback failure; log the first and then every 1000th."""
        self.stats.callback_errors += 1
        count = self.stats.callback_errors
        if count == 1 or count % 1000 == 0:
            _LOGGER.error(
                msg + " (%d callback error(s) so far)",
                *args,
                count,
                exc_info=exc if exc is not None else True,
            )
        else:
            _LOGGER.debug(msg, *args)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, found: dict[str, DiscoveredReader]) -> None:
        self._found = found

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        try:
            reader = parse_discovery(data)
        except UnexpectedReplyError as err:
            _LOGGER.debug("Ignored discovery datagram from %s: %s", addr[0], err)
            return
        self._found[reader.mac] = reader


async def discover(
    timeout: float = 5.0,
    *,
    bind: tuple[str, int] = ("0.0.0.0", DISCOVERY_PORT),
) -> list[DiscoveredReader]:
    """Listen for reader announcements on UDP 1111 for ``timeout`` seconds.

    This is passive, as in the SDK's device search: the readers broadcast
    ``MAC(6) IP(4) PORT(2)`` and nothing is sent. Unverified on hardware.
    """
    found: dict[str, DiscoveredReader] = {}
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DiscoveryProtocol(found), local_addr=bind, allow_broadcast=True
    )
    try:
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return sorted(found.values(), key=lambda r: r.ip)
