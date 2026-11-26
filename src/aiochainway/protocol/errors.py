"""Exception hierarchy for aiochainway.

Every exception the library raises on purpose derives from :class:`ChainwayError`,
so callers can catch one type at an integration boundary::

    ChainwayError
    ├── ProtocolError
    │   ├── FrameError
    │   │   └── ChecksumError
    │   └── UnexpectedReplyError
    ├── ReaderError
    ├── ReaderTimeout
    ├── ConnectionLostError
    ├── UnsafeOperationError
    ├── UnsupportedCommandError
    └── ValidationError
"""

from __future__ import annotations


class ChainwayError(Exception):
    """Base class for every error raised by aiochainway."""


class ProtocolError(ChainwayError):
    """Bytes on the wire did not match the protocol."""


class FrameError(ProtocolError):
    """A frame or record is malformed (bad header, length, trailer or field)."""


class ChecksumError(FrameError):
    """The XOR checksum of a frame does not match its contents."""

    def __init__(self, expected: int, actual: int) -> None:
        """Store the checksum the frame carried and the one computed."""
        super().__init__(
            f"XOR checksum mismatch: frame has 0x{actual:02X}, computed 0x{expected:02X}"
        )
        self.expected = expected
        self.actual = actual


class UnexpectedReplyError(ProtocolError):
    """A reply frame arrived but its command, sub-command or length is wrong."""


class ReaderError(ChainwayError):
    """The reader answered, and the answer says the command failed."""

    def __init__(self, command: str, status: int | None, detail: str = "") -> None:
        """Store the command name and the status byte the reader returned."""
        text = f"{command}: reader reported failure"
        if status is not None:
            text += f" (status 0x{status:02X})"
        if detail:
            text += f": {detail}"
        super().__init__(text)
        self.command = command
        self.status = status


class ReaderTimeout(ChainwayError, TimeoutError):
    """No (matching) reply arrived within the timeout."""

    def __init__(self, command: str, timeout: float) -> None:
        """Store the command name and the timeout that expired."""
        super().__init__(f"{command}: no reply within {timeout:g} s")
        self.command = command
        self.timeout = timeout


class ConnectionLostError(ChainwayError, ConnectionError):
    """The TCP connection is closed, or closed while a command was in flight."""


class UnsafeOperationError(ChainwayError):
    """A network-changing or destructive command was called without confirmation."""


class UnsupportedCommandError(ChainwayError):
    """The command exists in the protocol but the UR4 does not support it."""


class ValidationError(ChainwayError, ValueError):
    """An argument is out of range or malformed; nothing was sent."""
