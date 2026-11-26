"""aiochainway: async client for Chainway UR4 fixed UHF RFID readers.

Unofficial clean-room implementation; not affiliated with Chainway.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .client import InventoryStream, UR4Client, wait_for_reader
from .listener import ListenerStats, UR4UdpListener, discover
from .models import (
    AntennaPower,
    AntennaState,
    DiscoveredReader,
    FrequencyRegion,
    Gen2Settings,
    GpiState,
    GpoState,
    InventoryMode,
    InventoryModeConfig,
    NetworkChangeResult,
    ReaderInfo,
    ReaderNetworkConfig,
    RfLink,
    TagRead,
    TagSource,
    TriggerConfig,
    TriggerReport,
    UdpDestination,
    WorkMode,
)
from .presence import TagPresence, TagSighting
from .protocol.errors import (
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
from .protocol.frame import Frame, FrameDecoder, decode, encode
from .protocol.tags import parse_tag_record
from .setup import ProvisionResult, ProvisionStep, provision

try:
    __version__ = _pkg_version("aiochainway")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "AntennaPower",
    "AntennaState",
    "ChainwayError",
    "ChecksumError",
    "ConnectionLostError",
    "DiscoveredReader",
    "Frame",
    "FrameDecoder",
    "FrameError",
    "FrequencyRegion",
    "Gen2Settings",
    "GpiState",
    "GpoState",
    "InventoryMode",
    "InventoryModeConfig",
    "InventoryStream",
    "ListenerStats",
    "NetworkChangeResult",
    "ProtocolError",
    "ProvisionResult",
    "ProvisionStep",
    "ReaderError",
    "ReaderInfo",
    "ReaderNetworkConfig",
    "ReaderTimeout",
    "RfLink",
    "TagPresence",
    "TagRead",
    "TagSighting",
    "TagSource",
    "TriggerConfig",
    "TriggerReport",
    "UR4Client",
    "UR4UdpListener",
    "UdpDestination",
    "UnexpectedReplyError",
    "UnsafeOperationError",
    "UnsupportedCommandError",
    "ValidationError",
    "WorkMode",
    "__version__",
    "decode",
    "discover",
    "encode",
    "parse_tag_record",
    "provision",
    "wait_for_reader",
]
