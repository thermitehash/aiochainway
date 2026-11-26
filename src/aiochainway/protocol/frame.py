"""Frame envelope: encoding, single-frame decoding and a streaming decoder.

Wire format (both directions, see ``docs/protocol.md``)::

    A5 5A | LEN_H LEN_L | CMD | DATA... | XOR | 0D 0A

``LEN`` is the total frame length (``len(DATA) + 8``), big-endian. ``XOR`` is the
XOR of every byte from ``LEN_H`` through the last ``DATA`` byte.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from .errors import ChecksumError, FrameError, ValidationError

_LOGGER = logging.getLogger("aiochainway")

HEADER = b"\xa5\x5a"
TRAILER = b"\x0d\x0a"
#: Bytes of envelope around DATA: header 2, length 2, cmd 1, xor 1, trailer 2.
OVERHEAD = 8
MIN_FRAME_LEN = OVERHEAD
#: The SDK's frame splitter rejects LEN > 2048 (``c.b.a(byte[])``).
MAX_FRAME_LEN = 2048
MAX_DATA_LEN = MAX_FRAME_LEN - OVERHEAD
#: Hard cap on bytes buffered between ``feed()`` calls.
MAX_BUFFER = 64 * 1024


def checksum(body: bytes) -> int:
    """Return the XOR of every byte in ``body`` (``LEN_H`` .. last ``DATA`` byte)."""
    value = 0
    for byte in body:
        value ^= byte
    return value


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame: command byte and data payload."""

    cmd: int
    data: bytes = b""

    def encode(self) -> bytes:
        """Return the frame as bytes on the wire."""
        return encode(self.cmd, self.data)

    @property
    def sub(self) -> int | None:
        """First data byte (the sub-command for ``0xA1``/``0xA2``), if any."""
        return self.data[0] if self.data else None

    def __str__(self) -> str:
        """Render as ``CMD=0xNN DATA=..`` for logs."""
        return f"CMD=0x{self.cmd:02X} DATA={self.data.hex(' ').upper() or '-'}"


def encode(cmd: int, data: bytes = b"") -> bytes:
    """Build a complete frame for ``cmd`` carrying ``data``.

    Raises:
        ValidationError: ``cmd`` is not a byte, or ``data`` is too long.

    """
    if not 0 <= cmd <= 0xFF:
        raise ValidationError(f"command byte out of range: {cmd!r}")
    data = bytes(data)
    if len(data) > MAX_DATA_LEN:
        raise ValidationError(
            f"data is {len(data)} bytes; the maximum is {MAX_DATA_LEN}"
        )
    length = len(data) + OVERHEAD
    body = bytes((length >> 8, length & 0xFF, cmd)) + data
    return HEADER + body + bytes((checksum(body),)) + TRAILER


def decode(raw: bytes) -> Frame:
    """Decode exactly one frame from ``raw``.

    Raises:
        FrameError: the bytes are not exactly one well-formed frame.
        ChecksumError: the frame is well-formed but its XOR byte is wrong.

    """
    raw = bytes(raw)
    if len(raw) < MIN_FRAME_LEN:
        raise FrameError(f"frame too short: {len(raw)} bytes")
    if raw[:2] != HEADER:
        raise FrameError(f"bad header {raw[:2].hex().upper()}")
    length = (raw[2] << 8) | raw[3]
    if not MIN_FRAME_LEN <= length <= MAX_FRAME_LEN:
        raise FrameError(f"LEN field {length} out of range")
    if length != len(raw):
        raise FrameError(f"LEN field says {length} bytes, got {len(raw)}")
    if raw[-2:] != TRAILER:
        raise FrameError(f"bad trailer {raw[-2:].hex().upper()}")
    body = raw[2:-3]
    expected = checksum(body)
    if raw[-3] != expected:
        raise ChecksumError(expected, raw[-3])
    return Frame(cmd=raw[4], data=raw[5:-3])


class FrameDecoder:
    """Streaming frame decoder with resynchronisation.

    Feed it bytes in any chunking; it returns every valid frame. Garbage, frames
    with a bad length, trailer or checksum are skipped: the decoder restarts the
    search one byte after the rejected ``A5`` so a real frame that starts inside
    the garbage is still found. It never raises on input bytes.

    Between calls at most ``max_frame_len - 1`` bytes stay buffered (a partial
    frame); ``max_buffer`` is a second, hard cap.
    """

    def __init__(
        self, max_frame_len: int = MAX_FRAME_LEN, max_buffer: int = MAX_BUFFER
    ) -> None:
        """Create a decoder; ``max_frame_len`` must be in 8..2048."""
        if not MIN_FRAME_LEN <= max_frame_len <= MAX_FRAME_LEN:
            raise ValidationError(f"max_frame_len out of range: {max_frame_len}")
        if max_buffer < max_frame_len:
            raise ValidationError("max_buffer must be at least max_frame_len")
        self.max_frame_len = max_frame_len
        self.max_buffer = max_buffer
        self._buf = bytearray()
        #: Candidate frames rejected (bad LEN, trailer or XOR).
        self.bad_frames = 0
        #: Bytes thrown away while searching for a header.
        self.discarded_bytes = 0
        #: Valid frames returned.
        self.frames = 0

    @property
    def buffered(self) -> int:
        """Number of bytes held while waiting for the rest of a frame."""
        return len(self._buf)

    def reset(self) -> None:
        """Drop any partial frame."""
        self.discarded_bytes += len(self._buf)
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        """Add ``data`` and return the frames completed by it, in order."""
        buf = self._buf
        buf += data
        out: list[Frame] = []
        pos = 0
        end = len(buf)
        while True:
            start = buf.find(HEADER, pos)
            if start < 0:
                # Keep a trailing A5: it may be the first half of a header.
                keep = 1 if end > pos and buf[end - 1] == HEADER[0] else 0
                self.discarded_bytes += end - pos - keep
                pos = end - keep
                break
            self.discarded_bytes += start - pos
            pos = start
            if end - pos < 4:
                break  # need the LEN bytes
            length = (buf[pos + 2] << 8) | buf[pos + 3]
            if not MIN_FRAME_LEN <= length <= self.max_frame_len:
                self.bad_frames += 1
                _LOGGER.debug("Rejected frame header: LEN %d out of range", length)
                pos += 1
                continue
            if end - pos < length:
                break  # wait for the rest
            frame_end = pos + length
            body = bytes(buf[pos + 2 : frame_end - 3])
            if buf[frame_end - 2 : frame_end] != TRAILER or buf[
                frame_end - 3
            ] != checksum(body):
                self.bad_frames += 1
                _LOGGER.debug(
                    "Rejected frame (bad trailer or XOR): %s",
                    bytes(buf[pos:frame_end]).hex(" ").upper(),
                )
                pos += 1
                continue
            out.append(Frame(cmd=body[2], data=body[3:]))
            pos = frame_end
        del buf[:pos]
        if len(buf) > self.max_buffer:  # pragma: no cover - unreachable by design
            self.reset()
        self.frames += len(out)
        return out
