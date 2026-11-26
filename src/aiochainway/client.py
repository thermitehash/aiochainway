"""TCP client for the UR4 command port (default ``192.168.99.202:8888``)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
import contextlib
import logging
import random
import time
from types import TracebackType
from typing import Self
import weakref

from .models import (
    AntennaPower,
    AntennaState,
    FrequencyRegion,
    Gen2Settings,
    GpiState,
    GpoState,
    InventoryModeConfig,
    NetworkChangeResult,
    ReaderInfo,
    ReaderNetworkConfig,
    RfLink,
    TagRead,
    TagSource,
    TriggerConfig,
    UdpDestination,
    WorkMode,
)
from .protocol import commands as c
from .protocol.errors import (
    ChainwayError,
    ConnectionLostError,
    FrameError,
    ReaderError,
    ReaderTimeout,
    UnexpectedReplyError,
    UnsafeOperationError,
    UnsupportedCommandError,
    ValidationError,
)
from .protocol.frame import Frame, FrameDecoder
from .protocol.tags import parse_tag_record

_LOGGER = logging.getLogger("aiochainway")

DEFAULT_TIMEOUT = 3.0
#: How long to wait for the stop acknowledgement (0x8D) after an inventory.
DEFAULT_STOP_DRAIN = 0.5
#: Tags buffered during an inventory before the oldest are dropped.
DEFAULT_TAG_QUEUE = 10_000
_READ_CHUNK = 4096

DisconnectCallback = Callable[[BaseException | None], None]


def check_guard(name: str, *, confirm: bool, allow_unverified: bool) -> None:
    """Refuse a guarded command unless the caller confirmed it.

    Raises:
        UnsafeOperationError: ``confirm`` is false, or the command's layout is
            only ``inferred`` and ``allow_unverified`` is false.

    """
    info = c.COMMANDS[name]
    if not confirm:
        raise UnsafeOperationError(
            f"{name} changes the reader's network or settings; pass confirm=True"
        )
    if info.status is c.SpecStatus.INFERRED and not allow_unverified:
        raise UnsafeOperationError(
            f"{name}'s byte layout is inferred, not taken from the SDK; "
            "pass allow_unverified=True to send it anyway"
        )


def hexdump(data: bytes) -> str:
    """Return ``data`` as spaced upper-case hex."""
    return data.hex(" ").upper()


def _ack_abandoned(ack: asyncio.Future[None]) -> bool:
    """Whether a CancelledError came from the ack being cancelled, not our task."""
    task = asyncio.current_task()
    return ack.cancelled() and (task is None or task.cancelling() == 0)


class _Closed:
    """Sentinel put in the tag queue when the connection drops."""


_CLOSED = _Closed()


class InventoryStream:
    """Async iterator over tags from one continuous inventory.

    Created by :meth:`UR4Client.inventory`. It sends *start* on first use and
    **always** sends *stop* when it ends: on duration expiry, ``break``,
    cancellation, an exception, ``aclose()`` or garbage collection. The stop
    frame is written synchronously, so it is queued even if the task is
    cancelled again while finishing.
    """

    def __init__(self, client: UR4Client, duration: float | None) -> None:
        """Bind to ``client``; nothing is sent until iteration starts."""
        # Set first: __del__ may run on a half-constructed object.
        self._started = False
        self._finished = True
        if duration is not None and not duration > 0:  # also rejects NaN
            raise ValidationError(f"duration must be positive, got {duration!r}")
        self._client = client
        self._duration = duration
        self._queue: asyncio.Queue[TagRead | _Closed] = asyncio.Queue(
            client.tag_queue_size
        )
        self._deadline: float | None = None
        self._finished = False
        self._stop_ack: asyncio.Future[None] | None = None
        #: Tags dropped because the queue was full.
        self.dropped = 0

    def __aiter__(self) -> Self:
        """Return self."""
        return self

    async def __aenter__(self) -> Self:
        """Start the inventory now."""
        await self._start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the inventory."""
        await self.aclose()

    async def _start(self) -> None:
        if self._started:
            return
        client = self._client
        client._begin_inventory(self)  # raises if another inventory is running
        self._started = True
        try:
            await client._send_only(c.build_start_inventory())
        except BaseException:
            self._finish_now()
            raise
        loop = asyncio.get_running_loop()
        if self._duration is not None:
            self._deadline = loop.time() + self._duration

    def _offer(self, tag: TagRead | _Closed) -> None:
        """Queue a tag from the reader task, dropping the oldest when full."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                _LOGGER.warning(
                    "Inventory consumer too slow: %d tag(s) dropped", self.dropped
                )
        self._queue.put_nowait(tag)

    async def __anext__(self) -> TagRead:
        """Return the next tag; stop at the deadline."""
        if self._finished:
            # Hand out what was already received; a close marker raises.
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                raise StopAsyncIteration from None
            if isinstance(item, _Closed):
                raise ConnectionLostError("connection closed during inventory")
            return item
        try:
            await self._start()
            while True:
                timeout: float | None = None
                if self._deadline is not None:
                    timeout = self._deadline - asyncio.get_running_loop().time()
                    if timeout <= 0:
                        break
                try:
                    async with asyncio.timeout(timeout):
                        item = await self._queue.get()
                except TimeoutError:
                    break
                if isinstance(item, _Closed):
                    raise ConnectionLostError("connection lost during inventory")
                return item
        except BaseException:
            self._finish_now()
            raise
        await self.aclose()
        raise StopAsyncIteration

    def _finish_now(self) -> None:
        """End the inventory now and queue the stop frame (synchronous)."""
        if self._finished or not self._started:
            self._finished = True
            return
        self._finished = True
        self._stop_ack = self._client._end_inventory(self)

    async def aclose(self) -> None:
        """Stop the inventory and wait briefly for the reader's acknowledgement."""
        self._finish_now()
        ack, self._stop_ack = self._stop_ack, None
        if ack is None:
            return
        try:
            async with asyncio.timeout(self._client.stop_drain):
                await asyncio.shield(ack)
        except TimeoutError:
            _LOGGER.debug(
                "No stop acknowledgement (0x8D) within %ss", self._client.stop_drain
            )
        except asyncio.CancelledError:
            if not _ack_abandoned(ack):
                raise
            _LOGGER.debug("Stop acknowledgement abandoned (connection closed)")
        finally:
            if not ack.done():
                ack.cancel()

    def __del__(self) -> None:
        """Last resort: queue the stop frame if the stream was abandoned."""
        if self._started and not self._finished:
            with contextlib.suppress(RuntimeError):
                self._finish_now()


