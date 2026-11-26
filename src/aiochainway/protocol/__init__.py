"""Byte-level Chainway UR4 protocol: frames, commands, tag records, errors.

This package is pure (no I/O). ``commands`` and ``tags`` import the data models,
so import them as submodules: ``from aiochainway.protocol import commands``.
"""

from __future__ import annotations

from .errors import (
    ChainwayError,
    ChecksumError,
    ConnectionLostError,
    FrameError,
    ProtocolError,
    ReaderError,
    ReaderTimeout,
    UnexpectedReplyError,
    UnsafeOperationError,
    UnsupportedCommandError,
    ValidationError,
)
from .frame import Frame, FrameDecoder, checksum, decode, encode

__all__ = [
    "ChainwayError",
    "ChecksumError",
    "ConnectionLostError",
    "Frame",
    "FrameDecoder",
    "FrameError",
    "ProtocolError",
    "ReaderError",
    "ReaderTimeout",
    "UnexpectedReplyError",
    "UnsafeOperationError",
    "UnsupportedCommandError",
    "ValidationError",
    "checksum",
    "decode",
    "encode",
]