class UR4Client:
    """Async client for one UR4 over TCP.

    One request is in flight at a time; replies are matched by reply CMD (and
    sub-command for ``0xA2`` "get" replies). Frames that match nothing are
    logged at DEBUG and dropped, never handed to a waiting request.

    Example::

        async with UR4Client("192.168.99.202") as reader:
            print(await reader.get_version())
            async for tag in reader.inventory(duration=5):
                print(tag.epc, tag.rssi_dbm, tag.antenna)

    """

    def __init__(
        self,
        host: str,
        port: int = c.DEFAULT_TCP_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        connect_timeout: float | None = None,
        on_disconnect: DisconnectCallback | None = None,
        stop_drain: float = DEFAULT_STOP_DRAIN,
        strict_tags: bool = True,
        tag_queue_size: int = DEFAULT_TAG_QUEUE,
        stale_window: float | None = None,
    ) -> None:
        """Configure the client; call :meth:`connect` or use ``async with``."""
        if timeout <= 0:
            raise ValidationError("timeout must be positive")
        if not 1 <= port <= 0xFFFF:
            raise ValidationError(f"port must be 1-65535, got {port!r}")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout or timeout
        self.on_disconnect = on_disconnect
        self.stop_drain = stop_drain
        self.strict_tags = strict_tags
        self.tag_queue_size = tag_queue_size
        #: How long a late reply to a timed-out/cancelled request is waited for.
        self.stale_window = stale_window if stale_window is not None else 2 * timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pending: tuple[c.Request, asyncio.Future[Frame]] | None = None
        #: Reply keys of requests that timed out or were cancelled after being
        #: sent: (deadline, event set when the late reply lands).
        self._stale: dict[tuple[int, int | None], tuple[float, asyncio.Event]] = {}
        # Weak, so an abandoned stream (``break`` out of ``async for``) is
        # finalised at once and its __del__ sends stop.
        self._inventory_ref: weakref.ref[InventoryStream] | None = None
        self._stop_ack: asyncio.Future[None] | None = None
        self._decoder = FrameDecoder()
        self._closing = False
        #: Frames received that matched no request and were dropped.
        self.unmatched_frames = 0

    # Connection ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether the TCP connection is open."""
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._read_task is not None
            and not self._read_task.done()
        )

    @property
    def source(self) -> str:
        """Source label used on :class:`TagRead` objects from this client."""
        return f"{TagSource.TCP}:{self.host}:{self.port}"

    async def __aenter__(self) -> Self:
        """Connect."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close."""
        await self.close()

    async def connect(self) -> None:
        """Open the TCP connection and start the receive task.

        Raises:
            ReaderTimeout: the connection did not open within ``connect_timeout``.
            ConnectionLostError: the connection was refused or failed.

        """
        if self.connected:
            return
        await self._close_transport()
        try:
            async with asyncio.timeout(self.connect_timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError as err:
            raise ReaderTimeout("connect", self.connect_timeout) from err
        except (UnicodeError, ValueError) as err:
            raise ValidationError(f"invalid host {self.host!r}: {err}") from err
        except OSError as err:
            raise ConnectionLostError(
                f"cannot connect to {self.host}:{self.port}: {err}"
            ) from err
        self._reader, self._writer = reader, writer
        self._decoder = FrameDecoder()
        self._closing = False
        self._read_task = asyncio.create_task(
            self._read_loop(reader), name=f"aiochainway-read-{self.host}"
        )
        _LOGGER.debug("Connected to %s:%s", self.host, self.port)

    async def reconnect(
        self,
        *,
        attempts: int | None = None,
        base_delay: float = 0.5,
        max_delay: float = 60.0,
    ) -> None:
        """Reconnect with exponential backoff and jitter (delay capped at ``max_delay``).

        ``attempts=None`` retries forever; otherwise the last error is raised as
        :class:`ConnectionLostError` after that many failures.
        """
        await self._close_transport()
        attempt = 0
        while True:
            try:
                await self.connect()
            except (ConnectionLostError, ReaderTimeout) as err:
                attempt += 1
                if attempts is not None and attempt >= attempts:
                    raise ConnectionLostError(
                        f"giving up after {attempt} attempt(s): {err}"
                    ) from err
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                delay = random.uniform(delay / 2, delay)
                _LOGGER.debug(
                    "Reconnect attempt %d failed (%s); retry in %.2fs",
                    attempt,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                return

    async def close(self) -> None:
        """Close the connection. Safe to call more than once.

        A running inventory is stopped first (the stop frame is queued before
        the socket closes).
        """
        self._closing = True
        stream = self._inventory
        if stream is not None:
            stream._finish_now()  # queues the stop frame and detaches the stream
            stream._offer(_CLOSED)  # wake a consumer blocked on the queue
        await self._close_transport()

    async def _close_transport(self) -> None:
        task, self._read_task = self._read_task, None
        writer, self._writer = self._writer, None
        self._reader = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
        self._fail_waiters(ConnectionLostError("connection closed"))

    def _fail_waiters(self, err: ChainwayError) -> None:
        if self._pending is not None:
            _, fut = self._pending
            if not fut.done():
                fut.set_exception(err)
        if self._stop_ack is not None and not self._stop_ack.done():
            # Nobody may be awaiting it; cancel rather than set an exception
            # that would be reported as "never retrieved".
            self._stop_ack.cancel()
        self._stop_ack = None
        if self._inventory is not None:
            self._inventory._offer(_CLOSED)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        error: BaseException | None = None
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("<< %s", hexdump(chunk))
                for frame in self._decoder.feed(chunk):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError) as err:
            error = err
        except Exception as err:  # background-task boundary: log, never vanish
            _LOGGER.exception("Receive task for %s:%s failed", self.host, self.port)
            error = err
        if self._closing:
            return
        _LOGGER.debug(
            "Connection to %s:%s lost: %s", self.host, self.port, error or "EOF"
        )
        self._fail_waiters(
            ConnectionLostError(f"connection to {self.host}:{self.port} lost")
        )
        if self._writer is not None:
            self._writer.close()
        if self.on_disconnect is not None:
            try:
                self.on_disconnect(error)
            except Exception:
                _LOGGER.exception("on_disconnect callback raised")

    def _dispatch(self, frame: Frame) -> None:
        if frame.cmd == c.CMD_TAG_REPORT:
            self._dispatch_tag(frame)
            return
        if frame.cmd == 0x8D and self._stop_ack is not None:
            ack, self._stop_ack = self._stop_ack, None
            if not ack.done():
                ack.set_result(None)
            return
        key = c.reply_key(frame)
        stale = self._stale.pop(key, None)
        if stale is not None:
            stale[1].set()
            self.unmatched_frames += 1
            _LOGGER.debug("Dropped late reply to an abandoned request: %s", frame)
            return
        pending = self._pending
        if pending is not None and pending[0].matches(frame) and not pending[1].done():
            pending[1].set_result(frame)
            return
        self.unmatched_frames += 1
        _LOGGER.debug("Dropped unmatched frame %s", frame)

    def _dispatch_tag(self, frame: Frame) -> None:
        stream = self._inventory
        if stream is None:
            _LOGGER.debug("Dropped tag frame outside an inventory: %s", frame)
            return
        try:
            tag = parse_tag_record(
                frame.data, strict=self.strict_tags, source=self.source
            )
        except FrameError as err:
            _LOGGER.debug("Bad tag record %s: %s", frame, err)
            return
        stream._offer(tag)

    # Request plumbing --------------------------------------------------------

    def _require_writer(self) -> asyncio.StreamWriter:
        if not self.connected or self._writer is None:
            raise ConnectionLostError("not connected")
        return self._writer

    async def _write(self, data: bytes, name: str) -> None:
        writer = self._require_writer()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(">> %s  (%s)", hexdump(data), name)
        try:
            writer.write(data)
            async with asyncio.timeout(self.timeout):
                await writer.drain()
        except TimeoutError as err:
            raise ReaderTimeout(name, self.timeout) from err
        except (OSError, ConnectionError) as err:
            raise ConnectionLostError(f"{name}: write failed: {err}") from err

    async def _send_only(self, request: c.Request) -> None:
        async with self._lock:
            await self._write(request.encode(), request.name)

    async def _request(self, request: c.Request) -> Frame:
        """Send ``request`` and return its reply frame."""
        loop = asyncio.get_running_loop()
        key = request.reply_key
        assert key is not None  # only requests that expect a reply come here
        async with self._lock:
            await self._wait_out_stale(key)
            fut: asyncio.Future[Frame] = loop.create_future()
            self._pending = (request, fut)
            sent = False
            try:
                await self._write(request.encode(), request.name)
                sent = True
                async with asyncio.timeout(self.timeout):
                    return await fut
            except TimeoutError as err:
                if sent:
                    self._mark_stale(key)
                raise ReaderTimeout(request.name, self.timeout) from err
            except asyncio.CancelledError:
                # The bytes may already be on the wire; its reply must not be
                # handed to the next request of the same kind.
                self._mark_stale(key)
                raise
            finally:
                self._pending = None

    def _mark_stale(self, key: tuple[int, int | None]) -> None:
        deadline = asyncio.get_running_loop().time() + self.stale_window
        self._stale[key] = (deadline, asyncio.Event())

    async def _wait_out_stale(self, key: tuple[int, int | None]) -> None:
        """Wait until a late reply for ``key`` has landed or can be ignored."""
        stale = self._stale.get(key)
        if stale is None:
            return
        deadline, landed = stale
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(remaining):
                    await landed.wait()
        if self._stale.get(key) is stale:
            del self._stale[key]

    @property
    def _inventory(self) -> InventoryStream | None:
        ref = self._inventory_ref
        return ref() if ref is not None else None

    def _begin_inventory(self, stream: InventoryStream) -> None:
        if self._inventory is not None:
            raise UnsafeOperationError("an inventory is already running")
        self._require_writer()
        self._inventory_ref = weakref.ref(stream)

    def _end_inventory(self, stream: InventoryStream) -> asyncio.Future[None] | None:
        """Detach ``stream`` and synchronously queue the stop frame."""
        current = self._inventory
        if current is stream or current is None:
            self._inventory_ref = None
        writer = self._writer
        if writer is None or writer.is_closing():
            _LOGGER.debug("Inventory ended without a connection; stop not sent")
            return None
        data = c.build_stop_inventory().encode()
        ack: asyncio.Future[None] | None = None
        with contextlib.suppress(RuntimeError):
            ack = asyncio.get_running_loop().create_future()
            previous, self._stop_ack = self._stop_ack, ack
            if previous is not None and not previous.done():
                previous.cancel()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(">> %s  (stop_inventory)", hexdump(data))
        try:
            writer.write(data)
        except (OSError, ConnectionError, RuntimeError) as err:
            _LOGGER.debug("Could not send stop_inventory: %s", err)
            return None
        return ack

    # Inventory ---------------------------------------------------------------

    def inventory(self, duration: float | None = None) -> InventoryStream:
        """Run a continuous inventory, yielding :class:`TagRead` objects.

        ``async for tag in reader.inventory(duration=5): ...`` sends *start*,
        yields tags until ``duration`` seconds pass (or forever), and always
        sends *stop* at the end. Commands may be sent while it runs. Tag frames
        that arrive outside an inventory are dropped.
        """
        return InventoryStream(self, duration)

    async def stop_inventory(self) -> None:
        """Send *stop inventory* outside an inventory stream (e.g. at start-up).

        Waits up to ``stop_drain`` seconds for the acknowledgement; a missing
        acknowledgement is not an error, as in the SDK.
        """
        stream = self._inventory
        if stream is not None:
            await stream.aclose()
            return
        async with self._lock:
            writer = self._require_writer()
            loop = asyncio.get_running_loop()
            ack: asyncio.Future[None] = loop.create_future()
            self._stop_ack = ack
            data = c.build_stop_inventory().encode()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(">> %s  (stop_inventory)", hexdump(data))
            writer.write(data)
            try:
                async with asyncio.timeout(self.stop_drain):
                    await asyncio.shield(ack)
            except TimeoutError:
                _LOGGER.debug("No stop acknowledgement within %ss", self.stop_drain)
            except asyncio.CancelledError:
                if not _ack_abandoned(ack):
                    raise
                raise ConnectionLostError(
                    "connection lost while waiting for the stop acknowledgement"
                ) from None
            finally:
                if self._stop_ack is ack:
                    self._stop_ack = None

    # Read commands -----------------------------------------------------------

    async def get_version(self) -> str:
        """Return the firmware version string, e.g. ``"V1.2.3"``."""
        return c.parse_get_version(await self._request(c.build_get_version()))

    async def get_temperature(self) -> float:
        """Return the UHF module temperature in °C."""
        return c.parse_get_temperature(await self._request(c.build_get_temperature()))

    async def get_info(self) -> ReaderInfo:
        """Return version and temperature (``None`` if the reader refuses it)."""
        version = await self.get_version()
        try:
            temperature: float | None = await self.get_temperature()
        except (ReaderError, UnexpectedReplyError) as err:
            _LOGGER.debug("Temperature unavailable: %s", err)
            temperature = None
        return ReaderInfo(version=version, temperature=temperature)

    async def get_work_mode(self) -> WorkMode:
        """Return the work mode (command, auto or trigger)."""
        return c.parse_get_work_mode(await self._request(c.build_get_work_mode()))

    async def get_network_config(self) -> ReaderNetworkConfig:
        """Return the reader's IP, mask, gateway and TCP port."""
        return c.parse_get_network_config(
            await self._request(c.build_get_network_config())
        )

    async def get_udp_destination(self) -> UdpDestination | None:
        """Return the auto/trigger-mode UDP destination, or ``None`` if unset."""
        return c.parse_get_udp_destination(
            await self._request(c.build_get_udp_destination())
        )

    async def get_antennas(self) -> AntennaState:
        """Return which antennas are enabled."""
        return c.parse_get_antennas(await self._request(c.build_get_antennas()))

    async def get_power_all(self) -> list[AntennaPower]:
        """Return the power of each antenna."""
        return c.parse_get_power(await self._request(c.build_get_power()))

    async def get_region(self) -> FrequencyRegion | int:
        """Return the frequency region (an ``int`` if the code is unknown)."""
        return c.parse_get_region(await self._request(c.build_get_region()))

    async def get_gpi(self) -> GpiState:
        """Return the GPI input levels."""
        return c.parse_get_gpi(await self._request(c.build_get_gpi()))

    async def get_trigger_config(self) -> TriggerConfig:
        """Return the trigger-mode parameters."""
        return c.parse_get_trigger_config(
            await self._request(c.build_get_trigger_config())
        )

    async def get_beep(self) -> bool:
        """Return whether the reader beeps on reads."""
        return c.parse_get_beep(await self._request(c.build_get_beep()))

    async def get_rf_link(self) -> RfLink | int:
        """Return the RF link profile."""
        return c.parse_get_rf_link(await self._request(c.build_get_rf_link()))

    async def get_gen2(self) -> Gen2Settings:
        """Return the Gen2 settings."""
        return c.parse_get_gen2(await self._request(c.build_get_gen2()))

    async def get_inventory_mode(self) -> InventoryModeConfig:
        """Return which memory banks each tag report carries."""
        return c.parse_get_inventory_mode(
            await self._request(c.build_get_inventory_mode())
        )

    async def get_antenna_work_time(self, antenna: int) -> int:
        """Return one antenna's dwell time in ms."""
        return c.parse_get_antenna_work_time(
            await self._request(c.build_get_antenna_work_time(antenna))
        )

    # Write commands ----------------------------------------------------------

    async def set_work_mode(self, mode: WorkMode) -> None:
        """Set the work mode."""
        c.parse_set_work_mode(await self._request(c.build_set_work_mode(mode)))

    async def set_antennas(self, antennas: AntennaState | Iterable[int]) -> None:
        """Enable exactly these antennas (others are disabled)."""
        state = (
            antennas
            if isinstance(antennas, AntennaState)
            else AntennaState(frozenset(antennas))
        )
        c.parse_set_antennas(await self._request(c.build_set_antennas(state)))

    async def set_power(self, antenna: int, dbm: int) -> None:
        """Set one antenna's read and write power (1-30 dBm)."""
        c.parse_set_power(await self._request(c.build_set_power(antenna, dbm)))

    async def set_power_all(self, powers: Mapping[int, int]) -> None:
        """Set several antennas' power in one command, ``{antenna: dbm}``."""
        c.parse_set_power_all(
            await self._request(c.build_set_power_all(powers.items()))
        )

    async def set_region(self, region: FrequencyRegion) -> None:
        """Set the frequency region."""
        c.parse_set_region(await self._request(c.build_set_region(region)))

    async def set_gpo(
        self,
        state: GpoState | None = None,
        *,
        output1: bool = False,
        output2: bool = False,
        relay: bool = False,
    ) -> None:
        """Set GPO outputs and the relay (all three are written every time)."""
        state = state or GpoState(output1=output1, output2=output2, relay=relay)
        c.parse_set_gpo(await self._request(c.build_set_gpo(state)))

    async def set_trigger_config(self, config: TriggerConfig) -> None:
        """Set trigger-mode parameters."""
        c.parse_set_trigger_config(
            await self._request(c.build_set_trigger_config(config))
        )

    async def set_beep(self, enabled: bool) -> None:
        """Turn the beep on reads on or off."""
        c.parse_set_beep(await self._request(c.build_set_beep(enabled)))

    async def set_rf_link(self, link: RfLink) -> None:
        """Set the RF link profile."""
        c.parse_set_rf_link(await self._request(c.build_set_rf_link(link)))

    async def set_gen2(self, settings: Gen2Settings) -> None:
        """Set the Gen2 settings."""
        c.parse_set_gen2(await self._request(c.build_set_gen2(settings)))

    async def set_inventory_mode(self, config: InventoryModeConfig) -> None:
        """Choose which memory banks each tag report carries."""
        c.parse_set_inventory_mode(
            await self._request(c.build_set_inventory_mode(config))
        )

    async def set_antenna_work_time(self, antenna: int, ms: int) -> None:
        """Set one antenna's dwell time (10-65535 ms)."""
        c.parse_set_antenna_work_time(
            await self._request(c.build_set_antenna_work_time(antenna, ms))
        )

    async def set_fastid(self, enabled: bool) -> None:
        """Not supported: the SDK's UR4 class refuses FastID without sending."""
        raise UnsupportedCommandError("FastID is not supported on the UR4")

    async def set_tagfocus(self, enabled: bool) -> None:
        """Not supported: the SDK's UR4 class refuses TagFocus without sending."""
        raise UnsupportedCommandError("TagFocus is not supported on the UR4")

    async def read_tag_buffer(self) -> None:
        """Not supported: the SDK's UR4 path never reads the tag buffer (0xE0)."""
        raise UnsupportedCommandError(
            "read_tag_buffer (0xE0) is not used by the UR4; use inventory()"
        )

    # Guarded commands --------------------------------------------------------

    async def set_network_config(
        self,
        config: ReaderNetworkConfig,
        *,
        confirm: bool = False,
        allow_unverified: bool = False,
    ) -> NetworkChangeResult:
        """Change the reader's IP, mask, gateway and port.

        Requires ``confirm=True``. Success only means the reader acknowledged;
        reconnect at the new address and read the settings back to verify.
        """
        check_guard(
            "set_network_config", confirm=confirm, allow_unverified=allow_unverified
        )
        c.parse_set_network_config(
            await self._request(c.build_set_network_config(config))
        )
        return NetworkChangeResult(requested=config, accepted=True)

    async def set_udp_destination(
        self,
        dest: UdpDestination,
        *,
        confirm: bool = False,
        allow_unverified: bool = False,
    ) -> None:
        """Change where auto/trigger-mode tags are sent. Requires ``confirm=True``."""
        check_guard(
            "set_udp_destination", confirm=confirm, allow_unverified=allow_unverified
        )
        c.parse_set_udp_destination(
            await self._request(c.build_set_udp_destination(dest))
        )

    async def factory_reset(
        self, *, confirm: bool = False, allow_unverified: bool = False
    ) -> None:
        """Restore factory settings (SDK ``resetUHFSoft``). Requires ``confirm=True``.

        This may also reset the network settings back to ``192.168.99.202``.
        """
        check_guard("factory_reset", confirm=confirm, allow_unverified=allow_unverified)
        c.parse_factory_reset(await self._request(c.build_factory_reset()))


async def wait_for_reader(
    host: str, port: int = c.DEFAULT_TCP_PORT, *, timeout: float = 30.0
) -> float:
    """Poll until a TCP connection to ``host:port`` succeeds; return seconds waited.

    Raises:
        ReaderTimeout: the reader did not accept a connection in time.

    """
    start = time.monotonic()
    deadline = start + timeout
    while True:
        client = UR4Client(host, port, timeout=min(3.0, timeout))
        try:
            await client.connect()
        except ConnectionLostError, ReaderTimeout:
            if time.monotonic() >= deadline:
                raise ReaderTimeout("wait_for_reader", timeout) from None
            await asyncio.sleep(1.0)
        else:
            await client.close()
            return time.monotonic() - start
